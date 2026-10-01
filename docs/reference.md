# Gaussian Prep reference

This is the detailed reference that backs the [README](../README.md). It covers the dataset format, the five roles, the math checker, the structure contract, how exports are built, and what happens when something fails.

## Contents

- [How a run fits together](#how-a-run-fits-together)
- [Datasets](#datasets)
- [New questions and source adaptation](#new-questions-and-source-adaptation)
- [The five roles](#the-five-roles)
- [Mocks, practice, and the structure contract](#mocks-practice-and-the-structure-contract)
- [The math checker](#the-math-checker)
- [LaTeX, PDF, and Word](#latex-pdf-and-word)
- [Failures, retries, and resume](#failures-retries-and-resume)
- [Providers and models](#providers-and-models)
- [Command reference](#command-reference)
- [Limits](#limits)

## How a run fits together

The entry point is `python -m gaussian_prep`, reached through `run.ps1` or `START.cmd`. With no subcommand it behaves like `generate`. A run does the following in order:

1. Load the dataset into a `Corpus`, hashing the file so resume can prove the source has not changed.
2. Ask the planner for a blueprint, validate it against the request and the dataset taxonomy, and checkpoint it.
3. Assign source records to slots when the subject adapts existing questions.
4. Generate, verify, review, and revise the pending slots in parallel.
5. Run the whole-test auditor, apply targeted replacements if it finds problems, then finish.
6. Ask for an export format and write the documents and `summary.html`.

Every model call is written to `support/diagnostics/calls/`, and each question attempt to `support/diagnostics/items/`. Authentication headers are omitted from logs. Payloads and provider responses are retained and may contain private content; see the [privacy notes](../README.md#privacy-and-security). The checkpoint (`support/state.json`) is replaced after accepted or failed slots. A crash can lose work completed since the last successful checkpoint write.

## Datasets

The loader accepts four shapes:

- a JSON array of records
- a JSON object with a `questions` or `records` key
- JSONL, one record per line
- CSV with a header row

These field names are read, each with common aliases:

| Field | Purpose | Aliases |
| --- | --- | --- |
| `question_id` | stable identifier | `id` |
| `domain` | topic group | `topic`, `subject` |
| `subskill` | skill within the domain | `skill`, `topic` |
| `difficulty` | `Easy`, `Medium`, or `Hard` | |
| `format` | `MCQ`, `SPR`, or `FRQ` | |
| `question` | the stem | `stem`, `prompt` |
| `choices` | answer options | `options` |
| `answer` | correct answer | `correct_answer` |
| `passage` | reading text | `context` |
| `figure` | structured figure | |
| `table` | structured table | |

A passage supplied in `passage` or `context` is prepended to the stem unless it is already present. Choices can be plain strings, objects with `label` and `text`, a label-to-text object, or a JSON string; in CSV they can also come from columns `A` through `H`. Multiple-choice items need 2 to 8 choices and exactly one key. `format` accepts the long names too (`multiple choice`, `student-produced response`, `free response`, and so on). If the format is omitted, a record with choices becomes MCQ and one without becomes FRQ.

The taxonomy comes entirely from the file. The planner can only use `domain` and `subskill` values that exist in the dataset, and requesting an unknown subskill is rejected before generation. Every record is kept, including ones with advisory problems: a missing or duplicate id, a short stem, invalid taxonomy or difficulty, a missing answer, low `classification_confidence`, a `needs_review` flag, damaged encoding, a referenced but absent figure, an over-long record, or a calculus question in an SAT Math dataset. These become quality notes on the reference; they never remove a record or lower how it is retrieved. Retrieval ranks candidates by matching domain, then subskill, difficulty, and format, with a hashed tie-break and a near-duplicate filter so the reference window is not filled with near-identical extracted items.

To inspect a dataset without any model calls:

```powershell
.\run.ps1 inspect --report dataset-report.json
```

The report lists total and retained records, advisory note counts, per-domain, per-difficulty and per-format counts, the full taxonomy, the prep profile, and any supplied `exam_structure`.

A JSON object may carry an `exam_structure` beside its records. See [Mocks, practice, and the structure contract](#mocks-practice-and-the-structure-contract).

Image-only or PDF question banks are not parsed. Convert them to structured records first.

## New questions and source adaptation

The prep profile decides how the model is used, and the choice is recorded in the run and restored on resume.

SAT Math, and named quantitative subjects (math, algebra, geometry, calculus, statistics, physics, chemistry, biology, economics, computer science), run in new-question mode. The dataset is a reference for style, skill, and difficulty; the model writes new items.

SAT Verbal, and subjects matching reading, language, writing, English, literature, or history, run in source-based mode. Unrecognized subjects also default to source-based, since inventing source-dependent content is riskier than adapting it. In this mode each slot is assigned a distinct dataset record, and the generator must keep that record's passage, stem, table, and figure unchanged. It may reorder or reword the choices, replace wrong distractors, and update the key, but it cannot invent a new passage or question. The assigned source format and choice count must be preserved, and the choices must not be copied verbatim. If the dataset does not hold enough distinct records for the requested topic, skill, difficulty, and format, the planner reports the shortfall instead of padding the test.

For SAT specifically, the prep material must be entered as `SAT Math` or `SAT Verbal`; plain `SAT` is rejected because the two modes behave differently. For any subject, the name is trimmed, must be non-empty, and is capped at 160 characters.

## The five roles

Every run uses the same five roles, each a separate model call with its own system prompt and a JSON output schema that is validated locally. The prompts live in `src/gaussian_prep/prompts.py` and change by subject and mode. All five roles receive the saved assessment structure.

**Planner.** Turns the request into a `Plan`: a title, interpretation, assumptions, requirements, a conflict list, total questions, module count, per-module minutes, and one slot per question. A slot carries a unique id, module, position, domain, subskill, difficulty, format, and an objective that says what the student must do. The planner sees the dataset taxonomy, an inventory of what exists, a small sample of real records for style, the request guide, and any dataset `exam_structure`. If the request is contradictory, asks for something outside the supported formats or limits, or cannot be met with the available material, it fills `conflicts` and returns zero questions instead of inventing a blueprint. The app re-prompts up to three times with the specific validation error before giving up.

**Generator.** Writes one question per slot. It receives the slot, the request, the test requirements, a small reference sample, any revision feedback, the previous draft, the stems of questions already accepted, and the assigned source record in source-based mode. MCQ items get labeled choices, SPR items get a numeric answer in engine notation, and FRQ items get a written model answer with no choices. Working notes go in an internal `solution` field that is excluded from the student view.

**Independent verifier.** Sees only the student-facing question, never the key or the solution. It picks a math mode and, for supported items, lets SymPy compute the answer. For items the engine cannot represent, it solves the item from the text and returns `independent_answer` and `evidence`, which the pipeline records as subject reasoning rather than computational proof. If a supported model does not verify, the verifier gets one bounded repair attempt on the same displayed question, still blind to the answer.

**Item reviewer.** Checks separate dimensions: wording, estimated difficulty, alignment, clarity, originality (or source faithfulness), faithfulness of the verifier's model, and answer correctness. It inspects the computation, the independent model, and the similarity report. Every dimension must pass and `estimated_difficulty` must equal the slot difficulty. For source-based items, originality means faithful adaptation with valid changed choices, and reusing the source stem is required. A review that fails only the mathematical model triggers one blind verifier retry before the generator is involved.

**Test auditor.** Reviews the assembled test: coverage, repetition, difficulty progression, workload, format mix, consistency, and whether the test matches the requested structure. It sees computed counts and the item reviews. It names affected slot ids and gives replacement instructions. Unresolved findings after the configured replacement rounds are kept as warnings on a finished test rather than discarding verified items, except for structure conflicts, which block completion.

## Mocks, practice, and the structure contract

A **mock** reproduces a whole exam, section, or named paper. Its blueprint must include: a `target`, a `basis` explaining where the structure came from, and one section per module with a name, time limit, a `format_sequence` giving one `MCQ`, `SPR`, or `FRQ` per question in position order, an `mcq_choices` count, optional exact `domain_counts` quotas, and student-facing `instructions`. If the board, paper, or variant is ambiguous in a way that would change the mock, the planner has to report a conflict rather than guess.

**Practice**, a drill, or a shortened mock is a custom selection. It keeps your requested count and topics, and its timings are labeled as practice assumptions. The planner still records a structure, but marks it as custom practice with no full-exam claim.

When the dataset contains an `exam_structure`, a full mock must reproduce its sections exactly. A request for a shorter or different shape is only treated as custom practice if the user actually asked for practice; otherwise it is reported as a conflict. Datasets without `exam_structure` need no changes.

The app builds the structure from the request when the planner leaves it out of a practice plan, then checks the structure repeatedly: before generation, when accepting each item, on resume, before the final audit, and again before export. Checks include section count, format sequence and order, per-section timing, MCQ choice count, topic quotas, and the saved slot ids. The requested kind (mock versus practice) and explicit order requests are checked independently of the model. Structure conflicts raise a request conflict rather than an optional warning.

SAT Math mocks have one house layout preference: multiple-choice questions first, numeric SPR questions at the end of each module. A request for a different order, or for interleaved questions, overrides it. This is a preference in this project, not a claim about the real exam.

## The math checker

The checker is a bounded expression language, not a general Python evaluator. Model text is parsed with `ast` and never passed to `eval` or `sympify`. The verifier chooses one of four modes:

- `evaluate`: a fully specified numeric computation with no variables or equations. The target is the whole calculation.
- `solve`: one to three real variables and at least one equation. The engine computes the complete finite solution set, then evaluates the target. `aggregate` can be `each`, or `sum`, `product`, `count`, `min`, or `max` over the distinct computed target values.
- `identity`: the target is a displayed expression, compared against each choice over the stated domain. This covers "which expression is equivalent" items with radicals and denominators. Equations must be empty, aggregate must be `each`, and constraints carry the domain restrictions.
- `unsupported`: the task cannot be represented faithfully. It comes with a reason, and the reviewer must solve the item independently.

Expressions use explicit operators (`2*x`, `x**2`), named real variables, `pi`, `sqrt`, `abs`, `sin`, `cos`, `tan`, `log`, `exp`, and `mean`, `median`, `stdev`, or `sum` applied to a list. `stdev` is the population standard deviation, and trigonometry is in radians. The parser also understands common LaTeX forms (`\frac`, `\sqrt`, `\times`, `\pi`, unicode minus, and so on) so the verifier can write either style.

For MCQ, every displayed choice must be translated to an expression, the computed correct set must be exactly the keyed choice, and no two choices may be mathematically equal. For SPR, the computed numeric answer must match the key. For FRQ, computation is skipped and the item always relies on independent review.

The engine handles exact arithmetic, finite real solutions to equations and small systems, stated inequalities and exclusions, expression equivalence, complementary-angle trigonometry, and basic statistics. It proves identities symbolically using the stated domain, tracking denominator and radical restrictions, and treats sampled values only as counterexamples, never as proof. Symbolic results it cannot resolve, timeouts, and unmodelable items stay `unverified`; an unverified supported model is treated as a failure, while an unverified item with an `unsupported` model goes to the reviewer.

Computation runs in a separate child process with a 12 second timeout. The worker is killed if it overruns or the parent is interrupted. The saved evidence carries a `verification_version`, and resume rechecks older evidence with the current checker. A genuine computational failure, where the engine computes a different key, always sends the item back to the generator and cannot be overridden by a favorable review.

## LaTeX, PDF, and Word

The generator returns final LaTeX in the stem, choices, and table cells. JSON is transport only: each backslash is encoded once, decoded once, and saved unchanged. New drafts must pass validation without rewriting their authored math. Legacy checkpoints may receive the compatibility repairs described below.

Before mathematical verification, the question text is validated. Unmatched delimiters, unbalanced braces, doubled backslashes, unknown commands, bare LaTeX, dollar math, and unbraced multi-character exponents are rejected and returned to the generator with the original draft and specific feedback. A one-time, logged compatibility repair for older checkpoints normalizes unambiguous presentation defects: decoded doubled backslashes before known commands or delimiters, `$$...$$` display delimiters, and unbraced numeric scripts. New drafts that require these repairs are returned to the generator. Every math expression must be renderable by the shared typesetter and translatable to MathML, which is filtered to a safe element and attribute set.

PDFs prefer a native engine. If `pdflatex`, `xelatex`, `lualatex`, or Tectonic is found (including common MiKTeX and TeX Live install paths), the app writes `questions.tex` and `answer-key.tex` and compiles both. The source is retained whether or not compilation succeeds. A compile failure, timeout, or missing engine falls back to a reportlab renderer that typesets each math expression to an image at its natural size, so single variables are not enlarged and tall fractions and nested radicals are not crushed. Wide math is bounded by the paragraph or table cell width, and table text wraps.

Questions are kept together with their choices or answer box when they fit, and oversized questions are allowed to break across pages. DOCX uses the same math-to-image renderer and wraps table cells the same way. The answer key lists choice labels for MCQ, exact numeric answers for SPR, and model written responses for FRQ, with numeric keys typeset through the same expression parser. Student sheets omit the key and solution, keep passage line breaks, and leave a response box for open items and writing space for FRQ.

Figure point labels are literal text of 1 to 64 characters, not LaTeX. Raster figures use a fixed canvas; keep labels short to avoid clipping at the edges.

If `src/gaussian_prep/logo.png` exists, the sheet and key each end with one blank page carrying the logo centered both ways, with no text or page number. Remove the file to skip it.

## Failures, retries, and resume

When generation or an audit step raises a retryable failure, the CLI retries twice more with the same provider instance and the shared call budget, keeping accepted questions and the pending audit scope. After that it tells you to resume. A question that never passes after its revisions does not abort the run; the remaining questions still generate and the failures are reported. Ctrl+C returns exit code 130 and preserves checkpointed work; cleanup may wait for active work to stop.

```powershell
.\run.ps1 resume runs\20260101-120000-000000
```

Resume requires the same request, dataset contents (checked by hash), prep profile, provider, and model. Before spending any calls it revalidates the saved work locally with the current checker, and if it changes the checkpoint it first writes a `state-before-recovery-<stamp>.json` backup into `support/recovery/`. If local repairs finish the work, export proceeds with no credentials and no model calls. Otherwise it asks for the key again and continues only the unresolved items or the pending audit.

A saved audit verdict is consumed rather than replaced by a fresh whole-test audit. Audit replacements keep the accepted originals until a replacement passes, and resume preserves the audit verdict, revision round, pending feedback, and pending slot ids separately. Questions repaired during resume re-enter the next targeted audit even if a verdict was already saved. Older checkpoints migrate their audit scope from the saved findings, and older run folders are reorganized into the current layout without touching saved questions or finished documents.

The call budget is an application limit, separate from the provider's own quota. The default is 400 per invocation, and a clean run costs roughly `3 × question_count + 2` calls. Revisions, model repairs, and audit replacements add more. A restart receives a fresh budget for the new invocation. The default revision limits are 2 item revisions and 1 audit replacement round.

Parallel workers run up to half the question count (rounded up) at once, capped by local provider/model heuristics. These values do not guarantee compliance with account-specific quotas. If two workers accept the same stem, the pipeline detects the duplicate, retries that slot sequentially with fresh context, and can also repair duplicate stems found in older checkpoints.

## Providers and models

One provider, one model ID, and one key serve all five roles. Each provider is OpenAI-compatible unless noted, and has an environment prefix for unattended runs.

| Provider | Prefix | Notes |
| --- | --- | --- |
| OpenCode Go | `OPENCODE` | default; picks Chat Completions, Messages, or Responses per model family |
| DeepSeek direct | `DEEPSEEK` | |
| OpenAI | `OPENAI` | |
| Anthropic | `ANTHROPIC` | Messages API |
| Google Gemini | `GOOGLE` | |
| xAI (Grok) | `XAI` | |
| Mistral | `MISTRAL` | |
| Groq | `GROQ` | |
| OpenRouter | `OPENROUTER` | |
| Together | `TOGETHER` | |
| Fireworks | `FIREWORKS` | |
| DeepInfra | `DEEPINFRA` | |
| Perplexity | `PERPLEXITY` | |
| Cohere | `COHERE` | |
| Moonshot (Kimi) | `MOONSHOT` | |
| Zhipu GLM | `ZHIPU` | |
| Qwen (DashScope) | `QWEN` | |
| Cerebras | `CEREBRAS` | |
| SambaNova | `SAMBANOVA` | |
| NVIDIA NIM | `NVIDIA` | |
| Novita | `NOVITA` | |
| Hyperbolic | `HYPERBOLIC` | |
| GitHub Models | `GITHUB` | |
| Ollama | `OLLAMA` | local, no key |
| Custom | `CUSTOM` | base URL from `CUSTOM_BASE_URL` |

Set `<PREFIX>_MODEL` and `<PREFIX>_API_KEY` to skip the prompts. The API key is read from a hidden prompt or the environment and used in request headers. The app omits those headers from logs, but records payloads and provider responses. It does not load `.env` files automatically. OpenCode Go strips a leading `opencode-go/` from the model ID and sends an application user agent plus a stable session header. The custom provider requires `CUSTOM_BASE_URL`. Ollama needs no key.

Concurrency is selected in `provider.py` from model-family and provider tables. DeepSeek has a separate branch; other recognized families use their table value, then unmatched models use the provider fallback or the default of 4. These are local heuristics, not live quota checks. There is no artificial requests-per-minute throttle. Truncated completions and malformed envelopes are retried within a bounded attempt count, and a complete fenced JSON object is accepted while malformed JSON is never guessed at.

## Command reference

```text
gaussian-prep generate [request] [--dataset PATH] [--prep-material NAME] [--out DIR]
                       [--provider NAME] [--model ID] [--format pdf|docx|both|none]
                       [--max-calls N] [--revisions N] [--audit-revisions N]

gaussian-prep inspect [dataset] [--report PATH] [--prep-material NAME]

gaussian-prep resume RUN_DIR [--format pdf|docx|both|none]
                    [--max-calls N] [--revisions N] [--audit-revisions N]
```

`generate` is the default subcommand. `--dataset` can also come from `GAUSSIAN_DATASET` (or the older `SATPREP_DATASET`). `--provider`, `--model`, and `--format` are asked interactively when omitted; `--format` defaults to `none` when the session is not a terminal. Run folders are named `runs/<YYYYmmdd-HHMMSS-microseconds>` unless `--out` is given. The request guide is `PROMPTS.md` in the working directory.

## Limits

- The whole-test auditor is exercised in tests with scripted responses, not against a live account, so wording quality, difficulty estimates, and provider quotas have not been measured end to end.
- The concurrency values are local heuristics, not a guarantee of your account's current allowance.
- The notation validator catches the reproduced failure modes. It is not a complete TeX parser: extremely wide expressions, unusual macros, oversized tables, and non-Latin scripts are outside the visually checked sample.
- The fallback PDF fonts and plain-text substitutions cover a narrower glyph range than a fully configured Unicode TeX installation.
- "Complete" means every item passed its computation or independent review and the whole-test audit ran. It does not mean the auditor approved every coverage or workload preference; leftover suggestions become warnings.
- Math difficulty labels are estimates until there is student-performance evidence. The app does not claim official exam provenance, adaptive routing, or empirical calibration.
