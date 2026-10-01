from __future__ import annotations

from html import escape
import re

from latex2mathml.converter import convert

from .models import Figure, Question
from .notation import MATH, normalize_text
from .typesetting import math_png


def _check_plain_text(text: str) -> None:
    if re.search(r"\\[()[\]]", text):
        raise ValueError(r"Unmatched math delimiter; use paired \(...\) or \[...\].")
    if re.search(r"\\[A-Za-z]+|[\^_]", re.sub(r"_{2,}", "", text)):
        raise ValueError(r"Bare LaTeX outside math; wrap the expression in \(...\).")
    if "$$" in text:
        raise ValueError(r"Use \[...\] for display math instead of dollar delimiters.")


def _check_math(expression: str) -> None:
    if not expression.strip():
        raise ValueError("Empty math expression")
    if re.search(r"(?<!\\)[%&#$]", expression):
        raise ValueError(r"Escape literal %, &, #, and $ inside math with one LaTeX backslash.")
    if not expression.isascii():
        raise ValueError(r"Use LaTeX commands for math symbols (for example \theta and \leq) so the same source works in every exporter.")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", expression):
        raise ValueError("Invalid control character in math; correctly escape LaTeX backslashes in JSON.")
    if re.search(r"\\[()[\]]", expression):
        raise ValueError("Nested or mismatched math delimiters")
    if re.search(r"\\(?:begin|end)\b", expression):
        raise ValueError("Write cases, systems, and aligned equations as separate inline expressions with their conditions; math environments are not supported by every exporter.")
    depth = 0
    # Consume escaped characters as a unit so \{ and \} are literal braces.
    for token in re.findall(r"\\.|[{}]", expression):
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced braces in math expression")
    if depth:
        raise ValueError("Unbalanced braces in math expression")
    if re.search(r"(?<!\\)[\^_](?:\d{2,}|[-+]\w)", expression):
        raise ValueError("Use braces around multi-character exponents and subscripts, for example x^{10}.")


def text_html(text: str) -> str:
    # Validation never rewrites authored source. Legacy checkpoint repair is separate.
    if re.search(r"\\{2,}", text) or normalize_text(text) != text:
        raise ValueError(r"Supply LaTeX directly with single backslashes after JSON decoding, paired \(...\) or \[...\], and braced multi-character powers. Do not double-escape LaTeX or use dollar math delimiters.")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise ValueError("Invalid control character; escape LaTeX backslashes once for JSON transport.")
    parts, end = [], 0
    for match in MATH.finditer(text):
        _check_plain_text(text[end:match.start()])
        parts.append(escape(text[end:match.start()]).replace("\n", "<br>"))
        expression = match.group(1) if match.group(1) is not None else match.group(2)
        _check_math(expression)
        math_png(expression)
        try:
            mathml = convert(expression, display="inline" if match.group(1) else "block")
            # Accept only MathML markup produced by the converter, never injected raw HTML.
            import xml.etree.ElementTree as ET
            tree = ET.fromstring(mathml)
            for node in tree.iter():
                tag = node.tag.rsplit("}", 1)[-1]
                if tag not in {"math", "mrow", "mi", "mn", "mo", "mtext", "mspace", "msup", "msub", "msubsup", "mfrac", "msqrt", "mroot", "mover", "munder", "munderover", "mtable", "mtr", "mtd", "mstyle", "mpadded", "mphantom", "menclose", "mfenced", "mmultiscripts", "mprescripts", "none"}:
                    raise ValueError("Unsupported MathML element")
                if any(k.lower().startswith("on") or k.lower() in {"href", "src", "style"} for k in node.attrib):
                    raise ValueError("Unsupported MathML attribute")
                if tag in {"mi", "mo"} and re.search(r"\\[A-Za-z]+", node.text or ""):
                    raise ValueError("Unknown LaTeX command")
            parts.append(mathml)
        except Exception as exc:
            raise ValueError(f"Math could not be typeset: {expression[:100]}") from exc
        end = match.end()
    _check_plain_text(text[end:])
    parts.append(escape(text[end:]).replace("\n", "<br>"))
    return "".join(parts)


def figure_svg(figure: Figure) -> str:
    points = {p.label: p for p in figure.points}
    xs = [p.x for p in figure.points] + [v for c in figure.circles for v in (c.x - c.radius, c.x + c.radius)]
    ys = [p.y for p in figure.points] + [v for c in figure.circles for v in (c.y - c.radius, c.y + c.radius)]
    if figure.show_axes:
        xs.append(0)
        ys.append(0)
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    scale = min(360 / max(xmax - xmin, 1), 240 / max(ymax - ymin, 1))
    xmid, ymid = (xmin + xmax) / 2, (ymin + ymax) / 2
    def x(v):
        return 220 + (v - xmid) * scale
    def y(v):
        return 160 - (v - ymid) * scale
    parts = [f'<svg viewBox="0 0 440 320" role="img" xmlns="http://www.w3.org/2000/svg"><title>{escape(figure.description)}</title><g fill="none" stroke="#20252a" stroke-width="1.8">']
    if figure.show_axes:
        parts += [f'<path stroke="#aab1b7" d="M 20 {y(0):.2f} H 420 M {x(0):.2f} 20 V 300"/>']
    for segment in figure.segments:
        p, q = points[segment.start], points[segment.end]
        parts.append(f'<line x1="{x(p.x):.2f}" y1="{y(p.y):.2f}" x2="{x(q.x):.2f}" y2="{y(q.y):.2f}"/>')
    for circle in figure.circles:
        parts.append(f'<circle cx="{x(circle.x):.2f}" cy="{y(circle.y):.2f}" r="{circle.radius * scale:.2f}"/>')
    parts.append('</g><g fill="#20252a" font-family="Georgia,serif" font-size="14">')
    for p in figure.points:
        parts.append(f'<circle cx="{x(p.x):.2f}" cy="{y(p.y):.2f}" r="2.6"/><text x="{x(p.x)+8:.2f}" y="{y(p.y)-8:.2f}">{escape(p.label)}</text>')
    parts.append('</g></svg>')
    caption = "Coordinates are stated in the question or table." if figure.show_axes else "Figure not drawn to scale."
    return '<figure>' + "".join(parts) + f'<figcaption>{caption}</figcaption></figure>'


def question_html(question: Question, position: int, answer: bool = False) -> str:
    result = f'<article class="question"><div class="number">{position}</div><div class="question-body"><p>{text_html(question.stem)}</p>'
    if question.table:
        result += '<table><thead><tr>' + ''.join(f'<th>{text_html(h)}</th>' for h in question.table.headers) + '</tr></thead><tbody>'
        result += ''.join('<tr>' + ''.join(f'<td>{text_html(c)}</td>' for c in row) + '</tr>' for row in question.table.rows) + '</tbody></table>'
    if question.figure:
        result += figure_svg(question.figure)
    if question.choices:
        result += '<div class="choices">' + ''.join(f'<div><span class="choice-label">{c.label}</span>{text_html(c.text)}</div>' for c in sorted(question.choices, key=lambda c: c.label)) + '</div>'
    elif not answer:
        result += '<div class="response-box" aria-label="Answer space"></div>'
    if answer:
        result += f'<section class="solution"><strong>Answer: {text_html(question.answer)}</strong></section>'
    return result + '</div></article>'
