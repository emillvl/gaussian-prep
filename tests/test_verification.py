import pytest

from gaussian_prep.expressions import ExpressionReader, MathUnsupported
from gaussian_prep.models import MathModel
from gaussian_prep.verification import compute, verify
from conftest import mathematical_model, question


def test_mcq_checks_key_and_all_choices():
    result = compute(question(), mathematical_model())
    assert result["status"] == "pass"
    assert result["computed_targets"] == ["6"]
    assert len(result["options"]) == 4
    assert "distractors" not in result


def test_wrong_key_is_blocked():
    q = question().model_copy(update={"answer": "B"})
    assert compute(q, mathematical_model())["status"] == "fail"


def test_equivalent_duplicate_choices_are_blocked():
    model = mathematical_model()
    model.options[1].expression = "12/2"
    result = compute(question(), model)
    assert result["status"] == "fail"
    assert any("equivalent" in issue for issue in result["issues"])


def test_spr_accepts_computed_key_without_entry_grader():
    q = question(format="SPR")
    q.answer = "12/2"
    assert compute(q, mathematical_model(format="SPR"))["status"] == "pass"


def test_wrong_spr_key_is_blocked():
    q = question(format="SPR")
    q.answer = "7"
    assert compute(q, mathematical_model(format="SPR"))["status"] == "fail"


def model(**updates):
    data = dict(mode="solve", interpretation="Test mathematical interpretation.", variables=["x"], equations=[{"lhs": "x**2", "rhs": "9"}], constraints=[], target="x", aggregate="each", options=[], unsupported_reason="")
    data.update(updates)
    return MathModel(**data)


def test_spr_may_have_more_than_one_correct_value():
    q = question(format="SPR")
    q.answer = "-3"
    result = compute(q, model())
    assert result["status"] == "pass"
    assert set(result["computed_targets"]) == {"-3", "3"}


def test_constraints_exclude_otherwise_valid_root():
    q = question(format="SPR")
    q.answer = "-3"
    result = compute(q, model(constraints=[{"lhs": "x", "operator": "gt", "rhs": "0"}]))
    assert result["status"] == "fail"
    assert result["computed_targets"] == ["3"]


def test_original_denominator_is_preserved_after_simplification():
    q = question(format="SPR")
    q.answer = "1"
    m = model(equations=[{"lhs": "(x**2-1)/(x-1)", "rhs": "2"}])
    result = compute(q, m)
    assert result["status"] == "unverified"
    assert not result["solutions"]


def test_radical_equation_does_not_accept_extraneous_root():
    q = question(format="SPR")
    q.answer = "-1"
    assert compute(q, model(equations=[{"lhs": "sqrt(x+2)", "rhs": "x"}]))["status"] == "fail"


def test_finite_system_and_target_expression():
    q = question(format="SPR")
    q.answer = "10"
    m = model(variables=["x", "y"], equations=[{"lhs": "x+y", "rhs": "7"}, {"lhs": "x-y", "rhs": "3"}], target="2*x")
    assert compute(q, m)["status"] == "pass"


def test_sum_of_distinct_roots():
    q = question(format="SPR")
    q.answer = "0"
    assert compute(q, model(aggregate="sum"))["status"] == "pass"


def test_population_standard_deviation_is_computed():
    q = question(format="SPR")
    q.answer = "sqrt(2/3)"
    m = model(mode="evaluate", variables=[], equations=[], target="stdev([1,2,3])")
    assert compute(q, m)["status"] == "pass"


def test_geometry_expression_uses_actual_computation():
    q = question(format="SPR")
    q.answer = "13"
    m = model(mode="evaluate", variables=[], equations=[], target="sqrt(5**2+12**2)")
    assert compute(q, m)["status"] == "pass"


def test_underdetermined_system_is_not_approved():
    m = model(variables=["x", "y"], equations=[{"lhs": "x+y", "rhs": "5"}])
    assert compute(question(format="SPR"), m)["status"] == "unverified"


def test_unsupported_problem_is_not_approved():
    m = model(mode="unsupported", unsupported_reason="Requires an unsupported proof.")
    assert compute(question(format="SPR"), m)["status"] == "unverified"


@pytest.mark.parametrize("expression", ["__import__('os').system('echo bad')", "x.__class__", "[x for x in [1]]", "open('file')", "(lambda: 1)()", "2**1000000000", "1/0", "True", "[1]*1000000"])
def test_generated_code_and_resource_abuse_are_rejected(expression):
    with pytest.raises((MathUnsupported, TypeError)):
        ExpressionReader(["x"]).read(expression)


def test_polynomial_equivalence_checks_wrong_choices():
    q = question()
    m = model(mode="identity", equations=[], target="(x+1)**2", options=[{"label": k, "expression": v} for k, v in zip("ABCD", ["x**2+2*x+1", "x**2+1", "x**2+2*x", "x**2+2*x-1"])])
    assert compute(q, m)["status"] == "pass"


def test_latex_spr_answer_is_normalized():
    q = question(format="SPR")
    q.answer = r"\frac{3}{4}"
    assert compute(q, model(equations=[{"lhs": "x", "rhs": "3/4"}], target="x"))["status"] == "pass"


def test_to_engine_expression_converts_latex_and_notation():
    from gaussian_prep.expressions import to_engine_expression
    assert to_engine_expression(r"\frac{4}{15}") == "(4)/(15)"
    assert to_engine_expression(r"\dfrac{7}{24}") == "(7)/(24)"
    assert to_engine_expression(r"\sqrt{2}") == "sqrt(2)"
    assert to_engine_expression(r"2^{3}") == "2**(3)"
    assert to_engine_expression("50%") == "50/100"
    assert to_engine_expression("1,200") == "1200"
    assert to_engine_expression("[1,2,3]") == "[1,2,3]"


def test_worker_runs_real_sympy():
    result = verify(question(), mathematical_model())
    assert result["status"] == "pass"
    assert result["engine"].startswith("SymPy")


def test_worker_timeout_does_not_pass():
    assert verify(question(), mathematical_model(), timeout=0.001)["status"] == "unverified"


def test_worker_is_killed_when_the_parent_is_interrupted(monkeypatch):
    import gaussian_prep.verification as module

    class FakeProcess:
        returncode = None

        def __init__(self):
            self.killed = False
            self._first = True

        def wait(self, timeout=None):
            if self._first:
                self._first = False
                raise KeyboardInterrupt
            return 0

        def poll(self):
            return 0 if self.killed else None

        def kill(self):
            self.killed = True

    fake = FakeProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: fake)
    with pytest.raises(KeyboardInterrupt):
        verify(question(), mathematical_model())
    assert fake.killed


def test_trig_complementary_equation_is_solved():
    q = question().model_copy(update={"answer": "B", "choices": [{"label": label, "text": text} for label, text in zip("ABCD", ["40", "44", "46", "80"])]})
    m = MathModel(mode="solve", interpretation="sin A = cos B with both angles acute.", variables=["x"],
        equations=[{"lhs": "sin((3*x+10)*pi/180)", "rhs": "cos((2*x+20)*pi/180)"}],
        constraints=[{"lhs": "3*x+10", "operator": "gt", "rhs": "0"}, {"lhs": "3*x+10", "operator": "lt", "rhs": "90"}, {"lhs": "2*x+20", "operator": "gt", "rhs": "0"}, {"lhs": "2*x+20", "operator": "lt", "rhs": "90"}],
        target="2*x+20", aggregate="each",
        options=[{"label": label, "expression": text} for label, text in zip("ABCD", ["40", "44", "46", "80"])], unsupported_reason="")
    result = compute(q, m)
    assert result["status"] == "pass"
    assert result["computed_targets"] == ["44"]


def test_conditional_identity_is_verified():
    q = question().model_copy(update={"answer": "A", "choices": [{"label": label, "text": text} for label, text in zip("ABCD", ["6(sqrt(x)+3)/(x-9)", "6(sqrt(x)-3)/(x-9)", "6(sqrt(x)+3)/(x+9)", "(sqrt(x)+3)/(x-9)"])]})
    m = MathModel(mode="identity", interpretation="Equivalent expression over its domain.", variables=["x"], equations=[],
        constraints=[{"lhs": "x", "operator": "ge", "rhs": "0"}, {"lhs": "sqrt(x)-3", "operator": "ne", "rhs": "0"}],
        target="6/(sqrt(x)-3)", aggregate="each",
        options=[{"label": label, "expression": text} for label, text in zip("ABCD", ["6*(sqrt(x)+3)/(x-9)", "6*(sqrt(x)-3)/(x-9)", "6*(sqrt(x)+3)/(x+9)", "(sqrt(x)+3)/(x-9)"])], unsupported_reason="")
    assert compute(q, m)["status"] == "pass"
    assert compute(q.model_copy(update={"answer": "B"}), m)["status"] == "fail"
