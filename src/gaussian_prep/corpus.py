from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import csv
from difflib import SequenceMatcher
import hashlib
import json
import io
from pathlib import Path
import re

from .models import AssessmentStructure, Slot
from .prep import PrepProfile

FORMATS = {"multiple choice": "MCQ", "mcq": "MCQ", "student-produced response": "SPR", "spr": "SPR", "frq": "FRQ", "free response": "FRQ", "free-response": "FRQ", "short answer": "FRQ", "essay": "FRQ"}
CALCULUS = re.compile(r"\b(?:derivative|integral|antiderivative|differentiat\w*|integrat\w*|local maximum|local minimum|tangent line to the graph|lim\s*[a-z])\b|∫", re.I)
MISSING_VISUAL = re.compile(r"\b(?:figure|graph|diagram|table|scatterplot|histogram)\s+(?:shown|above|below)|\b(?:in|of)\s+the\s+(?:figure|graph|diagram|table)\s+(?:shown|above|below)", re.I)
BROKEN_ENCODING = re.compile(r"�|Ã.|â€|Â(?:\s|[£°])")


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def read_records(path: Path, raw: bytes | None = None) -> list:
    text = (raw if raw is not None else path.read_bytes()).decode("utf-8-sig")
    if path.suffix.lower() == ".csv":
        rows = list(csv.DictReader(io.StringIO(text)))
    elif path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = rows.get("questions", rows.get("records"))
    if not isinstance(rows, list):
        raise ValueError("Dataset must contain question records (JSON array, questions/records object, JSONL, or CSV).")
    return rows


@dataclass(frozen=True)
class Reference:
    question_id: str
    domain: str
    subskill: str
    difficulty: str
    format: str
    question: str
    choices: list[str]
    answer: str | None
    source_index: int
    quality_notes: list[str]
    source_metadata: dict
    figure: dict | None = None
    table: dict | None = None

    def prompt_data(self) -> dict:
        return dict(vars(self))


class Corpus:
    def __init__(self, path: str | Path, profile: PrepProfile | None = None):
        self.profile = profile or PrepProfile()
        self.path = Path(path).resolve()
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.exam_structure = None
        if self.path.suffix.lower() not in {".csv", ".jsonl"}:
            envelope = json.loads(raw.decode("utf-8-sig"))
            if isinstance(envelope, dict) and envelope.get("exam_structure") is not None:
                self.exam_structure = AssessmentStructure.model_validate(envelope["exam_structure"])
        rows = read_records(self.path, raw)
        self.total = len(rows)
        self.records = rows
        self.references: list[Reference] = []
        self.flags: list[dict] = []
        self.taxonomy: dict[str, list[str]] = {}
        self.all_stems: set[str] = set()
        ids = set()
        for index, row in enumerate(rows):
            reasons = []
            if not isinstance(row, dict):
                raise ValueError(f"Record {index} is not an object. No records have been removed; fix its JSON structure before loading.")
            question_id = str(row.get("question_id", row.get("id", "")))
            if not question_id or question_id in ids:
                reasons.append("missing_or_duplicate_id")
            ids.add(question_id)
            stem = row.get("question", row.get("stem", row.get("prompt", "")))
            if not isinstance(stem, str) or len(stem.strip()) < 15:
                reasons.append("missing_or_short_question")
                stem = str(stem or "")
            passage = row.get("passage", row.get("context", ""))
            if isinstance(passage, str) and passage.strip() and passage.strip() not in stem:
                stem = passage.strip() + "\n\n" + stem
            stem = stem.strip()
            self.all_stems.add(normalized(stem))
            domain = row.get("domain", row.get("topic", row.get("subject")))
            subskill = row.get("subskill", row.get("skill", row.get("topic", domain)))
            if not isinstance(domain, str) or not domain.strip() or not isinstance(subskill, str) or not subskill.strip():
                reasons.append("invalid_taxonomy")
            domain, subskill = str(domain or "Unclassified").strip(), str(subskill or "Unclassified").strip()
            difficulty = str(row.get("difficulty", "")).strip().title()
            if difficulty not in {"Easy", "Medium", "Hard"}:
                reasons.append("invalid_difficulty")
            format_ = FORMATS.get(str(row.get("format", "")).strip().lower())
            if not format_:
                reasons.append("invalid_format")
            choices = row.get("choices", row.get("options", []))
            if isinstance(choices, str):
                try:
                    choices = json.loads(choices)
                except ValueError:
                    choices = []
            if isinstance(choices, dict):
                choices = [f"{label}) {text}" for label, text in sorted(choices.items())]
            if isinstance(choices, list) and all(isinstance(c, dict) and "label" in c and "text" in c for c in choices):
                choices = [f"{c['label']}) {c['text']}" for c in choices]
            if not choices and any(row.get(label) for label in "ABCDEFGH"):
                choices = [f"{label}) {row[label]}" for label in "ABCDEFGH" if row.get(label)]
            if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
                reasons.append("invalid_choices")
                choices = []
            if not format_:
                format_ = "MCQ" if choices else "FRQ"
            if format_ == "MCQ" and not 2 <= len(choices) <= 8:
                reasons.append("invalid_mcq_choice_count")
            if format_ != "MCQ" and choices:
                reasons.append("spr_has_choices")
            answer = row.get("answer", row.get("correct_answer"))
            if answer is None or not str(answer).strip():
                reasons.append("missing_answer")
            if row.get("needs_review") or row.get("normalization_status") == "needs_review":
                reasons.append("source_flagged_for_review")
            confidence = row.get("classification_confidence")
            if confidence is not None:
                try:
                    if float(confidence) < 0.8:
                        reasons.append("low_classification_confidence")
                except (ValueError, TypeError):
                    reasons.append("invalid_classification_confidence")
            combined = stem + " " + " ".join(choices)
            if self.profile.name == "SAT Math" and CALCULUS.search(combined):
                reasons.append("calculus_outside_scope")
            if MISSING_VISUAL.search(stem):
                reasons.append("visual_dependency_not_present_in_record")
            if BROKEN_ENCODING.search(combined):
                reasons.append("damaged_character_encoding")
            if len(combined) > 9000:
                reasons.append("reference_too_long")
            if reasons:
                self.flags.append({"source_index": index, "question_id": question_id, "notes": reasons})
            # Quality notes are advisory. Every source record remains in the reference pool.
            metadata = {key: row.get(key) for key in ("classification_confidence", "needs_review", "normalization_status", "normalization_notes", "question_type", "command_word", "marks", "paper", "section", "exam_board", "syllabus", "year") if key in row}
            self.references.append(Reference(question_id, domain, subskill, str(difficulty), format_, stem, choices, None if answer is None else str(answer), index, reasons, metadata, row.get("figure"), row.get("table")))
            self.taxonomy.setdefault(domain, [])
            if subskill not in self.taxonomy[domain]:
                self.taxonomy[domain].append(subskill)
        self.references.sort(key=lambda r: r.question_id)
        for skills in self.taxonomy.values():
            skills.sort()

    def report(self) -> dict:
        return {
            "source": str(self.path), "sha256": self.sha256, "total_records": self.total,
            "records_loaded": len(self.references), "discarded_records": 0,
            "records_with_notes": len(self.flags),
            "quality_note_counts": dict(Counter(note for row in self.flags for note in row["notes"])),
            "by_domain": dict(Counter(r.domain for r in self.references)),
            "by_difficulty": dict(Counter(r.difficulty for r in self.references)),
            "by_format": dict(Counter(r.format for r in self.references)),
            "taxonomy": self.taxonomy,
            "prep_profile": self.profile.to_dict(),
            "exam_structure": self.exam_structure.model_dump() if self.exam_structure else None,
            "note": "All source records are retained. Quality notes are advisory. Answers are independently checked, including when source keys are absent.",
            "flags": self.flags,
        }

    def retrieve(self, slot: Slot, count: int = 3) -> list[Reference]:
        if self.profile.source_based and slot.source_index is not None:
            return [self.source(slot.source_index)]
        candidates = [r for r in self.references if r.domain == slot.domain]
        def score(r):
            relevance = 8 * (r.subskill == slot.subskill) + 3 * (r.difficulty == slot.difficulty) + 2 * (r.format == slot.format)
            tie = hashlib.sha256(f"{slot.id}:{r.question_id}".encode()).hexdigest()
            return -relevance, tie
        selected = []
        for record in sorted(candidates, key=score):
            # Avoid filling the reference window with near-identical extracted items.
            if all(SequenceMatcher(None, normalized(record.question), normalized(p.question)).ratio() < 0.88 for p in selected):
                selected.append(record)
            if len(selected) == count:
                break
        if not selected:
            raise ValueError(f"No reference examples for {slot.domain}")
        return selected

    def source(self, index: int) -> Reference:
        for reference in self.references:
            if reference.source_index == index:
                return reference
        raise ValueError(f"Saved source record {index} is unavailable in this dataset")

    def inventory(self) -> list[dict]:
        counts = Counter((r.domain, r.subskill, r.difficulty, r.format) for r in self.references)
        return [dict(domain=d, subskill=s, difficulty=l, format=f, count=n) for (d, s, l, f), n in sorted(counts.items())]

    def style_examples(self, count: int = 6) -> list[dict]:
        """Small sample for the planner to ground wording/type conventions in actual records."""
        selected, groups = [], set()
        for reference in self.references:
            group = (reference.domain, reference.format)
            if group not in groups:
                selected.append(reference.prompt_data())
                groups.add(group)
            if len(selected) == count:
                break
        return selected

    def assign_sources(self, plan) -> None:
        """Assign distinct existing questions once, then persist the assignment in the plan."""
        used = set()
        for slot in plan.slots:
            candidates = [r for r in self.references if r.domain == slot.domain and r.subskill == slot.subskill and r.format == slot.format and (r.difficulty == slot.difficulty or r.difficulty not in {"Easy", "Medium", "Hard"}) and len(r.question) >= 10 and normalized(r.question) not in used]
            if slot.source_index is not None:
                candidates = [r for r in candidates if r.source_index == slot.source_index]
            if not candidates:
                raise ValueError(f"Not enough distinct source questions for {slot.domain} / {slot.subskill} ({slot.difficulty}, {slot.format}). Provide more source questions or request a smaller/different set.")
            source = candidates[0]
            slot.source_index = source.source_index
            used.add(normalized(source.question))

    def similarity(self, stem: str, references: list[Reference]) -> dict:
        text = normalized(stem)
        return {"exact_source_copy": text in self.all_stems, "reference_similarities": [{"id": r.question_id, "similarity": round(SequenceMatcher(None, text, normalized(r.question)).ratio(), 3)} for r in references]}
