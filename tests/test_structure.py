import copy
import json

import pytest

from conftest import ScriptedProvider, audit_result, mathematical_model, question, slot
from gaussian_prep.corpus import Corpus
from gaussian_prep.export import build_docx, export, render_latex
from gaussian_prep.models import AssessmentStructure, Plan
from gaussian_prep.pipeline import Pipeline, PipelineError, RequestConflict, check_plan
from gaussian_prep.prep import prep_profile
from gaussian_prep.recovery import recover_saved_run
from gaussian_prep.run_summary import write_summary
from gaussian_prep.structure import check_structure_request, ensure_structure, requested_kind, requested_order, structure_errors
from gaussian_prep.verification import compute


def structured_plan(kind="mock", sequences=None):
    sequences = sequences or [["MCQ", "SPR"], ["MCQ", "SPR"]]
    slots, sections = [], []
    for module, formats in enumerate(sequences, 1):
        sections.append(dict(module=module, name=f"Paper {module}", minutes=10, format_sequence=formats,
            mcq_choices=4, domain_counts={"Algebra": len(formats)}, instructions=["Answer every question."]))
        for position, fmt in enumerate(formats, 1):
            slots.append(slot(len(slots) + 1, fmt).model_copy(update={"module": module, "position": position}))
    return Plan(title="Structured test", interpretation="Follow the supplied shape.", assumptions=[], requirements=[],
        total_questions=len(slots), module_count=len(sequences), minutes_per_module=[10] * len(sequences), slots=slots,
        structure=AssessmentStructure(kind=kind, target="Fixture exam paper", basis="User-specified test fixture, not an official exam specification.",
            sections=sections, question_style=["Use concise mathematical questions."]))


@pytest.mark.parametrize("prompt,kind", [("Generate a mock SAT Math exam", "mock"), ("A full-length paper", "mock"),
    ("A full 44-question SAT Math practice test", "mock"), ("Make a topic drill", "practice"),
    ("A shortened mock", "practice"), ("A practice set, not a mock", "practice")])
def test_mock_and_practice_intent(prompt, kind):
    assert requested_kind(prompt) == kind


@pytest.mark.parametrize("prompt,expected", [("MCQ first and SPR last", "open_last"),
    ("SPR first and MCQ last", "open_first"), ("Put SPR after MCQ", "open_last"),
    ("Put SPR before MCQ", "open_first"), ("Interleave MCQ and SPR", None),
    ("Use mixed difficulty and mixed question types", "open_last")])
def test_order_preferences(prompt, expected):
    assert requested_order(prompt, "SAT Math", "mock") == expected


def test_mock_requires_an_explicit_structure_but_practice_has_legacy_fallback():
    plan = structured_plan()
    plan.structure = None
    with pytest.raises(ValueError, match="explicit assessment structure"):
        ensure_structure(plan, "Create a mock")
    ensure_structure(plan, "Generate 4 practice questions")
    assert plan.structure.kind == "practice"


def test_end_placement_is_checked_in_each_module(corpus):
    plan = structured_plan(sequences=[["MCQ", "SPR"], ["SPR", "MCQ"]])
    with pytest.raises(PipelineError, match="Paper 2.*at the end"):
        check_plan(plan, "Generate a mock", corpus)
    check_plan(plan, "Generate a mock and interleave question types", corpus)


@pytest.mark.parametrize("defect", ["time", "quota", "sequence", "kind", "requested_time"])
def test_structure_mismatches_are_not_just_audit_suggestions(corpus, defect):
    plan = structured_plan()
    request = "Generate a mock"
    if defect == "time":
        plan.minutes_per_module[0] = 15
    elif defect == "quota":
        plan.structure.sections[0].domain_counts = {"Advanced Math": 1}
    elif defect == "sequence":
        plan.structure.sections[0].format_sequence = ["MCQ", "MCQ"]
    elif defect == "kind":
        plan.structure.kind = "practice"
    else:
        request += " with 20 minutes per module"
    with pytest.raises(PipelineError, match="Structure check"):
        check_plan(plan, request, corpus)


def test_dataset_blueprint_constrains_mocks_but_not_custom_practice(corpus, tmp_path):
    plan = structured_plan()
    path = tmp_path / "with-structure.json"
    path.write_text(json.dumps({"exam_structure": plan.structure.model_dump(), "questions": corpus.records}))
    loaded = Corpus(path)
    check_plan(plan, "Create a mock", loaded)
    plan.minutes_per_module[0] = plan.structure.sections[0].minutes = 15
    with pytest.raises(PipelineError, match="dataset's exam_structure"):
        check_plan(plan, "Create a mock", loaded)
    plan.structure.kind = "practice"
    check_plan(plan, "Create a custom practice set", loaded)
    assert loaded.report()["exam_structure"]["sections"][0]["minutes"] == 10


class StructuredProvider(ScriptedProvider):
    def __init__(self, bad_first=False, structure_conflict=False):
        super().__init__(4)
        self.plans = 0
        self.bad_first = bad_first
        self.structure_conflict = structure_conflict

    def ask(self, role, payload, schema):
        if role == "planner":
            self.calls.append((role, copy.deepcopy(payload)))
            self.plans += 1
            if self.bad_first and self.plans == 1:
                return structured_plan(sequences=[["SPR", "MCQ"], ["MCQ", "SPR"]])
            return structured_plan()
        if role == "generator":
            self.calls.append((role, copy.deepcopy(payload)))
            number = int(payload["slot"]["id"][1:])
            self.generated[payload["slot"]["id"]] = 1
            return question(number, 17 + 2 * number, payload["slot"]["format"])
        if role == "verifier":
            self.calls.append((role, copy.deepcopy(payload)))
            number = int(payload["question"]["slot_id"][1:])
            return mathematical_model(17 + 2 * number, payload["question"]["format"])
        if role == "auditor" and self.structure_conflict:
            return audit_result(payload, schema, accepted=False, summary="Paper identity conflict", issues=[dict(
                dimension="structure", reason="The requested paper differs from the declared target.", slot_ids=[], instruction="Specify the intended paper.")])
        return super().ask(role, payload, schema)


def test_ordering_repair_happens_before_generation_and_contract_reaches_all_roles(corpus, tmp_path):
    provider = StructuredProvider(bad_first=True)
    run = tmp_path / "run"
    state = Pipeline(corpus, provider, run, compute=compute).run("Generate a mock")
    assert provider.plans == 2
    assert "at the end" in provider.calls[1][1]["correction"]
    assert [role for role, _ in provider.calls[:2]] == ["planner", "planner"]
    for role, payload in provider.calls[2:]:
        assert payload["assessment_structure"] == state["plan"]["structure"]
        if role == "verifier":
            assert "answer" not in payload["question"]
    assert state["metrics"]["structure_errors"] == []
    before = copy.deepcopy(state["plan"]["structure"])
    resumed = ScriptedProvider()
    after = Pipeline(corpus, resumed, run, compute=compute).run("Generate a mock", resume=True)
    assert after["plan"]["structure"] == before
    assert not resumed.calls
    assert recover_saved_run(corpus, run, compute=compute)["status"] == "complete"


def test_wrong_choice_count_is_revised_before_independent_verification(corpus, tmp_path):
    corpus = Corpus(corpus.path, prep_profile("A-Levels Mathematics"))
    class FiveChoices(StructuredProvider):
        def ask(self, role, payload, schema):
            result = super().ask(role, payload, schema)
            if role == "planner":
                result = structured_plan(sequences=[["MCQ"]])
                result.structure.sections[0].mcq_choices = 5
            return result
    provider = FiveChoices()
    with pytest.raises(PipelineError, match="did not pass"):
        Pipeline(corpus, provider, tmp_path / "run", revisions=0, compute=compute).run("Generate a mock")
    assert not any(role == "verifier" for role, _ in provider.calls)


def test_auditor_structure_conflict_cannot_finish_as_warning(corpus, tmp_path):
    with pytest.raises(RequestConflict, match="Exam structure needs clarification"):
        Pipeline(corpus, StructuredProvider(structure_conflict=True), tmp_path, compute=compute, audit_revisions=0).run("Generate a mock")
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["status"] == "needs_attention"
    assert recover_saved_run(corpus, tmp_path, compute=compute)["status"] == "needs_attention"


def test_exports_preserve_section_names_directions_and_order(corpus, tmp_path):
    state = Pipeline(corpus, StructuredProvider(), tmp_path / "run", compute=compute).run("Generate a mock")
    path = tmp_path / "questions.docx"
    build_docx(state, path)
    from docx import Document
    text = "\n".join(p.text for p in Document(path).paragraphs)
    assert "Paper 1" in text and "Paper 2" in text and "Mock exam" in text
    assert text.count("Answer every question.") == 2
    tex = render_latex(state)
    assert r"\section*{Paper 1}" in tex and "Mock exam" in tex
    assert tex.index("= 19") < tex.index("= 21") < tex.index("= 23") < tex.index("= 25")
    summary = write_summary(state, tmp_path).read_text(encoding="utf-8")
    assert "1: MCQ; 2: SPR" in summary and "Mock exam" in summary
    state["items"]["q1"]["question"]["format"] = "SPR"
    state["items"]["q1"]["question"]["choices"] = []
    state["items"]["q1"]["question"]["answer"] = "7"
    assert structure_errors(Plan.model_validate(state["plan"]), state["items"])
    with pytest.raises(ValueError, match="Cannot export"):
        export(state, tmp_path / "blocked", "docx")
    assert not (tmp_path / "blocked").exists()
