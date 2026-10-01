"""Local checkpoint recovery before asking for credentials or spending more model calls."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from .models import Audit, Plan
from .pipeline import Pipeline, PipelineError, can_resolve_saved_audit, check_plan, resolve_formatting_audit, restore_audit_work, save_json, test_metrics, update_audit_warnings
from .verification import verify
from .prep import saved_profile


def recover_saved_run(corpus, run_dir: Path, progress=print, compute=verify, backup_dir: Path | None = None) -> dict:
    path = run_dir / "state.json"
    original = path.read_text(encoding="utf-8")
    state = json.loads(original)
    if saved_profile(state) != corpus.profile:
        raise PipelineError("Resume requires the original prep material and generation mode")
    if state["dataset_sha256"] != corpus.sha256:
        raise PipelineError("Resume requires the unchanged source dataset")
    state["dataset"] = str(corpus.path)
    if not state.get("plan"):
        return state
    plan = Plan.model_validate(state["plan"])
    check_plan(plan, state["request"], corpus)
    # This object has no ask method: local recovery cannot make a provider request.
    local = Pipeline(corpus, SimpleNamespace(model=state["model"]), run_dir, progress=progress, compute=compute)
    local.state = state
    state.setdefault("pending_audit_feedback", {})
    restore_audit_work(state)
    local_audit_resolution = can_resolve_saved_audit(state)
    local.refresh_saved_items()
    metrics = test_metrics(plan, state["items"])
    sound = metrics["complete_slot_set"] and not metrics["duplicate_stems"] and not metrics["structure_errors"] and metrics["all_computations_pass"] and metrics["all_item_reviews_pass"]
    if sound and not state["pending_audit_feedback"] and state.get("audit"):
        audit = Audit.model_validate(state["audit"])
        if any(issue.dimension == "structure" for issue in audit.issues):
            state["status"] = "needs_attention"
            state["error"] = "The auditor's exam-structure conflict needs clarification."
            sound = False
        resolutions = []
        if local_audit_resolution:
            audit, resolutions = resolve_formatting_audit(audit, state["items"])
        if sound and local_audit_resolution and (audit.accepted or state.get("status") == "complete"):
            state["audit"] = audit.model_dump()
            state.setdefault("audit_resolutions", []).extend(resolutions)
            state["metrics"] = metrics
            state["status"] = "complete"
            state["audit_work"]["phase"] = "done"
            state.pop("error", None)
            update_audit_warnings(state, audit)
        elif state.get("status") == "complete":
            state["status"] = "needs_attention"
            state["error"] = "A targeted audit is still pending."
    elif state.get("status") == "complete":
        state["status"] = "needs_attention"
        state["error"] = "Saved questions need review after local revalidation; accepted originals remain saved."
    if state != json.loads(original):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup = (backup_dir if backup_dir is not None else run_dir) / f"state-before-recovery-{stamp}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(original, encoding="utf-8")
        state.setdefault("recovery_history", []).append({"time": stamp, "backup": backup.name, "status": state["status"], "model_calls": 0})
        save_json(path, state)
    return state
