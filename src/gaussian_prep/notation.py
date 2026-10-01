"""Conservative, shared repairs to presentation syntax; never invent mathematical content."""
from __future__ import annotations

import re

from .models import Question

MATH = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]", re.S)
COMMANDS = r"(?:frac|dfrac|tfrac|sqrt|cdot|times|div|pi|theta|alpha|beta|gamma|Delta|leq?|geq?|neq?|text|mathrm|mathbf|left|right|sin|cos|tan|log|ln|angle|overline|overrightarrow|pm|circ|infty|displaystyle|quad|qquad)\b"


def normalize_text(text: str) -> str:
    # A decoded JSON string can still contain two backslashes. Repair delimiters and
    # known commands, but preserve TeX row breaks such as \\ in cases/arrays.
    text = re.sub(r"\\{2,}(?=[()[\]])", lambda _: "\\", text)
    text = re.sub(r"\\{2,}(?=" + COMMANDS + ")", lambda _: "\\", text)
    text = re.sub(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", lambda m: r"\[" + m[1] + r"\]", text, flags=re.S)

    def dollar(match):
        body = match[1].strip()
        # Prices such as '$5 and $10' and '$5 + $10' are not math delimiters.
        if text[match.end():match.end() + 1].isdigit() or re.search(r"[+*/=^-]$", body):
            return match[0]
        if re.fullmatch(r"[A-Za-z]|[\d., +-]+", body) or re.search(r"\\[A-Za-z]|[=^_]", body):
            return r"\(" + body + r"\)"
        return match[0]

    text = re.sub(r"(?<!\\)\$([^$\n]+)(?<!\\)\$", dollar, text)

    def math(match):
        inline = match[1] is not None
        body = match[1] if inline else match[2]
        body = re.sub(r"\\{2,}(?=[%#$&])", lambda _: "\\", body)
        # A JSON '\f' escape can turn an intended \frac into a form-feed character.
        body = body.replace("\f" + "rac", r"\frac")
        # These unbraced numeric scripts are an unambiguous presentation repair.
        body = re.sub(r"(?<!\\)([\^_])(-?\d{2,}|[-+]\d+)(?![\d.])", r"\1{\2}", body)
        return (r"\(" if inline else r"\[") + body + (r"\)" if inline else r"\]")

    return MATH.sub(math, text)


def prepare_question(question: Question) -> tuple[Question, list[dict]]:
    result = question.model_copy(deep=True)
    changes = []

    def repair(path, value):
        updated = normalize_text(value)
        if updated != value:
            changes.append({"field": path, "before": value, "after": updated})
        return updated

    result.stem = repair("stem", result.stem)
    for choice in result.choices:
        choice.text = repair(f"choices.{choice.label}", choice.text)
    if result.table:
        result.table.headers = [repair(f"table.headers.{i}", value) for i, value in enumerate(result.table.headers)]
        result.table.rows = [[repair(f"table.rows.{r}.{c}", value) for c, value in enumerate(row)] for r, row in enumerate(result.table.rows)]
    return result, changes


def is_formatting_issue(issue: dict) -> bool:
    if issue.get("dimension") != "wording_consistency":
        return False
    if issue.get("repair_kind") == "content":
        return False
    # Compatibility with saved audits from before repair_kind existed. Restrict to
    # serialization claims, not general wording, units, or mathematical notation.
    reason = issue.get("reason", "").lower()
    serialization = any(term in reason for term in ("double-escaped", "double escaped", "double backslash", "doubled backslash", "latex escaping"))
    syntax = any(term in reason for term in ("unbalanced braces", "unmatched math delimiter", "unknown latex command", "bare latex"))
    return serialization or (issue.get("repair_kind") == "formatting" and syntax)
