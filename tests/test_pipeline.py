import json

import pytest

from gaussian_prep.models import Audit, MathModel
from gaussian_prep.export import export
from gaussian_prep.pipeline import Pipeline, PipelineError
from gaussian_prep.verification import compute
from conftest import audit_result, ScriptedProvider


def pipeline(corpus, provider, run_dir, **kwargs):
    return Pipeline(corpus, provider, run_dir, progress=lambda _: None, compute=compute, **kwargs)


def test_end_to_end_and_blind_verifier(corpus, tmp_path):
    provider = ScriptedProvider()
    state = pipeline(corpus, provider, tmp_path / "run").run("Generate 1 question")
    assert state["status"] == "complete"
    assert [r for r, _ in provider.calls] == ["planner", "generator", "verifier", "reviewer", "auditor"]
    student = next(p["question"] for r, p in provider.calls if r == "verifier")
    assert not {"answer", "solution"}.intersection(student)
    written = export(state, tmp_path / "run", "docx")
    assert written and all(path.is_file() for path in written)


def test_failed_computation_cannot_be_overridden(corpus, tmp_path):
    provider = ScriptedProvider()
    process = Pipeline(corpus, provider, tmp_path / "run", progress=lambda _: None, revisions=0, compute=lambda *_: {"status": "fail", "issues": ["Wrong key"]})
    with pytest.raises(PipelineError):
        process.run("Generate 1 question")
    assert not any(role in {"reviewer", "auditor"} for role, _ in provider.calls)
    assert not (tmp_path / "run" / "questions.pdf").exists()


def test_failed_wording_triggers_generation_and_reverification(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            response = super().ask(role, payload, schema)
            if role == "reviewer" and self.generated["q1"] == 1:
                response.wording.status = "revise"
                response.wording.reason = "Rewrite the ambiguous final question."
            return response
    provider = Provider()
    assert pipeline(corpus, provider, tmp_path / "run").run("Generate 1 question")["status"] == "complete"
    assert [role for role, _ in provider.calls].count("verifier") == 2


def test_auditor_replaces_only_affected_slot(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            response = super().ask(role, payload, schema)
            if role == "auditor" and self.generated["q2"] == 1:
                return audit_result(payload, schema, accepted=False, summary="Change the second question.", issues=[{"dimension": "variety", "reason": "Repeated structure", "slot_ids": ["q2"], "instruction": "Use a different mathematical construction."}])
            return response
    provider = Provider(count=2)
    state = pipeline(corpus, provider, tmp_path / "run").run("Generate 2 questions")
    assert state["status"] == "complete"
    assert provider.generated == {"q1": 1, "q2": 2}


def test_resume_preserves_accepted_questions(corpus, tmp_path):
    class FailingProvider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == "generator" and payload["slot"]["id"] == "q2":
                raise RuntimeError("Temporary provider failure")
            return super().ask(role, payload, schema)
    run_dir = tmp_path / "run"
    with pytest.raises(RuntimeError):
        pipeline(corpus, FailingProvider(count=2), run_dir).run("Generate 2 questions")
    state = json.loads((run_dir / "state.json").read_text())
    assert set(state["items"]) == {"q1"}
    provider = ScriptedProvider(count=2)
    assert pipeline(corpus, provider, run_dir).run("Generate 2 questions", resume=True)["status"] == "complete"
    assert provider.generated == {"q2": 1}
    assert not any(role == "planner" for role, _ in provider.calls)


def test_explicit_request_count_is_enforced(corpus, tmp_path):
    provider = ScriptedProvider(count=1)
    with pytest.raises(PipelineError, match="specifies 2"):
        pipeline(corpus, provider, tmp_path / "run").run("Generate 2 questions")
    assert not provider.generated


def test_completed_run_needs_no_new_calls(corpus, tmp_path):
    run_dir = tmp_path / "run"
    pipeline(corpus, ScriptedProvider(), run_dir).run("Generate 1 question")
    provider = ScriptedProvider()
    state = pipeline(corpus, provider, run_dir).run("Generate 1 question", resume=True)
    assert state["status"] == "complete" and not provider.calls


def test_incomplete_state_cannot_be_exported(tmp_path):
    with pytest.raises(ValueError, match="completed test"):
        export({"status": "needs_attention"}, tmp_path)


def test_parallel_generation_finishes_all_slots(corpus, tmp_path):
    import threading

    class Provider(ScriptedProvider):
        def __init__(self, count):
            super().__init__(count=count)
            self.guard = threading.Lock()

        def ask(self, role, payload, schema):
            with self.guard:
                return super().ask(role, payload, schema)

    provider = Provider(count=3)
    process = Pipeline(corpus, provider, tmp_path / "run", progress=lambda _: None, compute=compute)
    state = process.run("Generate 3 questions")
    assert state["status"] == "complete"
    assert set(state["items"]) == {"q1", "q2", "q3"}
    assert process.effective_workers == 2


def test_auto_workers_is_half_the_test(corpus, tmp_path):
    import threading

    class Provider(ScriptedProvider):
        def __init__(self, count):
            super().__init__(count=count)
            self.guard = threading.Lock()

        def ask(self, role, payload, schema):
            with self.guard:
                return super().ask(role, payload, schema)

    process = Pipeline(corpus, Provider(count=4), tmp_path / "run", progress=lambda _: None, compute=compute)
    assert process.run("Generate 4 questions")["status"] == "complete"
    assert process.effective_workers == 2


def test_word_based_item_is_accepted_via_reviewer(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == "verifier":
                return MathModel(mode="unsupported", interpretation="Best-conclusion item the engine cannot model.", variables=[], equations=[], constraints=[], target="", aggregate="each", options=[], unsupported_reason="Qualitative claim selection.")
            return super().ask(role, payload, schema)

    provider = Provider()
    state = pipeline(corpus, provider, tmp_path / "run").run("Generate 1 question")
    assert state["status"] == "complete"
    assert state["items"]["q1"]["mathematical_model"]["mode"] == "unsupported"
    assert state["metrics"]["review_only"] == 1


@pytest.mark.parametrize("repaired", [True, False])
def test_model_only_review_failure_goes_back_to_blind_verifier(corpus, tmp_path, repaired):
    class Provider(ScriptedProvider):
        verifier_calls = 0
        reviewer_calls = 0

        def ask(self, role, payload, schema):
            result = super().ask(role, payload, schema)
            if role == "verifier":
                self.verifier_calls += 1
                assert "answer" not in payload["question"] and "solution" not in payload["question"]
                assert "review" not in payload and "computation" not in payload
                if self.verifier_calls == 1 or not repaired:
                    # A hardcoded target accidentally matches the key, but does not model the stem.
                    result.mode = "evaluate"
                    result.variables = []
                    result.equations = []
                    result.target = "7"
            if role == "reviewer":
                self.reviewer_calls += 1
                if self.reviewer_calls == 1 or not repaired:
                    result.mathematical_model.status = "revise"
                    result.mathematical_model.reason = "The model hardcodes the keyed value instead of translating the equation."
            return result

    provider = Provider()
    process = Pipeline(corpus, provider, tmp_path / "run", revisions=0, progress=lambda _: None, compute=compute)
    if repaired:
        assert process.run("Generate 1 question")["status"] == "complete"
    else:
        with pytest.raises(PipelineError, match="did not pass"):
            process.run("Generate 1 question")
    assert provider.generated == {"q1": 1}
    assert provider.verifier_calls == 2
    assert provider.reviewer_calls == 2


def test_one_failed_question_does_not_stop_the_rest(corpus, tmp_path):
    from gaussian_prep.verification import compute as real_compute

    def compute(question, model):
        return {"status": "fail", "issues": ["forced"]} if question.slot_id == "q1" else real_compute(question, model)

    provider = ScriptedProvider(count=2)
    process = Pipeline(corpus, provider, tmp_path / "run", progress=lambda _: None, revisions=0, compute=compute)
    with pytest.raises(PipelineError, match="q1"):
        process.run("Generate 2 questions")
    assert any(role == "generator" and payload["slot"]["id"] == "q2" for role, payload in provider.calls)


def test_audit_rejection_becomes_a_warning_after_budget(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == "auditor":
                return audit_result(payload, schema, accepted=False, summary="Variety.", issues=[{"dimension": "variety", "reason": "Repeated template.", "slot_ids": ["q1"], "instruction": "Change the context."}])
            return super().ask(role, payload, schema)

    state = pipeline(corpus, Provider(count=2), tmp_path / "run").run("Generate 2 questions")
    assert state["status"] == "complete"
    assert set(state["items"]) == {"q1", "q2"}
    assert state["audit_warnings"]["accepted"] is False


def test_auditor_cannot_invent_slot_id(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == "auditor":
                return audit_result(payload, schema, accepted=False, summary="Bad ID", issues=[{"dimension": "coverage", "reason": "Problem", "slot_ids": ["q999"], "instruction": "Revise"}])
            return super().ask(role, payload, schema)
    with pytest.raises(PipelineError, match="Unknown"):
        pipeline(corpus, Provider(), tmp_path / "run").run("Generate 1 question")


@pytest.mark.parametrize("count", [2, 3])
def test_provider_failure_does_not_discard_other_slots(corpus, tmp_path, count):
    from gaussian_prep.provider import ProviderError

    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == "generator" and payload["slot"]["id"] == "q1":
                raise ProviderError("Role response was invalid after retries")
            return super().ask(role, payload, schema)

    run_dir = tmp_path / "run"
    with pytest.raises(PipelineError, match="q1"):
        pipeline(corpus, Provider(count=count), run_dir).run(f"Generate {count} questions")
    saved = json.loads((run_dir / "state.json").read_text())
    assert set(saved["items"]) == {f"q{i}" for i in range(2, count + 1)}
    assert "q1" in saved["slot_failures"]
    provider = ScriptedProvider(count=count)
    restored = pipeline(corpus, provider, run_dir).run(f"Generate {count} questions", resume=True)
    assert restored["status"] == "complete"
    assert provider.generated == {"q1": 1}
    assert not restored["slot_failures"]


def test_unexpected_parallel_error_still_saves_finished_workers(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == "generator" and payload["slot"]["id"] == "q1":
                raise RuntimeError("Unexpected failure")
            return super().ask(role, payload, schema)

    run_dir = tmp_path / "run"
    with pytest.raises(RuntimeError, match="Unexpected failure"):
        pipeline(corpus, Provider(count=3), run_dir).run("Generate 3 questions")
    saved = json.loads((run_dir / "state.json").read_text())
    assert set(saved["items"]) == {"q2", "q3"}


def test_parallel_duplicate_is_replaced_before_audit(corpus, tmp_path):
    import threading
    from conftest import question

    class Provider(ScriptedProvider):
        def __init__(self):
            super().__init__(count=3)
            self.barrier = threading.Barrier(2)

        def ask(self, role, payload, schema):
            result = super().ask(role, payload, schema)
            if role == "generator":
                sid = payload["slot"]["id"]
                if sid in {"q1", "q2"} and self.generated[sid] == 1:
                    self.barrier.wait(timeout=5)
                    return question(int(sid[1:]), rhs=19)
            return result

    provider = Provider()
    state = pipeline(corpus, provider, tmp_path / "run").run("Generate 3 questions")
    assert state["status"] == "complete"
    assert len({item["question"]["stem"] for item in state["items"].values()}) == 3
    assert sum(provider.generated.values()) > 3
    assert not state["metrics"]["duplicate_stems"]


def test_audit_replacement_failure_preserves_original_and_resume_budget(corpus, tmp_path):
    from gaussian_prep.provider import ProviderError

    class Provider(ScriptedProvider):
        fail_replacement = True

        def ask(self, role, payload, schema):
            if role == "generator" and payload["slot"]["id"] == "q2" and self.generated.get("q2") and self.fail_replacement:
                raise ProviderError("Temporary failure")
            if role == "auditor":
                return audit_result(payload, schema, accepted=False, summary="Variety", issues=[{"dimension": "variety", "reason": "Repeated context", "slot_ids": ["q2"], "instruction": "Change context"}])
            return super().ask(role, payload, schema)

    run_dir = tmp_path / "run"
    with pytest.raises(PipelineError, match="Replacement questions"):
        pipeline(corpus, Provider(count=2), run_dir).run("Generate 2 questions")
    saved = json.loads((run_dir / "state.json").read_text())
    assert set(saved["items"]) == {"q1", "q2"}
    assert set(saved["pending_audit_feedback"]) == {"q2"}
    assert saved["audit_revision_round"] == 1
    provider = Provider(count=2)
    provider.fail_replacement = False
    restored = pipeline(corpus, provider, run_dir).run("Generate 2 questions", resume=True)
    assert restored["status"] == "complete"
    assert restored["audit_warnings"]
    assert provider.generated == {"q2": 1}
    assert not restored["pending_audit_feedback"]


def test_auditor_gets_a_chance_to_correct_unknown_ids(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == "auditor" and "correction" not in payload:
                return audit_result(payload, schema, accepted=False, summary="Wrong ID", issues=[{"dimension": "coverage", "reason": "Problem", "slot_ids": ["q999"], "instruction": "Revise"}])
            return super().ask(role, payload, schema)

    assert pipeline(corpus, Provider(), tmp_path / "run").run("Generate 1 question")["status"] == "complete"


def test_bad_latex_is_revised_before_verification(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            result = super().ask(role, payload, schema)
            if role == "generator" and self.generated["q1"] == 1:
                result.stem = r"Find \(\frac{1}{2\)."
            return result

    provider = Provider()
    assert pipeline(corpus, provider, tmp_path / "run").run("Generate 1 question")["status"] == "complete"
    assert provider.generated == {"q1": 2}
    assert sum(role == "verifier" for role, _ in provider.calls) == 1


@pytest.mark.parametrize("user_request", [
    "Generate 2 modules of 2 questions", "Generate 2 questions per module in 2 modules",
    "Generate two 2-question modules", "Generate 2 modules, each with 2 questions",
    "Generate 4 questions across 2 modules",
])
def test_per_module_counts_are_not_mistaken_for_test_totals(corpus, user_request):
    from conftest import slot
    from gaussian_prep.models import Plan
    from gaussian_prep.pipeline import check_plan
    slots = [slot(i).model_copy(update={"module": (i - 1) // 2 + 1, "position": (i - 1) % 2 + 1}) for i in range(1, 5)]
    plan = Plan(title="Test", interpretation="Two modules", assumptions=[], requirements=[], total_questions=4, module_count=2, minutes_per_module=[10, 10], slots=slots)
    check_plan(plan, user_request, corpus)
