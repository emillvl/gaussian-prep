from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Domain = str
Difficulty = Literal["Easy", "Medium", "Hard"]
Format = Literal["MCQ", "SPR", "FRQ"]
ChoiceLabel = Literal["A", "B", "C", "D", "E", "F", "G", "H"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Slot(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    module: int = Field(ge=1, le=10)
    position: int = Field(ge=1, le=100)
    domain: Domain = Field(min_length=1)
    subskill: str = Field(min_length=1)
    difficulty: Difficulty
    format: Format
    objective: str = Field(min_length=1, description="What this new question must require the student to do.")
    source_index: int | None = Field(default=None, ge=0, description="Assigned by the app for source-based practice; leave null when planning.")


class SectionStructure(StrictModel):
    module: int = Field(ge=1, le=10)
    name: str = Field(min_length=1, max_length=160, description="Student-facing section/paper/module name.")
    minutes: int = Field(gt=0)
    format_sequence: list[Format] = Field(min_length=1, max_length=100, description="One format per question, in its required position; this fixes counts and order.")
    mcq_choices: int | None = Field(default=4, ge=2, le=8, description="Number of choices for this section's MCQs; required for mocks, optional for legacy/custom practice.")
    domain_counts: dict[str, Annotated[int, Field(ge=0, le=100)]] = Field(default_factory=dict, description="Exact counts for any specified topic quotas; omit unspecified topics.")
    instructions: list[str] = Field(default_factory=list, description="Student-facing directions, such as calculator rules, supported by the request or exam convention.")


class AssessmentStructure(StrictModel):
    kind: Literal["mock", "practice"]
    target: str = Field(min_length=1, description="The exam/subject and paper, board or version where known; identify a full mock or the custom practice scope.")
    basis: str = Field(min_length=1, description="Explain the source of the structure: explicit request, dataset metadata, request guide, or named exam conventions. Do not invent a citation.")
    sections: list[SectionStructure] = Field(min_length=1, max_length=10)
    question_style: list[str] = Field(default_factory=list, description="Concrete wording, command-word, response-depth and question-type conventions supported by the dataset and selected exam.")

    @model_validator(mode="after")
    def consistent(self):
        if [section.module for section in self.sections] != list(range(1, len(self.sections) + 1)):
            raise ValueError("Structure sections must be in consecutive module order starting at 1")
        if sum(len(section.format_sequence) for section in self.sections) > 100:
            raise ValueError("The structure exceeds the 100-question run limit")
        for section in self.sections:
            if sum(section.domain_counts.values()) > len(section.format_sequence):
                raise ValueError("Topic quotas exceed their section's question count")
        return self


class Plan(StrictModel):
    title: str = Field(min_length=1)
    interpretation: str
    assumptions: list[str]
    requirements: list[str]
    conflicts: list[str] = Field(default_factory=list, description="Contradictory or unsupported user requirements; empty if feasible.")
    total_questions: int = Field(ge=0, le=100, description="Zero only when reporting conflicts without a blueprint.")
    module_count: int = Field(ge=0, le=10, description="Zero only when reporting conflicts without a blueprint.")
    minutes_per_module: list[int]
    slots: list[Slot]
    structure: AssessmentStructure | None = Field(default=None, description="Required for new feasible plans. Describe mock/practice scope and exact section structure before allocating slots; null only for conflicts or legacy plans.")

    @model_validator(mode="after")
    def consistent(self):
        if self.conflicts and not self.total_questions and not self.module_count and not self.slots and not self.minutes_per_module:
            return self
        if self.total_questions < 1 or self.module_count < 1:
            raise ValueError("A feasible plan requires questions and modules; an empty conflict report requires conflicts")
        if len(self.slots) != self.total_questions:
            raise ValueError("Slot count must equal total_questions")
        if len({s.id for s in self.slots}) != len(self.slots):
            raise ValueError("Slot IDs must be unique")
        if {s.module for s in self.slots} != set(range(1, self.module_count + 1)):
            raise ValueError("Every declared module must contain questions")
        if len(self.minutes_per_module) != self.module_count or any(t <= 0 for t in self.minutes_per_module):
            raise ValueError("Provide a positive duration for each module")
        for module in range(1, self.module_count + 1):
            positions = sorted(s.position for s in self.slots if s.module == module)
            if positions != list(range(1, len(positions) + 1)):
                raise ValueError("Positions within each module must be consecutive, starting at 1")
        return self


class Choice(StrictModel):
    label: ChoiceLabel
    text: str = Field(min_length=1)


class Point(StrictModel):
    label: str = Field(min_length=1, max_length=64, description="Literal point label, not LaTeX.")
    x: float = Field(ge=-10000, le=10000)
    y: float = Field(ge=-10000, le=10000)


class Segment(StrictModel):
    start: str
    end: str


class Circle(StrictModel):
    x: float = Field(ge=-10000, le=10000)
    y: float = Field(ge=-10000, le=10000)
    radius: float = Field(gt=0, le=10000)


class Figure(StrictModel):
    description: str = Field(min_length=1)
    show_axes: bool
    points: list[Point] = Field(max_length=60)
    segments: list[Segment] = Field(max_length=100)
    circles: list[Circle] = Field(max_length=10)

    @model_validator(mode="after")
    def references_exist(self):
        labels = [p.label for p in self.points]
        if len(labels) != len(set(labels)):
            raise ValueError("Figure point labels must be unique")
        if not self.points and not self.circles:
            raise ValueError("A figure must have visible geometry")
        if any(s.start not in labels or s.end not in labels for s in self.segments):
            raise ValueError("Segments must refer to existing points")
        return self


class Table(StrictModel):
    headers: list[str] = Field(min_length=1, max_length=10)
    rows: list[list[str]] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def rectangular(self):
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("Every table row must match the headers")
        return self


class Question(StrictModel):
    slot_id: str
    stem: str = Field(min_length=10, max_length=32000)
    format: Format
    choices: list[Choice]
    answer: str = Field(min_length=1, description="Choice label for MCQ; exact numeric answer for SPR; model response for FRQ.")
    solution: str = Field(default="", description="Internal working used only for checking; never shown on the sheet.")
    figure: Figure | None
    table: Table | None

    @model_validator(mode="after")
    def check_format(self):
        if self.format == "MCQ":
            labels = [c.label for c in self.choices]
            if not 2 <= len(labels) <= 8 or sorted(labels) != list("ABCDEFGH"[:len(labels)]):
                raise ValueError("MCQ requires 2–8 choices labeled consecutively from A")
            if self.answer not in labels:
                raise ValueError("MCQ key must match a displayed choice label")
        elif self.choices:
            raise ValueError("Open responses must not show choices")
        return self

    def student_view(self) -> dict:
        return self.model_dump(exclude={"answer", "solution"})


class Equation(StrictModel):
    lhs: str
    rhs: str


class Constraint(Equation):
    operator: Literal["gt", "ge", "lt", "le", "ne", "eq"]


class OptionMath(StrictModel):
    label: ChoiceLabel
    expression: str


class MathModel(StrictModel):
    mode: Literal["evaluate", "solve", "identity", "unsupported"]
    interpretation: str = Field(min_length=1, description="How the displayed question maps to these equations and target.")
    variables: list[str] = Field(max_length=3)
    equations: list[Equation] = Field(max_length=6)
    constraints: list[Constraint] = Field(max_length=10)
    target: str
    aggregate: Literal["each", "sum", "product", "count", "min", "max"]
    options: list[OptionMath]
    unsupported_reason: str
    independent_answer: str = Field(default="", description="For non-computational questions, independently derive the choice label or model response without the author's key.")
    evidence: str = Field(default="", description="Evidence from the passage, facts, or reasoning supporting the independent answer and ruling out distractors.")


class Finding(StrictModel):
    status: Literal["pass", "revise", "reject"]
    reason: str = Field(min_length=1)


class ItemReview(StrictModel):
    wording: Finding
    difficulty: Finding
    alignment: Finding
    clarity: Finding
    originality: Finding
    mathematical_model: Finding
    solution: Finding = Field(description="Independent confirmation that the keyed choice is correct and the others are wrong.")
    estimated_difficulty: Difficulty

    def accepted(self) -> bool:
        return all(getattr(self, name).status == "pass" for name in type(self).model_fields if name != "estimated_difficulty")


class AuditIssue(StrictModel):
    dimension: Literal["coverage", "variety", "progression", "workload", "intent", "format", "wording_consistency", "structure"]
    reason: str = Field(min_length=1)
    slot_ids: list[str]
    instruction: str = Field(min_length=1)
    repair_kind: Literal["content", "formatting"] = Field(default="content", description="formatting only for escaping/delimiter/typesetting defects, never changed numbers, units, wording meaning, or coverage.")


class Audit(StrictModel):
    accepted: bool
    summary: str = Field(min_length=1)
    issues: list[AuditIssue]

    @model_validator(mode="after")
    def consistent(self):
        if self.accepted != (not self.issues):
            raise ValueError("Accepted audit must have no issues; failed audit must explain issues")
        return self


class FollowupAudit(Audit):
    checked_slot_ids: list[str] = Field(description="Every assigned follow-up slot reviewed, including resolved slots. Must exactly match the supplied audit scope.")
