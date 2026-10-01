"""Persisted assessment structure and deterministic checks shared by planning and export."""
from collections import Counter
import re

from .models import AssessmentStructure, Plan, Question, SectionStructure, Slot


def requested_kind(request: str) -> str | None:
    text = request.casefold()
    # Shortened/mini mocks are custom practice, not a claim to a full exam.
    if re.search(r"\b(?:mini|shortened|short|custom)\s+(?:mock|test|practice)\b", text):
        return "practice"
    text = re.sub(r"\b(?:not|no|without)\s+(?:a\s+)?(?:full\s+)?mock(?:\s+(?:test|exam))?\b", "", text)
    if re.search(r"\bmock\b|\bfull[- ]length\b|\bfull\b[^.\n]{0,60}\b(?:exam|test|paper|section)\b", text):
        return "mock"
    if re.search(r"\b(?:practice|drill|warm[- ]?up)\b", text):
        return "practice"
    return None


def requested_order(request: str, material: str, kind: str) -> str | None:
    """Common explicit ordering requests plus this project's SAT Math mock preference."""
    text = request.casefold()
    # An explicit mixed layout overrides the saved SAT layout preference.
    if re.search(r"\b(?:interleav\w*|alternat\w*)\b|\b(?:mixed|random|randomized)\s+(?:question\s+)?order\b", text):
        return None
    open_type = r"(?:open[- ]ended|student[- ]produced(?: responses?)?|numeric(?: responses?)?|written[- ]responses?|free[- ]responses?|sprs?|frqs?)"
    mcq = r"(?:mcqs?|multiple[- ]choice(?: questions?)?)"
    if re.search(open_type + r"[^.;\n]{0,25}\bbefore\s+" + mcq, text) or re.search(mcq + r"[^.;\n]{0,25}\bafter\s+" + open_type, text):
        return "open_first"
    if re.search(open_type + r"[^.;\n]{0,25}\bafter\s+" + mcq, text) or re.search(mcq + r"[^.;\n]{0,25}\bbefore\s+" + open_type, text):
        return "open_last"
    mentions = list(re.finditer(r"\b(?:" + open_type + "|" + mcq + r")\b", text))
    orders = set()
    for index, match in enumerate(mentions):
        end = mentions[index + 1].start() if index + 1 < len(mentions) else len(text)
        following = re.split(r"[.;\n]", text[match.end():end][:65])[0]
        is_open = bool(re.fullmatch(open_type, match[0]))
        if re.search(r"\b(?:first|at the (?:start|beginning))\b", following):
            orders.add("open_first" if is_open else "open_last")
        if re.search(r"\b(?:last|at the end|at each[^.;\n]{0,15}end)\b", following):
            orders.add("open_last" if is_open else "open_first")
    if len(orders) > 1:
        raise ValueError("Conflicting format-order requirements; clarify which question type comes first")
    if orders:
        return orders.pop()
    if material == "SAT Math" and kind == "mock":
        return "open_last"
    return None


def ensure_structure(plan: Plan, request: str) -> None:
    """Older/simple practice planners can be upgraded without imposing mock defaults."""
    if plan.structure is not None:
        return
    if requested_kind(request) == "mock":
        raise ValueError("A mock needs an explicit assessment structure: target exam/paper, basis, section timing, format sequence, and style conventions. Supply structure before allocating questions.")
    plan.structure = AssessmentStructure(kind="practice", target=plan.title,
        basis="Custom practice blueprint from the user's request; no full-exam compatibility claim.",
        sections=[SectionStructure(module=module, name=f"Module {module}", minutes=plan.minutes_per_module[module - 1],
            format_sequence=[slot.format for slot in sorted(plan.slots, key=lambda slot: slot.position) if slot.module == module], mcq_choices=None)
            for module in range(1, plan.module_count + 1)])


def section_for(plan: Plan, module: int) -> SectionStructure | None:
    if plan.structure:
        return next((s for s in plan.structure.sections if s.module == module), None)
    return None


def item_structure_errors(plan: Plan, slot: Slot, question: Question) -> list[str]:
    errors = []
    section = section_for(plan, slot.module)
    if question.slot_id != slot.id or question.format != slot.format:
        errors.append(f"Question {slot.id} must keep its assigned ID and {slot.format} format")
    if section and section.mcq_choices is not None and question.format == "MCQ" and len(question.choices) != section.mcq_choices:
        errors.append(f"{section.name}, question {slot.position} requires exactly {section.mcq_choices} choices")
    return errors


def structure_errors(plan: Plan, items: dict | None = None) -> list[str]:
    structure = plan.structure
    if structure is None:
        return []  # Existing saved runs retain their original plan.
    errors = []
    if len(structure.sections) != plan.module_count:
        errors.append("Section count differs from the saved structure")
    for section in structure.sections:
        slots = sorted((s for s in plan.slots if s.module == section.module), key=lambda s: s.position)
        if [s.format for s in slots] != section.format_sequence:
            errors.append(f"{section.name}: question counts, types or order differ from format_sequence")
        if section.module > len(plan.minutes_per_module) or plan.minutes_per_module[section.module - 1] != section.minutes:
            errors.append(f"{section.name}: time limit must remain {section.minutes} minutes")
        domains = Counter(s.domain for s in slots)
        for domain, count in section.domain_counts.items():
            if domains[domain] != count:
                errors.append(f"{section.name}: requires {count} questions in {domain}, found {domains[domain]}")
        if items is not None:
            for slot in slots:
                if slot.id not in items:
                    errors.append(f"{section.name}: missing question {slot.id}")
                else:
                    errors.extend(item_structure_errors(plan, slot, Question.model_validate(items[slot.id]["question"])))
    if items is not None and set(items) != {slot.id for slot in plan.slots}:
        errors.append("Saved questions do not match the structure's assigned question IDs")
    return errors


def check_structure_request(plan: Plan, request: str, material: str, dataset_structure: AssessmentStructure | None = None) -> None:
    """Check the explicit contract and common literal requests independently of the model."""
    errors = structure_errors(plan)
    structure = plan.structure
    if structure is not None:
        kind = requested_kind(request)
        if kind and kind != structure.kind:
            errors.append(f"The request is for {kind}; structure.kind must match")
        # Dataset metadata describes a full mock. Practice can freely change its shape.
        if structure.kind == "mock" and dataset_structure is not None:
            if [s.model_dump() for s in structure.sections] != [s.model_dump() for s in dataset_structure.sections]:
                errors.append("The mock must follow the dataset's exam_structure sections exactly. A changed/shortened structure must be requested as custom practice.")
        ordering = requested_order(request, material, structure.kind)
        for section in structure.sections:
            if structure.kind == "mock" and "MCQ" in section.format_sequence and section.mcq_choices is None:
                errors.append(f"{section.name}: specify the MCQ choice count for a mock")
            flags = [fmt != "MCQ" for fmt in section.format_sequence]
            if ordering and flags != sorted(flags, reverse=ordering == "open_first"):
                errors.append(f"{section.name}: place all open-ended questions {'at the end' if ordering == 'open_last' else 'at the start'} of this section")
        minutes = re.findall(r"\b(\d+)\s*minutes?\s+(?:each|per\s+(?:module|section)|for each\s+(?:module|section))\b", request, re.I)
        if len(set(minutes)) == 1 and any(s.minutes != int(minutes[0]) for s in structure.sections):
            errors.append(f"Every section must use the requested {minutes[0]}-minute limit")
    if errors:
        raise ValueError("Structure check: " + "; ".join(errors))


def section_name(plan: Plan, module: int) -> str:
    section = section_for(plan, module)
    return section.name if section else f"Module {module}"


def assessment_label(plan: Plan) -> str:
    return "Mock exam" if plan.structure and plan.structure.kind == "mock" else "Custom practice"
