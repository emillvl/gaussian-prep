# Request guide

Gaussian Prep uses your selected dataset and named exam/subject to build practice through the same five
roles: planner, generator, independent verifier, item reviewer, and final test auditor.

First name the prep material when prompted. For SAT, explicitly choose **SAT Math** or **SAT Verbal**.
For other exams include the subject, such as **AP Biology**, **A-Levels Mathematics**, **ACT English**,
**GCSE Physics**, or **CSCA Mathematics**. Include the exam board or syllabus version when relevant.

This guide provides editable examples. Conventions for one subject apply only to that subject; SAT Math
counts, timing, domain mix, and calculator rules must not carry into other prep material.
Lines beginning with `EXAMPLE MOCK:` or `EXAMPLE PRACTICE:` are shown at the request prompt.

## Example prompts

Replace the bracketed placeholders with your own exam and topics. `[TOPIC]` is the prep material you named,
and `[SUBTOPIC1]` and `[SUBTOPIC2]` are the skills to emphasize.

EXAMPLE MOCK: Generate a full [TOPIC] mock test using the paper structure, timing, and question types in my dataset.
EXAMPLE PRACTICE: Generate [COUNT] [TOPIC] practice questions covering [SUBTOPIC1] and [SUBTOPIC2], with mixed difficulty.

## More request shapes

- Create 8 questions for my selected exam and subject, with mixed difficulty and a balanced topic mix.
- Build a 12-question drill on the topic I specify, medium and hard only, using the dataset's usual answer format.
- For SAT Verbal, use 10 existing reading and writing questions, preserving passages and stems while reordering or rewording answer choices.
- For AP Biology, create 6 multiple-choice questions and 2 written-response questions about cell membranes, with mixed difficulty.
- For SAT Math, build a 44-question, 2-module practice test with 35 minutes per module, all four domains, and about 75% MCQ and 25% numeric SPR.
- For A-Levels Mathematics, create 8 questions on differentiation, including 3 written explanations. Use my stated exam-board requirements.
- For ACT English, adapt 10 existing questions and their associated passages; preserve the original tasks and verify the revised answer choices.
- For GCSE Physics, create 8 questions on electricity, including numeric calculations and conceptual explanations.
- For CSCA Mathematics, create 12 questions on functions and geometry, using the format and difficulty shown in my dataset.

## Writing a request

- Give the number of questions, up to 100 per run.
- Name the topics or subskills to emphasize. Available labels come from your dataset.
- Choose easy, medium, hard, or mixed difficulty. Difficulty should reflect reasoning, not verbose wording.
- Specify modules and timing if needed. Otherwise the planner records practice assumptions, not official exam rules.
- Specify MCQ, numeric SPR, or written FRQ, or give an exact mix. MCQ permits 2 to 8 choices with one correct answer; SAT uses four.
- Name the exam board and any important syllabus or calculator requirements.

## Mock versus practice

Use **mock** for a complete target exam, subject section, or named paper. State the board, variant and paper
when relevant. The planner states its structural basis, then fixes the sections, timing, question types,
choice counts, order, directions and any exact topic quotas. It reports essential ambiguity for clarification.
Wording and question style continue to come from your reference examples and the named exam.

Use **practice**, **drill**, or **mini/shortened mock** for a custom selection. It keeps your requested size
and topics instead of filling out a whole exam. Estimated timings are identified as practice assumptions.

For SAT Math mocks, put MCQs first and all numeric SPR questions at the end of EACH module. This is this
project's chosen layout preference. An explicit request for interleaving or another order overrides it.
For other subjects, follow the selected paper's own sequence and any ordering the user specifies.

Examples: "Create a mock using the paper structure in my dataset"; "Create 12 practice questions, 8 MCQ
followed by 4 numeric responses"; "Use 2 sections with 10 minutes per section, putting written responses last."
All five roles must preserve the saved structure during revisions. The final export must keep its order.

## Existing questions for verbal material

SAT Verbal and reading/language/history material use existing source questions. The passage, stem, table,
and figure stay unchanged. The generator may reorder or reword choices, or replace incorrect distractors,
while keeping the correct meaning and updating the answer key. It does not invent new passages or questions.
There must be enough distinct matching source questions for the requested count, skills, format, and difficulty.
Supply complete passages and any required visuals in the dataset.

## New questions for math and science

SAT Math and named math/science subjects use the dataset as reference material for new questions.
Other subjects can include calculus or advanced topics when appropriate; SAT Math remains within SAT Math scope.
The verifier computes what the engine can represent and independently reasons through other tasks. The item
reviewer checks the answer, subject evidence, and difficulty; the final auditor checks the complete practice set.

## Output

Choose PDF, DOCX, both, or skip after the checks finish. Student sheets omit answers. The separate answer key
contains choice labels, exact numeric answers, or model written responses. The saved summary reports audit
results and distinguishes computational checks from independent subject reasoning.
