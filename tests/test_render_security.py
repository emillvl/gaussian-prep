from types import SimpleNamespace
from unittest.mock import Mock

import matplotlib
import pytest

from conftest import ScriptedProvider
from gaussian_prep import export as exporters
from gaussian_prep import typesetting
from gaussian_prep.pipeline import Pipeline, check_item_format
from gaussian_prep.render import text_html
from gaussian_prep.verification import compute
from gaussian_prep.models import Figure
from test_export import state


@pytest.fixture(autouse=True)
def clear_render_cache():
    typesetting.math_png.cache_clear()
    yield
    typesetting.math_png.cache_clear()


@pytest.mark.parametrize("label", [r"$x\hspace{1000000}x$", r"$\genfrac{}{}{1000000}{}{x}{x}$", r"\input{private.txt}"])
def test_figure_labels_are_literal_on_a_bounded_canvas(monkeypatch, label):
    from io import BytesIO
    from PIL import Image
    from matplotlib.mathtext import MathTextParser

    parse = Mock(side_effect=AssertionError("Figure labels must not enter Mathtext"))
    monkeypatch.setattr(MathTextParser, "parse", parse)
    figure = Figure(description="Point", show_axes=False, points=[{"label": label, "x": 0, "y": 0}], segments=[], circles=[])
    with matplotlib.rc_context({"text.usetex": True, "text.parse_math": True, "savefig.bbox": "tight", "savefig.pad_inches": 100}):
        png = exporters.figure_png(figure)
        assert matplotlib.rcParams["text.usetex"]
        assert matplotlib.rcParams["savefig.bbox"] == "tight"
    with Image.open(BytesIO(png)) as image:
        assert image.size == (880, 640)
    parse.assert_not_called()


def test_oversize_figure_label_is_rejected():
    with pytest.raises(ValueError, match="64 characters"):
        Figure(description="Point", show_axes=False, points=[{"label": "A" * 65, "x": 0, "y": 0}], segments=[], circles=[])


def test_failed_figure_export_closes_its_canvas(monkeypatch):
    from matplotlib.figure import Figure as MatplotlibFigure

    before = exporters.plt.get_fignums()
    monkeypatch.setattr(MatplotlibFigure, "savefig", Mock(side_effect=OSError("disk full")))
    figure = Figure(description="Point", show_axes=False, points=[{"label": "A", "x": 0, "y": 0}], segments=[], circles=[])
    with pytest.raises(OSError, match="disk full"):
        exporters.figure_png(figure)
    assert exporters.plt.get_fignums() == before


@pytest.mark.parametrize("expression", [
    r"\text{\input audit-canary.txt }",
    r"\text{^^5cinput audit-canary.txt }",
    r"\text{^^^^005cinput audit-canary.txt }",
    r"\text{^^^^^^00005cinput audit-canary.txt }",
    r"\text{{\input{audit-canary.txt}}}",
    r"\text{\csname input\endcsname audit-canary.txt }",
    r"\text{\openin1=audit-canary.txt}",
    r"\text{\write18{anything}}",
    r"\text{\directlua{anything}}",
    r"\text{\catcode1=2}",
    r"\text{\scantokens{anything}}",
    r"\text{\def\x{anything}}",
    r"\text{\fracinput audit-canary.txt}",
    r"\text{\}\input audit-canary.txt}",
    r"\operatorname{\text{\input audit-canary.txt}}",
    "\\text{\\\ninput audit-canary.txt}",
])
def test_unsafe_tex_is_rejected_before_any_raster_or_native_source(monkeypatch, expression):
    raster = Mock(side_effect=AssertionError("Raster must not run"))
    monkeypatch.setattr(typesetting, "math_to_image", raster)
    with pytest.raises(ValueError):
        exporters._latex_text(r"Find \(" + expression + r"\).")
    raster.assert_not_called()


@pytest.mark.parametrize("expression", [
    r"\genfrac{}{}{10000}{}{x\hspace{10000}x}{x}",
    r"x\hspace{10000}x",
    r"x\hspace{10000}x\hspace{-10000}x",
    r"x\hspace{-10000}x\hspace{10000}x",
    r"\llap{x\hspace{10000}x}x",
    r"\rlap{x\hspace{10000}x}x",
    r"\genfrac{}{}{700}{}{x\hspace{200}x}{x}",
])
def test_excessive_layout_is_rejected_before_allocation(monkeypatch, expression):
    raster = Mock(side_effect=AssertionError("Raster must not run"))
    monkeypatch.setattr(typesetting, "math_to_image", raster)
    with pytest.raises(ValueError, match="rendering size limit"):
        typesetting.math_png(expression)
    raster.assert_not_called()


@pytest.mark.parametrize("small,large,spacing", [(12, 72, 100), (12, 15, 220), (11, 13, 240)])
def test_cached_layout_cannot_bypass_limits_at_a_larger_export_font(monkeypatch, small, large, spacing):
    calls = []

    def raster(source, buffer, **kwargs):
        calls.append(kwargs["prop"].get_size_in_points())
        buffer.write(b"\x89PNG")

    monkeypatch.setattr(typesetting, "math_to_image", raster)
    expression = rf"x\hspace{{{spacing}}}x"
    assert typesetting.math_png(expression, fontsize=small) == b"\x89PNG"
    with pytest.raises(ValueError, match="rendering size limit"):
        typesetting.math_png(expression, fontsize=large)
    assert calls == [small]


@pytest.mark.parametrize("field", ["stem", "choice", "header", "cell", "answer"])
@pytest.mark.parametrize("format", ["latex", "pdf", "docx"])
def test_every_export_field_rejects_hidden_commands(tmp_path, monkeypatch, field, format):
    saved = state()
    question = saved["items"]["q1"]["question"]
    source = r"Find \(\text{\input audit-canary.txt }\)."
    if field == "stem":
        question["stem"] = source
    elif field == "choice":
        question["choices"][0]["text"] = source
    elif field == "header":
        question["table"]["headers"][0] = source
    elif field == "cell":
        question["table"]["rows"][0][0] = source
    else:
        question.update(format="SPR", choices=[], answer=source)
        saved["plan"]["slots"][0]["format"] = "SPR"
    compiler = Mock(side_effect=AssertionError("Compiler must not run"))
    monkeypatch.setattr(exporters, "find_latex", lambda: "pdflatex")
    monkeypatch.setattr(exporters.subprocess, "run", compiler)
    builder = {"latex": exporters.build_latex_pdf, "pdf": exporters.build_pdf, "docx": exporters.build_docx}[format]
    path = tmp_path / ("output.docx" if format == "docx" else "output.pdf")
    with pytest.raises(ValueError, match="Unsupported LaTeX command"):
        builder(saved, path, answers=field == "answer")
    compiler.assert_not_called()
    assert not path.exists()


def test_unsafe_generated_question_returns_for_revision(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            result = super().ask(role, payload, schema)
            if role == "generator" and self.generated["q1"] == 1:
                result.stem = r"Find \(\text{\input audit-canary.txt }\)."
            return result

    provider = Provider()
    result = Pipeline(corpus, provider, tmp_path, compute=compute, progress=lambda _: None).run("Generate 1 question")
    assert result["status"] == "complete"
    assert provider.generated == {"q1": 2}
    assert sum(role == "verifier" for role, _ in provider.calls) == 1
    drafts = [payload for role, payload in provider.calls if role == "generator"]
    assert "Unsupported LaTeX command" in drafts[1]["revision_feedback"][0]["formatting_error"]


def test_saved_formatting_pass_cannot_bypass_new_guard():
    artifact = state()["items"]["q1"]
    artifact.update(slot={"position": 1}, formatting_check={"status": "pass", "source_version": 2})
    artifact["question"]["stem"] = r"Find \(\text{^^5cinput audit-canary.txt }\)."
    with pytest.raises(ValueError, match="character escapes"):
        check_item_format(artifact)


@pytest.mark.parametrize("expression", ["x" * 8001, "{" * 65 + "x" + "}" * 65, "x\x7f", "x\\"])
def test_input_bounds_precede_parsing(monkeypatch, expression):
    parse = Mock(side_effect=AssertionError("Parser must not run"))
    monkeypatch.setattr(typesetting._LAYOUT, "parse", parse)
    with pytest.raises(ValueError):
        typesetting.math_png(expression)
    parse.assert_not_called()


@pytest.mark.parametrize("fontsize", [0, -1, 73, float("inf"), float("nan")])
def test_font_bounds_precede_parsing(monkeypatch, fontsize):
    parse = Mock(side_effect=AssertionError("Parser must not run"))
    monkeypatch.setattr(typesetting._LAYOUT, "parse", parse)
    with pytest.raises(ValueError, match="Font size"):
        typesetting.math_png("x", fontsize=fontsize)
    parse.assert_not_called()


@pytest.mark.parametrize("six_fields", [False, True])
def test_positioned_ink_cannot_hide_outside_advance(monkeypatch, six_fields):
    font = SimpleNamespace(units_per_EM=1000, bbox=(-100, -200, 1000, 1000))
    glyph = (font, 12, 120) + ((1,) if six_fields else ()) + (10000, 0)
    monkeypatch.setattr(typesetting._LAYOUT, "parse", lambda *a, **k: SimpleNamespace(width=12, height=12, depth=0, glyphs=[glyph], rects=[]))
    with pytest.raises(ValueError, match="rendering size limit"):
        typesetting._check_layout("x")


@pytest.mark.parametrize("changes", [
    {"width": float("inf")}, {"height": -1},
    {"width": 1000, "height": 1000},
    {"rects": [(10000, 0, 1, 1)]},
    {"rects": [(0, float("nan"), 1, 1)]},
])
def test_invalid_or_oversize_layout_bounds(monkeypatch, changes):
    layout = dict(width=12, height=12, depth=0, glyphs=[], rects=[])
    layout.update(changes)
    monkeypatch.setattr(typesetting._LAYOUT, "parse", lambda *a, **k: SimpleNamespace(**layout))
    with pytest.raises(ValueError):
        typesetting._check_layout("x")


def test_ambient_settings_cannot_invoke_tex_or_expand_image(monkeypatch):
    captured = []

    def raster(source, buffer, **kwargs):
        captured.append(source)
        assert not matplotlib.rcParams["text.usetex"]
        assert matplotlib.rcParams["text.parse_math"]
        assert matplotlib.rcParams["savefig.bbox"] is None
        assert matplotlib.rcParams["savefig.pad_inches"] == 0
        buffer.write(b"\x89PNG")

    monkeypatch.setattr(typesetting, "math_to_image", raster)
    with matplotlib.rc_context({"text.usetex": True, "text.parse_math": False, "savefig.bbox": "tight", "savefig.pad_inches": 100}):
        assert typesetting.math_png(r"\frac{1}{2}") == b"\x89PNG"
        assert matplotlib.rcParams["text.usetex"]
    assert captured == [r"$\frac{1}{2}$"]


@pytest.mark.parametrize("expression", [
    r"\text{two words}\quad 50\%",
    r"\frac{\sqrt{1+\sqrt{2}}}{3}",
    r"\left\{x^{10},x^{-1}\right\}",
    r"\alpha\leq\beta\quad\sin(\theta)",
    "+".join(f"x_{{{i}}}" for i in range(50)),
])
def test_supported_formulas_still_render_verbatim(expression):
    source = r"Find \(" + expression + r"\)."
    assert exporters._latex_text(source) == source
    assert "<math" in text_html(source)
    assert typesetting.math_png(expression, fontsize=13).startswith(b"\x89PNG")


@pytest.mark.parametrize("engine", ["pdflatex", "xelatex", "lualatex"])
def test_native_compiler_explicitly_disables_shell_escape(tmp_path, monkeypatch, engine):
    monkeypatch.setattr(exporters, "find_latex", lambda: engine)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        (tmp_path / "questions.pdf").write_bytes(b"%PDF test")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(exporters.subprocess, "run", run)
    exporters.build_latex_pdf(state(), tmp_path / "questions.pdf")
    assert "-no-shell-escape" in commands[0]
