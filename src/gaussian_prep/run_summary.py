"""A readable, offline overview of a saved test; no new model judgments."""
from collections import Counter
from datetime import datetime, timezone
from html import escape
from itertools import groupby
from pathlib import Path


def write_summary(state: dict, run_dir: Path) -> Path:
    plan = state.get("plan") or {}
    structure = plan.get("structure") or {}
    section_specs = {section["module"]: section for section in structure.get("sections", [])}
    slots = sorted(plan.get("slots", []), key=lambda slot: (slot["module"], slot["position"]))
    items = state.get("items", {})
    complete = state.get("status") == "complete"
    title = escape(plan.get("title") or "Your practice")
    status = "Ready" if complete else "Saved · resume to continue"
    audit = state.get("audit") or {}
    issues = (state.get("audit_warnings") or audit).get("issues", [])
    pending = state.get("pending_audit_feedback") or state.get("audit_work", {}).get("phase", "done") != "done"
    if not audit:
        audit_status = "Not yet completed"
    elif not complete or pending:
        audit_status = "Follow-up pending"
    elif issues or not audit.get("accepted"):
        audit_status = "Completed with notes"
    else:
        audit_status = "Passed"

    documents = []
    if complete:
        for name, label in [("questions.pdf", "Questions · PDF"), ("answer-key.pdf", "Answer key · PDF"), ("questions.docx", "Questions · Word"), ("answer-key.docx", "Answer key · Word")]:
            if (run_dir / name).is_file():
                documents.append(f'<a class="document" href="{name}">{label} <span>↗</span></a>')
    documents_html = "".join(documents) or '<p class="muted">No documents exported yet. Resume this run to choose an export format.</p>'

    module_rows = []
    minutes = plan.get("minutes_per_module", [])
    for module in sorted({slot["module"] for slot in slots}):
        group = [slot for slot in slots if slot["module"] == module]
        formats = Counter(slot["format"] for slot in group)
        duration = str(minutes[module - 1]) + " min" if module <= len(minutes) else "—"
        name = section_specs.get(module, {}).get("name", f"Module {module}")
        ranges = []
        position = 1
        for fmt, chunk in groupby(slot["format"] for slot in group):
            count = len(list(chunk))
            label = str(position) if count == 1 else f"{position}–{position + count - 1}"
            ranges.append(f"{label}: {fmt}")
            position += count
        sequence = escape("; ".join(ranges))
        module_rows.append(f'<tr><th>{escape(name)}</th><td>{len(group)}</td><td>{formats["MCQ"]} multiple choice · {formats["SPR"]} numeric · {formats["FRQ"]} written<br><small>{sequence}</small></td><td>{escape(duration)}</td></tr>')

    def breakdown(field):
        counts = Counter(slot[field] for slot in slots)
        return "".join(f'<li><span>{escape(name)}</span><strong>{count}</strong></li>' for name, count in counts.items()) or '<li>Available after planning.</li>'

    numbering = {slot["id"]: str(index) for index, slot in enumerate(slots, 1)}
    notes = []
    for issue in issues:
        targets = ", ".join(numbering.get(sid, sid) for sid in issue.get("slot_ids", []))
        label = "Questions " + targets if targets else "Whole test"
        notes.append(f'<li><strong>{escape(label)}</strong> — {escape(issue.get("reason", ""))}</li>')
    notes_html = '<section class="notes"><h2>Audit notes</h2><ul>' + "".join(notes) + '</ul></section>' if notes else ""
    error_html = '<p class="notes">' + escape(str(state["error"])) + '</p>' if state.get("error") and not complete else ""
    computed = sum(item.get("verification", {}).get("status") == "pass" for item in items.values())
    reviewed = sum(item.get("verification", {}).get("status") == "unverified" and item.get("mathematical_model", {}).get("mode") == "unsupported" for item in items.values())
    checks = f"{computed} question(s) have a passing computational check. {reviewed} use the item reviewer's independent solution because the computation engine does not model that task."
    audit_text = escape(audit.get("summary") or "The whole-test audit runs after the question checks.")
    requested = escape(state.get("request", ""))
    structure_html = ""
    if structure:
        kind = "Mock exam" if structure.get("kind") == "mock" else "Custom practice"
        style = "".join(f"<li>{escape(value)}</li>" for value in structure.get("question_style", []))
        structure_html = f'<section><h2>{kind}</h2><p>{escape(structure.get("target", ""))}</p><p>{escape(structure.get("basis", ""))}</p>' + (f"<ul>{style}</ul>" if style else "") + '</section>'
    created = str(state.get("created_at") or "Not recorded")
    try:
        timestamp = datetime.fromisoformat(created)
        created = timestamp.astimezone(timezone.utc).strftime("%d %B %Y · %H:%M UTC") if timestamp.tzinfo else timestamp.strftime("%d %B %Y · %H:%M")
    except ValueError:
        pass
    created = escape(created)
    path = run_dir / "summary.html"
    run_dir.mkdir(parents=True, exist_ok=True)
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{title} — Gaussian Prep</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f5f1;color:#20382d;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:980px;margin:48px auto;padding:0 28px}}.brand{{font-size:12px;letter-spacing:.17em;text-transform:uppercase;font-weight:700}}
header{{border-bottom:1px solid #cdd5cc;padding-bottom:28px;margin-bottom:28px}}h1{{font:normal 42px/1.15 Georgia,serif;margin:18px 0}}h2{{font-size:18px;margin:0 0 14px}}
.muted,footer{{color:#59695e}}.status{{display:inline-block;background:#e2ebdf;border-radius:20px;padding:5px 14px;font-size:14px}}
.stats,.columns{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin:24px 0}}.stat,section{{background:#fff;border:1px solid #dce2d8;border-radius:12px;padding:24px}}
.stat strong{{display:block;font:32px/1.2 Georgia,serif;margin-bottom:7px}}.stat span{{color:#59695e;font-size:14px}}section{{margin:20px 0}}
.documents{{display:flex;flex-wrap:wrap;gap:12px}}.document{{text-decoration:none;background:#254e3b;color:white;padding:12px 18px;border-radius:7px}}.document:hover{{background:#183927}}.document:focus-visible{{outline:3px solid #be902d;outline-offset:3px}}.document span{{margin-left:18px}}
table{{width:100%;border-collapse:collapse;text-align:left}}th,td{{padding:12px 10px;border-bottom:1px solid #e3e7e0}}thead th{{font-size:13px;color:#59695e}}
.columns{{grid-template-columns:1fr 1fr}}.columns section{{margin:0}}.breakdown{{list-style:none;padding:0;margin:0}}.breakdown li{{display:flex;justify-content:space-between;gap:16px;padding:7px 0}}
.notes{{background:#fff5dd;border-color:#e7dcbf}}details{{border-top:1px solid #e3e7e0;padding-top:14px;margin-top:18px}}summary{{cursor:pointer;font-weight:600}}.request{{white-space:pre-wrap}}footer{{font-size:13px;padding:16px 0 32px}}
@media(max-width:640px){{main{{margin:24px auto;padding:0 16px}}h1{{font-size:32px}}.stats,.columns{{grid-template-columns:1fr}}section,.stat{{padding:18px}}.table-wrap{{overflow-x:auto}}}}
@media print{{body{{background:white}}main{{margin:0;max-width:none}}section,.stat{{break-inside:avoid}}.document{{background:white;color:#20382d;border:1px solid}}details{{display:none}}}}
</style></head><body><main>
<header><div class="brand">Gaussian Prep · Test overview</div><h1>{title}</h1><span class="status">{status}</span></header>
<div class="stats"><div class="stat"><strong>{len(items)} / {escape(str(plan.get("total_questions", 0)))}</strong><span>Questions saved</span></div>
<div class="stat"><strong>{escape(str(plan.get("module_count", 0)))}</strong><span>Modules</span></div><div class="stat"><strong>{audit_status}</strong><span>Whole-test audit</span></div></div>
{error_html}<section><h2>Your documents</h2><div class="documents">{documents_html}</div></section>
{structure_html}
<section><h2>Test structure</h2><div class="table-wrap"><table><thead><tr><th>Module</th><th>Questions</th><th>Format</th><th>Time</th></tr></thead><tbody>{"".join(module_rows)}</tbody></table></div></section>
<div class="columns"><section><h2>Topics</h2><ul class="breakdown">{breakdown("domain")}</ul></section><section><h2>Planned difficulty</h2><ul class="breakdown">{breakdown("difficulty")}</ul></section></div>
<section><h2>Whole-test audit · {audit_status}</h2><p>{audit_text}</p><details><summary>Question-level checks</summary><p>{checks}</p><p>Question checks and the whole-test audit are separate stages of the same review process.</p></details></section>{notes_html}
<section><h2>Your request</h2><p class="request">{requested}</p></section>
<footer>Created: {created}<br>Keep the support folder with this run to resume work or create more exports. Open this summary in any browser; it works offline.</footer>
</main></body></html>'''
    temporary = path.with_suffix(".html.tmp")
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(path)
    return path
