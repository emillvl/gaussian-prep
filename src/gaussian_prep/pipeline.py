from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import Callable

from .corpus import Corpus, normalized
from .models import Audit, FollowupAudit, ItemReview, MathModel, Plan, Question, Slot
from .provider import ProviderError, concurrency_limit
from .notation import is_formatting_issue, prepare_question
from .render import question_html
from .verification import verify
from .prep import saved_profile
from .structure import check_structure_request, ensure_structure, item_structure_errors, structure_errors, assessment_label


class PipelineError(RuntimeError):
    pass


class RequestConflict(PipelineError):
    """Requires changed instructions or source material, not another model attempt."""


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def check_plan(plan: Plan, request: str, corpus: Corpus):
    if plan.conflicts:
        raise RequestConflict("Request needs clarification: " + "; ".join(plan.conflicts))
    try:
        check_structure_request(plan, request, corpus.profile.name, corpus.exam_structure)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    counts = list(re.finditer(r"\b(\d+)\s*[- ]?\s*(?:questions?|items?)\b", request, flags=re.I))
    if len(counts) == 1:
        match = counts[0]
        before, after = request[:match.start()], request[match.end():]
        per_module = bool(
            re.match(r"\s*(?:(?:modules?|sections?)\b|(?:per|in each|for each)\s+(?:module|section)\b|each\b)", after, re.I)
            or re.search(r"(?:(?:modules?|sections?)\s+(?:of|with|containing)|each\s+(?:with|of|containing|having))\s*$", before, re.I)
        )
        expected = int(match[1]) * (plan.module_count if per_module else 1)
        if expected != plan.total_questions:
            raise PipelineError(f"Plan has {plan.total_questions} questions but the request specifies {expected}")
    modules = re.findall(r"\b(\d+)\s*[- ]?\s*(?:modules?|sections?)\b", request, flags=re.I)
    if len(modules) == 1 and int(modules[0]) != plan.module_count:
        raise PipelineError("Plan does not match the requested module count")
    for slot in plan.slots:
        if corpus.profile.name.startswith("SAT ") and slot.format == "FRQ":
            raise PipelineError("SAT practice supports MCQ and numeric SPR, not written FRQ")
        if corpus.profile.name == "SAT Verbal" and slot.format != "MCQ":
            raise PipelineError("SAT Verbal requires multiple choice questions")
        if slot.subskill not in corpus.taxonomy.get(slot.domain, []):
            raise PipelineError(f"Unknown or unavailable subskill: {slot.domain} / {slot.subskill}")


def item_review(artifact: dict) -> ItemReview:
    """Load a review, tolerating items accepted before the solution dimension existed."""
    review = dict(artifact["review"])
    review.setdefault("solution", {"status": "pass", "reason": "Item accepted before the independent solution check was added."})
    return ItemReview.model_validate(review)


def check_item_format(artifact: dict) -> None:
    question = Question.model_validate(artifact["question"])
    changes = []
    # One-time compatibility repair for checkpoints created by the old writer.
    if artifact.get("formatting_check", {}).get("source_version") != 2:
        question, changes = prepare_question(question)
    question_html(question, artifact["slot"]["position"])
    artifact["question"] = question.model_dump()
    if changes:
        artifact.setdefault("formatting_repairs", []).extend(changes)
    artifact["formatting_check"] = {"status": "pass", "source_version": 2}


def resolve_formatting_audit(audit: Audit, items: dict) -> tuple[Audit, list[dict]]:
    remaining, resolved = [], []
    for issue in audit.issues:
        targets = issue.slot_ids or list(items)
        if is_formatting_issue(issue.model_dump(exclude_unset=True)) and all(sid in items for sid in targets):
            try:
                for sid in targets:
                    check_item_format(items[sid])
            except ValueError:
                remaining.append(issue)
            else:
                resolved.append({"issue": issue.model_dump(), "resolution": "Decoded question text normalized and passed local typesetting checks; mathematical content retained."})
        else:
            remaining.append(issue)
    if not resolved:
        return audit, []
    return Audit(accepted=not remaining, summary="Local typesetting checks resolved formatting findings. " + audit.summary, issues=remaining), resolved


def test_metrics(plan: Plan, items: dict) -> dict:
    questions = [Question.model_validate(v["question"]) for v in items.values()]
    stems = Counter(normalized(q.stem) for q in questions)
    ordered = sorted(plan.slots, key=lambda s: (s.module, s.position))
    assignments = {slot.id: slot for slot in ordered}
    return {
        "expected_questions": plan.total_questions, "actual_questions": len(items),
        "complete_slot_set": set(items) == set(assignments) and all(v["question"]["slot_id"] == sid and v["question"]["format"] == assignments[sid].format for sid, v in items.items()),
        "duplicate_stems": [stem for stem, count in stems.items() if count > 1],
        "domains": dict(Counter(s.domain for s in ordered)),
        "formats": dict(Counter(q.format for q in questions)),
        "structure_errors": structure_errors(plan, items),
        "modules": [{"module": m, "count": sum(s.module == m for s in ordered), "minutes": plan.minutes_per_module[m - 1], "format_sequence": [s.format for s in ordered if s.module == m], "difficulty_sequence": [s.difficulty for s in ordered if s.module == m]} for m in range(1, plan.module_count + 1)],
        "all_computations_pass": all(v["verification"]["status"] == "pass" or (v["verification"]["status"] == "unverified" and v["mathematical_model"]["mode"] == "unsupported") for v in items.values()),
        "computationally_verified": sum(v["verification"]["status"] == "pass" for v in items.values()),
        "review_only": sum(v["verification"]["status"] != "pass" and v["mathematical_model"]["mode"] == "unsupported" for v in items.values()),
        "all_item_reviews_pass": all(sid in assignments and item_review(v).accepted() and item_review(v).estimated_difficulty == assignments[sid].difficulty for sid, v in items.items()),
    }


def audit_targets(audit: Audit, items: dict) -> list[str]:
    targets = {sid for issue in audit.issues for sid in issue.slot_ids}
    return list(items) if any(not issue.slot_ids for issue in audit.issues) else sorted(targets)


def restore_audit_work(state: dict) -> None:
    """Migrate older checkpoints once; never infer scope from a cleared feedback queue."""
    if state.get("audit_work"):
        return
    state.setdefault("pending_audit_feedback", {})
    audit = Audit.model_validate(state["audit"]) if state.get("audit") else None
    work = {"phase": "review", "slot_ids": None, "previous_audit": None}
    if audit:
        if audit.accepted or state.get("status") == "complete":
            work["phase"] = "done"
        elif state["pending_audit_feedback"] or state.get("audit_revision_round", 0):
            work = {"phase": "replace", "slot_ids": audit_targets(audit, state["items"]), "previous_audit": audit.model_dump()}
            state["audit_revision_round"] = max(1, state.get("audit_revision_round", 0))
        else:
            # The previous process saved the verdict but stopped before scheduling repairs.
            work["phase"] = "decide"
    state["audit_work"] = work


def can_resolve_saved_audit(state: dict) -> bool:
    """Local syntax checks cannot stand in for a scheduled audit of replacements."""
    work = state["audit_work"]
    if work.get("revalidated_slot_ids"):
        return False
    if work["phase"] not in {"replace", "review"} or work["slot_ids"] is None:
        return True
    pending = state["pending_audit_feedback"]
    # A legacy formatting repair may finish locally before any item is replaced.
    # Check this before refresh_saved_items clears locally resolved feedback.
    return work["phase"] == "replace" and bool(work["slot_ids"]) and set(pending) == set(work["slot_ids"]) and all(
        findings and all(isinstance(f, dict) and is_formatting_issue(f) for f in findings)
        for findings in pending.values()
    )


def update_audit_warnings(state: dict, audit: Audit) -> None:
    """A scoped verdict resolves only warnings within that same scope."""
    issues = list(audit.issues)
    scope = state["audit_work"]["slot_ids"]
    if scope is not None and state.get("audit_warnings"):
        for issue in Audit.model_validate(state["audit_warnings"]).issues:
            remaining = [sid for sid in (issue.slot_ids or list(state["items"])) if sid not in scope]
            if remaining:
                retained = issue.model_copy(update={"slot_ids": remaining})
                if retained not in issues:
                    issues.append(retained)
    if issues:
        state["audit_warnings"] = Audit(accepted=False, summary="Unresolved audit findings. " + audit.summary, issues=issues).model_dump()
    else:
        state.pop("audit_warnings", None)


class Pipeline:
    def __init__(self, corpus: Corpus, provider, run_dir: Path, progress: Callable[[str], None] = print, revisions: int = 2, audit_revisions: int = 1, compute: Callable = verify, request_guide: str = "", diagnostics_dir: Path | None = None):
        if revisions < 0 or audit_revisions < 0:
            raise ValueError("Revision limits cannot be negative")
        self.corpus, self.provider, self.run_dir = corpus, provider, run_dir
        self.diagnostics_dir = diagnostics_dir if diagnostics_dir is not None else run_dir
        self.progress, self.revisions, self.audit_revisions = progress, revisions, audit_revisions
        self.compute, self.request_guide = compute, request_guide
        self.effective_workers = 1
        self.numbering: dict[str, int] = {}
        self.state = {}
        self._lock = threading.Lock()
        self._progress_lock = threading.Lock()

    def ask(self, role, payload, schema):
        structure = (self.state.get("plan") or {}).get("structure")
        return self.provider.ask(role, {**payload, "prep_profile": self.corpus.profile.to_dict(), "assessment_structure": structure}, schema)

    def source_failures(self, slot: Slot, question: Question) -> list[str]:
        if not self.corpus.profile.source_based:
            return []
        if slot.source_index is None:
            return ["A source-based question must have an assigned dataset record"]
        source = self.corpus.source(slot.source_index)
        failures = []
        if question.stem != source.question or question.model_dump()["figure"] != source.figure or question.model_dump()["table"] != source.table:
            failures.append("Keep the assigned source passage, question, table, and figure unchanged; only adapt choices and the answer/solution")
        if question.format != source.format or (question.format == "MCQ" and len(question.choices) != len(source.choices)):
            failures.append("Preserve the assigned source format and number of choices")
        if question.format == "MCQ":
            original = [normalized(re.sub(r"^\s*[A-Ha-h][.):]\s*", "", text)) for text in source.choices]
            adapted = [normalized(c.text) for c in sorted(question.choices, key=lambda c: c.label)]
            if original == adapted:
                failures.append("Adapt the existing question by reordering or rewording answer choices; recheck the key")
        return failures

    def check_content_evidence(self, question: Question, model: MathModel, verification: dict) -> dict:
        if self.corpus.profile.name == "SAT Math" or model.mode != "unsupported":
            return verification
        verification = dict(verification, issues=list(verification.get("issues", [])))
        if not model.independent_answer.strip() or not model.evidence.strip():
            verification["status"] = "fail"
            verification["issues"].append("Independent verifier must solve this subject question and provide supporting evidence")
        elif question.format == "MCQ" and model.independent_answer.strip().upper() != question.answer:
            verification["status"] = "fail"
            verification["issues"].append("Independent verifier's answer disagrees with the generated key")
        verification["independent_answer"] = model.independent_answer
        verification["subject_evidence"] = model.evidence
        return verification

    def say(self, message: str):
        with self._progress_lock:
            self.progress(message)

    def checkpoint(self):
        with self._lock:
            save_json(self.run_dir / "state.json", self.state)

    def existing_stems(self, exclude_slot: str | None = None) -> list[str]:
        with self._lock:
            return [v["question"]["stem"] for sid, v in self.state["items"].items() if sid != exclude_slot]

    def _generate_one(self, slot: Slot, plan: Plan, request: str) -> dict:
        with self._lock:
            feedback = list(self.state.get("pending_audit_feedback", {}).get(slot.id, []))
        return self.make_item(slot, plan, request, feedback)

    def _work(self, slot: Slot, plan: Plan, request: str):
        try:
            return slot, self._generate_one(slot, plan, request), None
        except (PipelineError, ProviderError) as exc:
            return slot, None, exc

    def generate_slots(self, slots: list[Slot], plan: Plan, request: str) -> dict:
        """Generate every slot, returning {slot_id: error} for the ones that could not pass."""
        failures: dict[str, str] = {}
        duplicates: list[Slot] = []
        unexpected = None
        if self.effective_workers > 1 and len(slots) > 1:
            with ThreadPoolExecutor(max_workers=self.effective_workers) as pool:
                futures = [pool.submit(self._work, slot, plan, request) for slot in slots]
                for future in as_completed(futures):
                    try:
                        duplicate = self._store(future.result(), failures)
                        if duplicate:
                            duplicates.append(duplicate)
                    except Exception as exc:
                        # Drain completed workers so a failure cannot discard their accepted output.
                        if unexpected is None:
                            unexpected = exc
        else:
            for slot in slots:
                duplicate = self._store(self._work(slot, plan, request), failures)
                if duplicate:
                    duplicates.append(duplicate)
        if unexpected is not None:
            raise unexpected
        # Workers can start with the same snapshot. Resolve collisions sequentially with fresh context.
        for slot in duplicates:
            self.say(f"[{self.numbering.get(slot.id, slot.position)}] Duplicate detected; generating a replacement...")
            self._store(self._work(slot, plan, request), failures)
        return failures

    def _store(self, outcome, failures: dict):
        slot, artifact, error = outcome
        with self._lock:
            duplicate = False
            if artifact is not None:
                stem = normalized(artifact["question"]["stem"])
                duplicate = any(sid != slot.id and normalized(item["question"]["stem"]) == stem for sid, item in self.state["items"].items())
                if duplicate:
                    error = PipelineError("Question duplicates another accepted question; use a different construction")
                    self.state["pending_audit_feedback"].setdefault(slot.id, []).append(str(error))
            if error is not None:
                failures[slot.id] = str(error)
                self.state.setdefault("slot_failures", {})[slot.id] = str(error)
            else:
                self.state["items"][slot.id] = artifact
                self.state["pending_audit_feedback"].pop(slot.id, None)
                self.state.setdefault("slot_failures", {}).pop(slot.id, None)
                failures.pop(slot.id, None)
            save_json(self.run_dir / "state.json", self.state)
            return slot if duplicate else None

    def refresh_saved_items(self):
        """Migrate saved questions without discarding accepted work or trusting an old checker."""
        plan = Plan.model_validate(self.state['plan'])
        assignments = {slot.id: slot for slot in plan.slots}
        for sid, artifact in self.state["items"].items():
            feedback = list(self.state["pending_audit_feedback"].get(sid, []))

            def remember(finding):
                if finding not in feedback:
                    feedback.append(finding)
                self.state["pending_audit_feedback"][sid] = feedback

            try:
                check_item_format(artifact)
            except ValueError as exc:
                remember({"formatting_error": str(exc), "instruction": "Repair the malformed notation in previous_draft; preserve the question and answer unless other saved feedback requires a content change."})
                continue
            if self.corpus.profile.source_based and sid in assignments:
                for failure in self.source_failures(assignments[sid], Question.model_validate(artifact["question"])):
                    remember(failure)
            if sid in assignments:
                for failure in item_structure_errors(plan, assignments[sid], Question.model_validate(artifact["question"])):
                    remember({"instruction": failure})
            if artifact["mathematical_model"]["mode"] != "unsupported" and artifact["verification"].get("verification_version") != 2:
                self.say(f"Rechecking saved question {sid} with the corrected math checker...")
                artifact["verification"] = self.compute(Question.model_validate(artifact["question"]), MathModel.model_validate(artifact["mathematical_model"]))
            artifact["verification"] = self.check_content_evidence(Question.model_validate(artifact["question"]), MathModel.model_validate(artifact["mathematical_model"]), artifact["verification"])
            valid = artifact["verification"]["status"] == "pass" or (artifact["verification"]["status"] == "unverified" and artifact["mathematical_model"]["mode"] == "unsupported")
            if not valid:
                remember({"computation": artifact["verification"], "instruction": "The updated checker could not verify this saved item. Repair the model or question and verify again."})
            assigned = assignments.get(sid)
            review = item_review(artifact)
            consistent = assigned is not None and artifact['question']['slot_id'] == sid and artifact['question']['format'] == assigned.format and review.accepted() and review.estimated_difficulty == assigned.difficulty
            if not consistent:
                remember({"instruction": "The saved item no longer passes its assigned format, slot, difficulty, or review checks. Repair it for the original slot and repeat verification and review."})
            if valid and consistent:
                feedback = [f for f in feedback if not (isinstance(f, dict) and is_formatting_issue(f))]
            if feedback:
                self.state["pending_audit_feedback"][sid] = feedback
            else:
                self.state["pending_audit_feedback"].pop(sid, None)
                self.state.get("slot_failures", {}).pop(sid, None)

    def run(self, request: str, resume: bool = False) -> dict:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.run_dir / "state.json"
        if resume:
            if not state_path.is_file():
                raise PipelineError("No saved run at this path")
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
            if saved_profile(self.state) != self.corpus.profile:
                raise PipelineError("Resume requires the original prep material and generation mode")
            if self.state["request"] != request or self.state["dataset_sha256"] != self.corpus.sha256:
                raise PipelineError("Resume requires the same request and unchanged source dataset")
            if self.state["model"] != self.provider.model:
                raise PipelineError("Resume requires the original shared model")
            if self.state.get("provider", "deepseek") != getattr(self.provider, "name", "deepseek"):
                raise PipelineError("Resume requires the original provider; start a new run to switch")
            # Failed runs can retry their unresolved slots; accepted slots are reused.
        else:
            if state_path.exists():
                raise PipelineError("Run already exists. Use resume or choose a new output directory")
            self.state = {"version": 1, "status": "running", "request": request, "dataset": str(self.corpus.path), "dataset_sha256": self.corpus.sha256, "model": self.provider.model, "created_at": datetime.now(timezone.utc).isoformat(), "plan": None, "items": {}, "audit": None, "pending_audit_feedback": {}}
            self.state["provider"] = getattr(self.provider, "name", "deepseek")
            self.state["prep_profile"] = self.corpus.profile.to_dict()
            self.state["dataset_exam_structure"] = self.corpus.exam_structure.model_dump() if self.corpus.exam_structure else None
        restore_audit_work(self.state)
        self.state["status"] = "running"
        self.state.pop("error", None)
        self.checkpoint()
        try:
            if not self.state["plan"]:
                self.progress("Planner: turning your request into a test blueprint...")
                correction = ""
                for attempt in range(3):
                    plan = self.ask("planner", {"request": request, "available_taxonomy": self.corpus.taxonomy, "source_inventory": self.corpus.inventory(), "source_style_examples": self.corpus.style_examples(), "dataset_exam_structure": self.corpus.exam_structure.model_dump() if self.corpus.exam_structure else None, "correction": correction, "request_guide": self.request_guide}, Plan)
                    save_json(self.diagnostics_dir / f"plan-attempt-{attempt + 1}.json", plan.model_dump())
                    if plan.conflicts:
                        raise RequestConflict("Request needs clarification: " + "; ".join(plan.conflicts))
                    try:
                        ensure_structure(plan, request)
                        check_plan(plan, request, self.corpus)
                        if self.corpus.profile.source_based:
                            self.corpus.assign_sources(plan)
                        break
                    except (PipelineError, ValueError) as exc:
                        correction = str(exc)
                else:
                    raise PipelineError(correction)
                self.state["plan"] = plan.model_dump()
                self.checkpoint()
            plan = Plan.model_validate(self.state["plan"])
            check_plan(plan, request, self.corpus)
            limit = concurrency_limit(getattr(self.provider, "name", ""), self.provider.model)
            self.effective_workers = min(max(1, (plan.total_questions + 1) // 2), limit)
            self.say(f"Plan: {plan.total_questions} questions, {plan.module_count} module(s), {self.effective_workers} parallel worker(s), provider limit {limit}. {plan.interpretation}")
            if plan.structure:
                self.say(f"{assessment_label(plan)}: {plan.structure.target}. {plan.structure.basis}")
                for section in plan.structure.sections:
                    mix = ", ".join(f"{count} {fmt}" for fmt, count in Counter(section.format_sequence).items())
                    self.say(f"  {section.name}: {len(section.format_sequence)} questions, {section.minutes} minutes; {mix}.")
            for assumption in plan.assumptions:
                self.say(f"Assumption: {assumption}")
            ordered = sorted(plan.slots, key=lambda s: (s.module, s.position))
            self.numbering = {slot.id: index for index, slot in enumerate(ordered, 1)}
            local_audit_resolution = can_resolve_saved_audit(self.state)
            self.refresh_saved_items()
            # Resume older runs stuck at the final duplicate check by replacing only repeated slots.
            seen = set()
            for slot in ordered:
                if slot.id in self.state["items"]:
                    stem = normalized(self.state["items"][slot.id]["question"]["stem"])
                    if stem in seen:
                        self.state["pending_audit_feedback"].setdefault(slot.id, []).append("Question duplicates another accepted question; use a different construction")
                    seen.add(stem)
            pending = [s for s in ordered if s.id not in self.state["items"] or s.id in self.state["pending_audit_feedback"]]
            work = self.state["audit_work"]
            saved_approval = work["phase"] == "decide" and self.state.get("audit", {}).get("accepted", False)
            if pending and (work["phase"] == "done" or saved_approval):
                # Local revalidation can discover a concrete defect in an older accepted item.
                # Even a saved passing verdict applies only to the pre-repair questions.
                work.update(phase="replace", slot_ids=[s.id for s in pending], previous_audit=self.state["audit"])
            elif pending and work["phase"] == "decide":
                # Keep newly repaired items in the next audit even while consuming a saved rejection.
                work["revalidated_slot_ids"] = sorted(set(work.get("revalidated_slot_ids", [])) | {s.id for s in pending})
            elif pending and work["slot_ids"] is not None:
                work["slot_ids"] = sorted(set(work["slot_ids"]) | {s.id for s in pending})
            if not pending and self.state.get("audit") and local_audit_resolution:
                audit, resolutions = resolve_formatting_audit(Audit.model_validate(self.state["audit"]), self.state["items"])
                if resolutions:
                    self.state["audit"] = audit.model_dump()
                    self.state.setdefault("audit_resolutions", []).extend(resolutions)
                    if audit.accepted:
                        work["phase"] = "decide"
            self.checkpoint()
            failures = self.generate_slots(pending, plan, request)
            if failures:
                listed = "; ".join(f"{slot_id}: {reason}" for slot_id, reason in failures.items())
                raise PipelineError(f"These questions did not pass after revisions: {listed}. Findings are saved; resume to retry them.")
            if work["phase"] == "replace":
                work["phase"] = "review"
                self.checkpoint()
            while True:
                metrics = test_metrics(plan, self.state["items"])
                if metrics["structure_errors"]:
                    raise PipelineError("Assembled test failed structure checks: " + "; ".join(metrics["structure_errors"]))
                if not metrics["complete_slot_set"] or metrics["duplicate_stems"] or not metrics["all_computations_pass"] or not metrics["all_item_reviews_pass"]:
                    raise PipelineError("Assembled test failed a deterministic integrity check")
                if work["phase"] == "review":
                    targets = work["slot_ids"]
                    self.say("Test auditor: checking the complete test..." if targets is None else f"Test auditor: following up on questions {', '.join(str(self.numbering[sid]) for sid in targets)}...")
                    entries = [{"slot": s.model_dump(), "question": self.state["items"][s.id]["question"], "review": self.state["items"][s.id]["review"]} for s in ordered]
                    audit_payload = {"request": request, "plan": plan.model_dump(), "computed_metrics": metrics, "audit_mode": "initial" if targets is None else "followup", "items": [item for item in entries if targets is None or item["slot"]["id"] in targets]}
                    if targets is not None:
                        audit_payload.update(scope_slot_ids=targets, previous_audit=work["previous_audit"], context_items=[item for item in entries if item["slot"]["id"] not in targets])
                    for _ in range(3):
                        result = self.ask("auditor", audit_payload, Audit if targets is None else FollowupAudit)
                        unknown = {sid for issue in result.issues for sid in issue.slot_ids} - set(self.state["items"])
                        if unknown:
                            correction = f"Unknown question IDs: {sorted(unknown)}. Use only these slot IDs: {sorted(self.state['items'])}."
                        elif targets is not None and (set(getattr(result, "checked_slot_ids", [])) != set(targets) or len(result.checked_slot_ids) != len(targets)):
                            correction = f"Review EVERY assigned question. checked_slot_ids must contain exactly {targets}, each once, even when an issue is resolved."
                        elif targets is not None and any(not issue.slot_ids or not set(issue.slot_ids) <= set(targets) for issue in result.issues):
                            correction = f"Follow-up findings must name specific IDs within {targets}. Other questions are read-only context; do not reopen their audit."
                        else:
                            break
                        audit_payload["correction"] = correction
                    else:
                        raise PipelineError(f"Auditor returned invalid scope after three attempts: {correction}")
                    raw_audit = result.model_dump()
                    audit = Audit.model_validate({key: raw_audit[key] for key in Audit.model_fields})
                    audit, resolutions = resolve_formatting_audit(audit, self.state["items"])
                    self.state.setdefault("audit_resolutions", []).extend(resolutions)
                    self.state["audit"] = audit.model_dump()
                    work["phase"] = "decide"
                    # A saved verdict is consumed on resume, never replaced by a fresh whole-test audit.
                    self.checkpoint()
                    save_json(self.diagnostics_dir / f"audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.json", {"audit": raw_audit, "effective_audit": audit.model_dump(), "local_resolutions": resolutions, "metrics": metrics, "scope_slot_ids": targets, "previous_audit": work["previous_audit"]})
                audit = Audit.model_validate(self.state["audit"])
                structural_conflicts = [issue for issue in audit.issues if issue.dimension == "structure"]
                if structural_conflicts:
                    raise RequestConflict("Exam structure needs clarification: " + "; ".join(f"{issue.reason} {issue.instruction}" for issue in structural_conflicts))
                if audit.accepted or work["phase"] == "done":
                    update_audit_warnings(self.state, audit)
                    work["phase"] = "done"
                    self.state["status"] = "complete"
                    self.state["metrics"] = metrics
                    self.checkpoint()
                    return self.state
                affected = audit_targets(audit, self.state["items"])
                audit_attempt = self.state.get("audit_revision_round", 0)
                if audit_attempt >= self.audit_revisions:
                    if work.get("revalidated_slot_ids"):
                        # The old findings remain warnings, but changed questions still need an audit.
                        update_audit_warnings(self.state, audit)
                        changed = work.pop("revalidated_slot_ids")
                        work.update(phase="review", slot_ids=changed, previous_audit=audit.model_dump())
                        self.checkpoint()
                        continue
                    # Per-item checks still pass; exhausted audit revisions leave explicit warnings.
                    self.state["status"] = "complete"
                    self.state["metrics"] = metrics
                    update_audit_warnings(self.state, audit)
                    work["phase"] = "done"
                    self.checkpoint()
                    self.say("Test auditor had unresolved suggestions; exporting the verified test with warnings.")
                    return self.state
                feedback_map = {sid: [issue.model_dump() for issue in audit.issues if not issue.slot_ids or sid in issue.slot_ids] for sid in affected}
                # Keep accepted originals until a replacement passes. Pending feedback marks slots
                # that must still be retried, including after a restart.
                self.state["pending_audit_feedback"] = feedback_map
                self.state["audit_revision_round"] = audit_attempt + 1
                scope = sorted(set(affected) | set(work.pop("revalidated_slot_ids", [])))
                work.update(phase="replace", slot_ids=scope, previous_audit=audit.model_dump())
                self.checkpoint()
                replacements = [slot for slot in ordered if slot.id in affected]
                replacement_failures = self.generate_slots(replacements, plan, request)
                if replacement_failures:
                    listed = "; ".join(f"{slot_id}: {reason}" for slot_id, reason in replacement_failures.items())
                    raise PipelineError(f"Replacement questions did not pass after revisions: {listed}. Findings are saved; resume to retry them.")
                work["phase"] = "review"
                self.checkpoint()
        except Exception as exc:
            self.state["status"] = "needs_attention"
            self.state["error"] = str(exc)
            self.checkpoint()
            raise
        raise PipelineError("Run ended without an accepted audit")

    def make_item(self, slot: Slot, plan: Plan, request: str, initial_feedback: list) -> dict:
        references = self.corpus.retrieve(slot)
        feedback = list(initial_feedback)
        with self._lock:
            previous = self.state["items"].get(slot.id, {}).get("question")
        for attempt in range(self.revisions + 1):
            # Keep the auditor's original instructions visible through notation/model repairs.
            feedback = list(initial_feedback) + [finding for finding in feedback if finding not in initial_feedback]
            tag = self.numbering.get(slot.id, slot.position)
            self.say(f"[{tag}] generating" + (f" (revision {attempt})" if attempt else "") + "...")
            artifact = {"slot": slot.model_dump(), "reference_ids": [r.question_id for r in references], "feedback": feedback}
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            filename = self.diagnostics_dir / "items" / f"m{slot.module}-q{slot.position}-{stamp}.json"
            existing = self.existing_stems(exclude_slot=slot.id)
            context = {"request": request, "slot": slot.model_dump(), "test_requirements": plan.requirements, "reference_examples": [r.prompt_data() for r in references], "revision_feedback": feedback, "previous_draft": previous, "existing_questions": existing}
            if self.corpus.profile.source_based:
                context["assigned_source"] = self.corpus.source(slot.source_index).prompt_data()
            try:
                question = self.ask("generator", context, Question)
                artifact["question"] = question.model_dump()
                previous = question.model_dump()
                structural = self.source_failures(slot, question) + item_structure_errors(plan, slot, question)
                if self.corpus.profile.name.startswith("SAT ") and question.format == "MCQ" and len(question.choices) != 4:
                    structural.append("SAT questions require exactly four choices")
                if question.slot_id != slot.id or question.format != slot.format:
                    structural.append("Generated slot_id/format must match the assigned slot")
                similarity = self.corpus.similarity(question.stem, references)
                artifact["similarity"] = similarity
                if similarity["exact_source_copy"] and not self.corpus.profile.source_based:
                    structural.append("Question copies a source question; create a new construction")
                if any(normalized(question.stem) == normalized(stem) for stem in existing):
                    structural.append("Question duplicates another question in this test")
                if structural:
                    feedback = structural
                    artifact["failures"] = structural
                    continue
                try:
                    question_html(question, slot.position)
                except ValueError as exc:
                    feedback = [{"formatting_error": str(exc), "instruction": "Repair malformed notation in previous_draft. Keep the same mathematical content, values, choices, and answer unless other saved feedback requires a content change."}]
                    artifact["failures"] = feedback
                    continue
                artifact["formatting_check"] = {"status": "pass", "source_version": 2}
                self.say(f"[{tag}] Verifier: independently checking the answer and evidence...")
                mathematical_model = self.ask("verifier", {"question": question.student_view()}, MathModel)
                artifact["mathematical_model"] = mathematical_model.model_dump()
                verification = self.compute(question, mathematical_model)
                if mathematical_model.mode != "unsupported" and verification["status"] == "unverified":
                    artifact["model_repair"] = {"previous_model": mathematical_model.model_dump(), "previous_verification": verification}
                    # The verifier remains blind to the author's answer, solution, and key comparisons.
                    self.say(f"[{tag}] Verifier: repairing the mathematical model without rewriting the question...")
                    model_errors = [issue for issue in verification.get("issues", []) if not issue.startswith(("Expected exactly key", "Generated answer"))]
                    mathematical_model = self.ask("verifier", {"question": question.student_view(), "previous_model": mathematical_model.model_dump(), "model_errors": model_errors, "instruction": "Repair your model for the SAME displayed question. Identity mode requires empty equations and aggregate each. Use unsupported only if the task cannot be faithfully modeled."}, MathModel)
                    verification = self.compute(question, mathematical_model)
                    artifact["mathematical_model"] = mathematical_model.model_dump()
                verification = self.check_content_evidence(question, mathematical_model, verification)
                artifact["verification"] = verification
                # Word-based items the engine cannot model (e.g. "best conclusion") go to the reviewer's
                # independent solution check instead of being forced into a computable form.
                engine_supported = mathematical_model.mode != "unsupported"
                if verification["status"] == "fail" or (engine_supported and verification["status"] != "pass"):
                    feedback = [{"computation": verification, "independent_model": mathematical_model.model_dump(), "instruction": "Correct the question or its computation model by making all givens/targets unambiguous; never merely claim a pass. For an SPR item the answer field must be plain engine notation (for example 4/15, sqrt(2), or 2**3), not LaTeX and not a percent."}]
                    continue
                self.say(f"[{tag}] Reviewer: checking {self.corpus.profile.name} wording, difficulty, and question quality...")
                review_context = {"request": request, "slot": slot.model_dump(), "references": [r.prompt_data() for r in references], "question": question.model_dump(), "independent_model": mathematical_model.model_dump(), "engine_supported": engine_supported, "computation": verification, "similarity": similarity}
                review = self.ask("reviewer", review_context, ItemReview)
                artifact["review"] = review.model_dump()
                failed_dimensions = [name for name in ItemReview.model_fields if name != "estimated_difficulty" and getattr(review, name).status != "pass"]
                if failed_dimensions == ["mathematical_model"] and review.estimated_difficulty == slot.difficulty:
                    artifact["semantic_model_repair"] = {"previous_model": mathematical_model.model_dump(), "previous_verification": verification, "previous_review": review.model_dump()}
                    self.say(f"[{tag}] Verifier: rechecking the translation of the unchanged question...")
                    # Review prose and computed comparisons can reveal the key. Keep this retry blind.
                    mathematical_model = self.ask("verifier", {"question": question.student_view(), "previous_model": mathematical_model.model_dump(), "instruction": "A semantic review found that the previous model does not faithfully translate the displayed question. Independently re-derive the model for the SAME question, checking every given, target, unit, domain restriction, and choice. Do not hardcode an inferred answer. Use unsupported only for a justified representational limitation."}, MathModel)
                    verification = self.check_content_evidence(question, mathematical_model, self.compute(question, mathematical_model))
                    artifact["mathematical_model"] = mathematical_model.model_dump()
                    artifact["verification"] = verification
                    engine_supported = mathematical_model.mode != "unsupported"
                    if verification["status"] == "fail" or (engine_supported and verification["status"] != "pass"):
                        feedback = [{"computation": verification, "independent_model": mathematical_model.model_dump(), "instruction": "Independent model repair did not verify the question. Correct the question or answer as needed, making all givens and the target unambiguous; never merely claim a pass."}]
                        continue
                    review_context = {**review_context, "independent_model": mathematical_model.model_dump(), "engine_supported": engine_supported, "computation": verification}
                    review = self.ask("reviewer", review_context, ItemReview)
                    artifact["review"] = review.model_dump()
                if not review.accepted() or review.estimated_difficulty != slot.difficulty:
                    feedback = [{"item_review": review.model_dump(), "requested_difficulty": slot.difficulty, "instruction": "Repair every dimension marked revise or reject. If the difficulty is off, raise or lower the reasoning burden to match the requested level while keeping the same objective skill; do not merely relabel it and do not abandon the objective. Keep the stem unambiguous."}]
                    continue
                self.say(f"[{tag}] Accepted.")
                return artifact
            finally:
                save_json(filename, artifact)
        raise PipelineError(f"Module {slot.module}, question {slot.position} did not pass after {self.revisions + 1} attempts. Findings are saved in {self.diagnostics_dir / 'items'}")
