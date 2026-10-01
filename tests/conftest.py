import json

import pytest

from gaussian_prep.corpus import Corpus
from gaussian_prep.models import Audit, ItemReview, MathModel, Plan, Question, Slot


def slot(number=1, format="MCQ"):
    return Slot(id=f"q{number}", module=1, position=number, domain="Algebra", subskill="Linear equations in one variable", difficulty="Easy", format=format, objective="Solve a two-step linear equation.")


def question(number=1, rhs=17, format="MCQ"):
    key = (rhs - 5) // 2
    return Question(slot_id=f"q{number}", stem=f"If 2x + 5 = {rhs}, what is the value of x?", format=format,
        choices=[] if format == "SPR" else [{"label": label, "text": str(value)} for label, value in zip("ABCD", [key, (rhs+5)//2, rhs-5, (rhs-5)*2])],
        answer=str(key) if format == "SPR" else "A", solution=f"Subtract 5, then divide both sides by 2 to get x = {key}.",
        figure=None, table=None)


def mathematical_model(rhs=17, format="MCQ"):
    return MathModel(mode="solve", interpretation="Translate the displayed equation; solve for the real value x.", variables=["x"], equations=[{"lhs": "2*x+5", "rhs": str(rhs)}], constraints=[], target="x", aggregate="each", options=[] if format == "SPR" else [{"label": label, "expression": str(value)} for label, value in zip("ABCD", [(rhs-5)//2, (rhs+5)//2, rhs-5, (rhs-5)*2])], unsupported_reason="")


def review():
    return ItemReview(**{name: {"status": "pass", "reason": "Test fixture: acceptable for the assigned objective."} for name in ["wording", "difficulty", "alignment", "clarity", "originality", "mathematical_model", "solution"]}, estimated_difficulty="Easy")


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "references.json"
    path.write_text(json.dumps([{"question_id": "ref1", "domain": "Algebra", "subskill": "Linear equations in one variable", "difficulty": "Easy", "format": "Multiple Choice", "question": "A worker earns a fixed payment plus an hourly amount. Determine the number of hours from the total payment.", "choices": ["A) 2", "B) 4", "C) 6", "D) 8"], "answer": "B", "needs_review": False, "classification_confidence": 0.9}]), encoding="utf-8")
    return Corpus(path)


class ScriptedProvider:
    model = "test-only-scripted-model"

    def __init__(self, count=1):
        self.count = count
        self.calls = []
        self.generated = {}

    def ask(self, role, payload, schema):
        self.calls.append((role, payload))
        if role == "planner":
            return Plan(title="Fixture Practice", interpretation="Fixture plan.", assumptions=[], requirements=[], total_questions=self.count, module_count=1, minutes_per_module=[10], slots=[slot(i) for i in range(1, self.count+1)])
        if role == "generator":
            sid = payload["slot"]["id"]
            self.generated[sid] = self.generated.get(sid, 0) + 1
            number = int(sid[1:])
            return question(number, 17 + 2*number + 2*(self.generated[sid]-1))
        if role == "verifier":
            import re
            rhs = int(re.search(r"= (\d+)", payload["question"]["stem"])[1])
            return mathematical_model(rhs)
        if role == "reviewer":
            return review()
        if role == "auditor":
            return audit_result(payload, schema, accepted=True, summary="Test fixture audit accepted.", issues=[])
        raise AssertionError(role)


def audit_result(payload, schema, **fields):
    if "checked_slot_ids" in schema.model_fields:
        fields["checked_slot_ids"] = list(payload["scope_slot_ids"])
    return schema(**fields)
