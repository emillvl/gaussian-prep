import copy
import json

import pytest

from conftest import ScriptedProvider, audit_result, question
from gaussian_prep.pipeline import Pipeline, PipelineError
from gaussian_prep.provider import ProviderError
from gaussian_prep.recovery import recover_saved_run
from gaussian_prep.verification import compute


ISSUE = dict(dimension="variety", reason="The second and third items repeat a construction.",
             slot_ids=["q2", "q3"], instruction="Revise the two named constructions.")


def process(corpus, provider, path, **kwargs):
    return Pipeline(corpus, provider, path, compute=compute, progress=lambda _: None, **kwargs)


class ReplacementProvider(ScriptedProvider):
    fail_slot = None
    fail_followup = False

    def __init__(self):
        super().__init__(count=3)

    def ask(self, role, payload, schema):
        if role == "generator" and self.generated.get(payload["slot"]["id"]):
            sid = payload["slot"]["id"]
            if sid == self.fail_slot:
                raise ProviderError("Replacement interrupted")
            self.calls.append((role, payload))
            self.generated[sid] += 1
            return question(int(sid[1:]), rhs=101 + 2 * int(sid[1:]))
        if role == "auditor":
            self.calls.append((role, copy.deepcopy(payload)))
            if payload["audit_mode"] == "initial":
                return audit_result(payload, schema, accepted=False, summary="Two replacements", issues=[ISSUE])
            if self.fail_followup:
                raise ProviderError("Follow-up interrupted")
            return audit_result(payload, schema, accepted=True, summary="Both saved findings resolved", issues=[])
        return super().ask(role, payload, schema)


@pytest.mark.parametrize("legacy", [False, True])
def test_resume_remembers_all_targets_after_partial_replacement(corpus, tmp_path, legacy):
    provider = ReplacementProvider()
    provider.fail_slot = "q3"
    with pytest.raises(PipelineError, match="Replacement"):
        process(corpus, provider, tmp_path).run("Generate 3 questions")
    saved = json.loads((tmp_path / "state.json").read_text())
    assert set(saved["pending_audit_feedback"]) == {"q3"}
    if legacy:
        saved.pop("audit_work")
        (tmp_path / "state.json").write_text(json.dumps(saved))
    restored_provider = ScriptedProvider(count=3)
    state = process(corpus, restored_provider, tmp_path).run("Generate 3 questions", resume=True)
    followup = next(payload for role, payload in restored_provider.calls if role == "auditor")
    assert followup["audit_mode"] == "followup"
    assert set(followup["scope_slot_ids"]) == {"q2", "q3"}
    assert {item["slot"]["id"] for item in followup["items"]} == {"q2", "q3"}
    assert [item["slot"]["id"] for item in followup["context_items"]] == ["q1"]
    assert followup["previous_audit"]["issues"][0]["reason"] == ISSUE["reason"]
    assert restored_provider.generated == {"q3": 1}
    assert state["items"]["q1"] == saved["items"]["q1"]
    assert state["items"]["q2"] == saved["items"]["q2"]
    assert state["audit_revision_round"] == 1
    assert state["status"] == "complete"


def test_resume_after_followup_failure_does_not_repeat_replacements(corpus, tmp_path):
    provider = ReplacementProvider()
    provider.fail_followup = True
    with pytest.raises(ProviderError, match="Follow-up"):
        process(corpus, provider, tmp_path).run("Generate 3 questions")
    saved = json.loads((tmp_path / "state.json").read_text())
    assert not saved["pending_audit_feedback"]
    assert saved["audit_work"]["phase"] == "review"
    assert recover_saved_run(corpus, tmp_path, compute=compute)["status"] != "complete"
    resumed = ScriptedProvider(count=3)
    state = process(corpus, resumed, tmp_path).run("Generate 3 questions", resume=True)
    assert [role for role, _ in resumed.calls] == ["auditor"]
    assert resumed.calls[0][1]["scope_slot_ids"] == ["q2", "q3"]
    assert state["items"] == saved["items"]


@pytest.mark.parametrize("accepted", [False, True])
def test_interrupt_after_verdict_consumes_saved_result(corpus, tmp_path, accepted):
    class Interrupted(Pipeline):
        def checkpoint(self):
            super().checkpoint()
            if self.state.get("audit_work", {}).get("phase") == "decide":
                raise KeyboardInterrupt()

    provider = ScriptedProvider(count=3) if accepted else ReplacementProvider()
    with pytest.raises(KeyboardInterrupt):
        Interrupted(corpus, provider, tmp_path, compute=compute, progress=lambda _: None).run("Generate 3 questions")
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["audit_work"]["phase"] == "decide"
    # Use the same replacement behavior without carrying provider-local counters.
    resumed = ReplacementProvider()
    resumed.generated = {"q2": 1, "q3": 1}
    state = process(corpus, resumed, tmp_path).run("Generate 3 questions", resume=True)
    audits = [payload for role, payload in resumed.calls if role == "auditor"]
    assert len(audits) == (0 if accepted else 1)
    assert all(payload["audit_mode"] == "followup" for payload in audits)
    assert state["status"] == "complete"


@pytest.mark.parametrize("defect", ["omit", "duplicate", "expand", "global"])
def test_followup_cannot_skip_or_expand_saved_scope(corpus, tmp_path, defect):
    class BadFollowup(ReplacementProvider):
        def ask(self, role, payload, schema):
            result = super().ask(role, payload, schema)
            if role == "auditor" and payload["audit_mode"] == "followup":
                if defect in {"omit", "duplicate"}:
                    result.checked_slot_ids = ["q2"] if defect == "omit" else ["q2", "q3", "q3"]
                else:
                    issue = dict(ISSUE, slot_ids=["q1"] if defect == "expand" else [])
                    result = audit_result(payload, schema, accepted=False, summary="New unrelated finding", issues=[issue])
            return result

    provider = BadFollowup()
    with pytest.raises(PipelineError, match="invalid scope"):
        process(corpus, provider, tmp_path).run("Generate 3 questions")
    assert len([1 for role, payload in provider.calls if role == "auditor" and payload["audit_mode"] == "followup"]) == 3
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["audit_work"]["phase"] == "review"
    assert saved["audit_work"]["slot_ids"] == ["q2", "q3"]
    assert saved["audit"]["issues"][0]["slot_ids"] == ["q2", "q3"]
    resumed = ScriptedProvider(count=3)
    assert process(corpus, resumed, tmp_path).run("Generate 3 questions", resume=True)["status"] == "complete"
    assert [role for role, _ in resumed.calls] == ["auditor"]


def test_local_recovery_cannot_bypass_pending_followup_of_repaired_item(corpus, tmp_path):
    state = process(corpus, ScriptedProvider(count=3), tmp_path).run("Generate 3 questions")
    state["status"] = "needs_attention"
    state["audit_work"] = dict(phase="review", slot_ids=["q2"], previous_audit=state["audit"])
    (tmp_path / "state.json").write_text(json.dumps(state))
    assert recover_saved_run(corpus, tmp_path, compute=compute)["status"] == "needs_attention"


def test_testwide_findings_explicitly_preserve_testwide_scope(corpus, tmp_path):
    from gaussian_prep.pipeline import restore_audit_work
    state = dict(items={"q1": {}, "q2": {}}, status="needs_attention", audit_revision_round=1,
                 audit=dict(accepted=False, summary="Workload", issues=[dict(ISSUE, slot_ids=[])]), pending_audit_feedback={})
    restore_audit_work(state)
    assert state["audit_work"]["slot_ids"] == ["q1", "q2"]


@pytest.mark.parametrize("prior_scope", [None, ["q2", "q3"]])
def test_repair_after_saved_passing_verdict_requires_new_followup(corpus, tmp_path, prior_scope):
    class Interrupted(Pipeline):
        def checkpoint(self):
            super().checkpoint()
            work = self.state.get("audit_work", {})
            if work.get("phase") == "decide" and self.state["audit"]["accepted"]:
                raise KeyboardInterrupt()

    provider = ScriptedProvider(count=3) if prior_scope is None else ReplacementProvider()
    with pytest.raises(KeyboardInterrupt):
        Interrupted(corpus, provider, tmp_path, compute=compute, progress=lambda _: None).run("Generate 3 questions")
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["audit_work"]["slot_ids"] == prior_scope
    # A saved item now fails revalidation (for example, after a checker upgrade).
    saved["items"]["q1"]["review"]["estimated_difficulty"] = "Hard"
    (tmp_path / "state.json").write_text(json.dumps(saved))
    recover_saved_run(corpus, tmp_path, compute=compute, progress=lambda _: None)

    resumed = ScriptedProvider(count=3)
    state = process(corpus, resumed, tmp_path).run("Generate 3 questions", resume=True)
    audits = [payload for role, payload in resumed.calls if role == "auditor"]
    assert len(audits) == 1
    assert audits[0]["audit_mode"] == "followup"
    assert audits[0]["scope_slot_ids"] == ["q1"]
    assert audits[0]["previous_audit"]["accepted"] is True
    assert resumed.generated == {"q1": 1}
    assert state["items"]["q2"] == saved["items"]["q2"]
    assert state["items"]["q3"] == saved["items"]["q3"]
    assert state["status"] == "complete"


def test_resume_audits_only_question_eight_when_other_questions_passed(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == "auditor":
                if payload["audit_mode"] == "followup":
                    raise ProviderError("Follow-up interrupted")
                return audit_result(payload, schema, accepted=False, summary="Only question 8 needs repair",
                                    issues=[dict(ISSUE, slot_ids=["q8"], instruction="Revise question 8 only")])
            return super().ask(role, payload, schema)

    with pytest.raises(ProviderError, match="Follow-up interrupted"):
        process(corpus, Provider(count=10), tmp_path).run("Generate 10 questions")
    saved = json.loads((tmp_path / "state.json").read_text())
    resumed = ScriptedProvider(count=10)
    state = process(corpus, resumed, tmp_path).run("Generate 10 questions", resume=True)
    assert [role for role, _ in resumed.calls] == ["auditor"]
    audit_payload = resumed.calls[0][1]
    assert audit_payload["scope_slot_ids"] == ["q8"]
    assert [entry["slot"]["id"] for entry in audit_payload["items"]] == ["q8"]
    assert {entry["slot"]["id"] for entry in audit_payload["context_items"]} == {f"q{i}" for i in range(1, 11)} - {"q8"}
    assert state["items"] == saved["items"]
    assert state["status"] == "complete"


@pytest.mark.parametrize("offline_recovery", [False, True])
def test_formatting_resolution_cannot_skip_other_pending_audit_targets(corpus, tmp_path, offline_recovery):
    state = process(corpus, ScriptedProvider(count=3), tmp_path).run("Generate 3 questions")
    formatting_issue = dict(dimension="wording_consistency", repair_kind="formatting",
                            reason="Double-escaped LaTeX delimiters", slot_ids=["q2"], instruction="Fix escaping")
    state["audit"] = dict(accepted=False, summary="Formatting", issues=[formatting_issue])
    state["status"] = "needs_attention"
    # q1 was also replaced after revalidation; both replacements now await their follow-up.
    state["audit_work"] = dict(phase="review", slot_ids=["q1", "q2"], previous_audit=state["audit"])
    (tmp_path / "state.json").write_text(json.dumps(state))
    if offline_recovery:
        recovered = recover_saved_run(corpus, tmp_path, compute=compute, progress=lambda _: None)
        assert recovered["status"] != "complete"
    resumed = ScriptedProvider(count=3)
    process(corpus, resumed, tmp_path).run("Generate 3 questions", resume=True)
    assert [role for role, _ in resumed.calls] == ["auditor"]
    assert resumed.calls[0][1]["scope_slot_ids"] == ["q1", "q2"]


def test_targeted_approval_preserves_unrelated_saved_warnings(corpus, tmp_path):
    state = process(corpus, ScriptedProvider(count=3), tmp_path).run("Generate 3 questions")
    warning = dict(ISSUE, slot_ids=["q3"], repair_kind="content")
    state["audit"] = dict(accepted=False, summary="Unresolved variety", issues=[warning])
    state["audit_warnings"] = copy.deepcopy(state["audit"])
    state["audit_revision_round"] = 1
    state["items"]["q1"]["review"]["estimated_difficulty"] = "Hard"
    (tmp_path / "state.json").write_text(json.dumps(state))
    resumed = ScriptedProvider(count=3)
    result = process(corpus, resumed, tmp_path).run("Generate 3 questions", resume=True)
    audits = [payload for role, payload in resumed.calls if role == "auditor"]
    assert len(audits) == 1 and audits[0]["scope_slot_ids"] == ["q1"]
    assert result["audit_warnings"]["issues"] == [warning]
    recovered = recover_saved_run(corpus, tmp_path, compute=compute, progress=lambda _: None)
    assert recovered["audit_warnings"]["issues"] == [warning]


@pytest.mark.parametrize("audit_revisions", [0, 1])
def test_revalidation_during_saved_rejection_is_in_next_audit(corpus, tmp_path, audit_revisions):
    class Interrupted(Pipeline):
        def checkpoint(self):
            super().checkpoint()
            if self.state.get("audit_work", {}).get("phase") == "decide":
                raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        Interrupted(corpus, ReplacementProvider(), tmp_path, compute=compute, progress=lambda _: None).run("Generate 3 questions")
    saved = json.loads((tmp_path / "state.json").read_text())
    saved["items"]["q1"]["review"]["estimated_difficulty"] = "Hard"
    (tmp_path / "state.json").write_text(json.dumps(saved))
    resumed = ScriptedProvider(count=3)
    result = process(corpus, resumed, tmp_path, audit_revisions=audit_revisions).run("Generate 3 questions", resume=True)
    audits = [payload for role, payload in resumed.calls if role == "auditor"]
    assert len(audits) == 1
    assert audits[0]["audit_mode"] == "followup"
    assert set(audits[0]["scope_slot_ids"]) == ({"q1", "q2", "q3"} if audit_revisions else {"q1"})
    assert resumed.generated["q1"] == 1
    if not audit_revisions:
        assert result["audit_warnings"]["issues"][0]["slot_ids"] == ["q2", "q3"]
        assert resumed.generated == {"q1": 1}


def test_second_interruption_keeps_revalidation_audit_scope(corpus, tmp_path):
    state = process(corpus, ScriptedProvider(count=3), tmp_path).run("Generate 3 questions")
    state["status"] = "needs_attention"
    state["audit"] = dict(accepted=False, summary="Unresolved variety", issues=[ISSUE])
    state["audit_work"] = dict(phase="decide", slot_ids=None, previous_audit=None)
    state["items"]["q1"]["review"]["estimated_difficulty"] = "Hard"
    (tmp_path / "state.json").write_text(json.dumps(state))

    class Interrupted(Pipeline):
        def _store(self, outcome, failures):
            result = super()._store(outcome, failures)
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        Interrupted(corpus, ScriptedProvider(count=3), tmp_path, audit_revisions=0,
                    compute=compute, progress=lambda _: None).run("Generate 3 questions", resume=True)
    recover_saved_run(corpus, tmp_path, compute=compute, progress=lambda _: None)
    resumed = ScriptedProvider(count=3)
    result = process(corpus, resumed, tmp_path, audit_revisions=0).run("Generate 3 questions", resume=True)
    assert [role for role, _ in resumed.calls] == ["auditor"]
    assert resumed.calls[0][1]["scope_slot_ids"] == ["q1"]
    assert result["audit_warnings"]["issues"][0]["slot_ids"] == ["q2", "q3"]
