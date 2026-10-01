import json

import httpx
import pytest

from conftest import ScriptedProvider, question
from gaussian_prep.export import _latex_text, _segments, build_docx, build_pdf, render_latex
from gaussian_prep.models import Question
from gaussian_prep.pipeline import Pipeline
from gaussian_prep.provider import ModelProvider
from gaussian_prep.render import text_html
from gaussian_prep.typesetting import math_png
from gaussian_prep.verification import compute


SOURCE = r"If \(2x + 5 = 19\), what is the value of \(x\)?"


def test_provider_decodes_json_once_and_exports_identical_latex(tmp_path, monkeypatch):
    q = question(rhs=19)
    q.stem = r"Find \(\frac{\sqrt{2}}{3}\), with \(50\%\) and \(\text{two words}\)."
    wire = q.model_dump_json()
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": wire}}]}))
    received = ModelProvider("test-model", "test-key", tmp_path).ask("generator", {}, Question)
    assert received.stem == q.stem
    assert _latex_text(received.stem) == q.stem
    assert _segments(received.stem)[1] == ("math", r"\frac{\sqrt{2}}{3}")
    # Logging uses JSON escaping; loading that log does not introduce another layer.
    log = json.loads(next(tmp_path.glob("*.json")).read_text())
    recorded = json.loads(log["response"]["choices"][0]["message"]["content"])
    assert recorded["stem"] == received.stem


@pytest.mark.parametrize("bad", [
    r"If \\(2x + 5 = 19\\), find \\(x\\).",
    r"If \\\(2x + 5 = 19\\\), find \\\(x\\\).",
    r"Find \(\\frac{1}{2}\).",
    r"Find \(x^10\).",
    r"Find \(50%\).",
    "Find \\(" + "\f" + "rac{1}{2}\\).",
])
def test_malformed_source_returns_to_author_without_local_rewrite(corpus, tmp_path, bad):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            result = super().ask(role, payload, schema)
            if role == "generator":
                result = question(rhs=19)
                result.stem = bad if self.generated["q1"] == 1 else SOURCE
            return result

    provider = Provider()
    state = Pipeline(corpus, provider, tmp_path, compute=compute, progress=lambda _: None).run("Generate 1 question")
    generations = [payload for role, payload in provider.calls if role == "generator"]
    assert len(generations) == 2
    assert generations[1]["previous_draft"]["stem"] == bad
    assert generations[1]["revision_feedback"][0]["formatting_error"]
    assert state["items"]["q1"]["question"]["stem"] == SOURCE
    assert not state["items"]["q1"].get("formatting_repairs")
    assert len([role for role, _ in provider.calls if role == "verifier"]) == 1
    assert SOURCE in render_latex(state)
    # Save/resume/export all preserve exactly the author's accepted math string.
    resumed_provider = ScriptedProvider()
    resumed = Pipeline(corpus, resumed_provider, tmp_path, compute=compute, progress=lambda _: None).run("Generate 1 question", resume=True)
    assert resumed["items"]["q1"]["question"]["stem"] == SOURCE
    assert not resumed_provider.calls


@pytest.mark.parametrize("source", [
    r"\(\left(\frac{1}{\sqrt{2}}\right)^{10}\)",
    r"\(\text{two words}\quad 50\%\)",
    r"\[\dfrac{x^{-12}}{2}+\overline{AB}\].",
    r"\(x \leq 5\), \(y \neq 3\) and \(\{1,2\}\)",
])
def test_supported_math_is_never_rewritten(source):
    assert _latex_text(source) == source
    assert "<math" in text_html(source)


def test_image_typesetter_receives_verbatim_math(monkeypatch):
    import gaussian_prep.typesetting as module
    expression = r"\text{leave these spaces}\;\left(\dfrac{19}{23}\right)"
    captured = []
    original = module.math_to_image

    def capture(source, *args, **kwargs):
        captured.append(source)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(module, "math_to_image", capture)
    math_png.cache_clear()
    assert math_png(expression).startswith(b"\x89PNG")
    assert captured == ["$" + expression + "$"]


@pytest.mark.parametrize("builder,suffix", [(build_pdf, ".pdf"), (build_docx, ".docx")])
def test_export_never_strips_unsupported_math(tmp_path, builder, suffix):
    from test_export import state
    st = state()
    st["items"]["q1"]["question"]["stem"] = r"Find \(\tfrac{1}{2}\)."
    original = st["items"]["q1"]["question"]["stem"]
    path = tmp_path / ("invalid" + suffix)
    with pytest.raises(ValueError, match="typeset unchanged"):
        builder(st, path)
    assert not path.exists()
    assert st["items"]["q1"]["question"]["stem"] == original


def test_source_version_two_is_not_silently_repaired_on_resume(corpus, tmp_path):
    state = Pipeline(corpus, ScriptedProvider(), tmp_path, compute=compute, progress=lambda _: None).run("Generate 1 question")
    state["items"]["q1"]["question"]["stem"] = r"Find \\(x\\)."
    original = state["items"]["q1"]["question"]["stem"]
    (tmp_path / "state.json").write_text(json.dumps(state))
    from gaussian_prep.recovery import recover_saved_run
    recovered = recover_saved_run(corpus, tmp_path, compute=compute)
    assert recovered["status"] == "needs_attention"
    assert "q1" in recovered["pending_audit_feedback"]
    assert recovered["items"]["q1"]["question"]["stem"] == original
