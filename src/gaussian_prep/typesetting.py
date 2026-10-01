"""Typeset the author's LaTeX verbatim, shared by validation and image exporters."""
from functools import lru_cache
from io import BytesIO
from math import ceil, isfinite
import threading

import matplotlib
from matplotlib.font_manager import FontProperties
from matplotlib.mathtext import MathTextParser, math_to_image

from .latex_safety import check_math_source

RENDER_LOCK = threading.RLock()
_DPI = 200
_MAX_AXIS = 8192
_MAX_PIXELS = 4_000_000
_LAYOUT = MathTextParser("path")


def _check_layout(expression: str, prop: FontProperties | None = None) -> None:
    # Match math_to_image's vector pass. No bitmap is allocated here.
    # Explicit properties bind the parser's cache to the current font settings.
    layout = _LAYOUT.parse("$" + expression + "$", dpi=72, prop=prop if prop is not None else FontProperties())
    if not all(isfinite(v) for v in (layout.width, layout.height, layout.depth)):
        raise ValueError("Non-finite math layout")
    if layout.width <= 0 or layout.height <= 0:
        raise ValueError("Math layout must have positive dimensions")
    xs, ys = [0, layout.width], [0, layout.height]
    for glyph in layout.glyphs:
        # Matplotlib 3.x exposes five- or six-element glyph tuples.
        font, size = glyph[:2]
        x, y = glyph[-2:]
        scale = size / font.units_per_EM
        left, bottom, right, top = (v * scale for v in font.bbox)
        xs.extend((x + left, x + right))
        ys.extend((y + bottom, y + top))
    for x, y, width, height in layout.rects:
        xs.extend((x, x + width))
        ys.extend((y, y + height))
    if not all(isfinite(v) for v in (*xs, *ys)):
        raise ValueError("Non-finite math layout")
    # Include positioned ink: negative spacing/overlaps can hide it from the
    # layout's advance width. Padding covers pixel rounding in the raster pass.
    width = ceil((max(xs) - min(xs)) * _DPI / 72) + 8
    ink_height = max(ys) - min(ys)
    # Mathtext's raster baseline padding can exceed its ink span when boxes
    # overlap. Bound that intermediate bitmap as well as the final figure.
    height = ceil(max(ink_height, 2 * ink_height - layout.height) * _DPI / 72) + 16
    if max(width, height) > _MAX_AXIS or width * height > _MAX_PIXELS:
        raise ValueError("Math image exceeds the rendering size limit")


@lru_cache(maxsize=1024)
def math_png(expression: str, fontsize: float = 12.0) -> bytes:
    try:
        check_math_source(expression)
        if not isfinite(fontsize) or not 0 < fontsize <= 72:
            raise ValueError("Font size is outside the rendering limit")
        # Matplotlib's font/parser state is shared across parallel question workers.
        with RENDER_LOCK, matplotlib.rc_context({"mathtext.fontset": "cm", "mathtext.default": "it", "font.family": "serif", "font.size": fontsize, "text.usetex": False, "text.parse_math": True, "savefig.bbox": None, "savefig.pad_inches": 0}):
            prop = FontProperties(size=fontsize)
            _check_layout(expression, prop)
            buffer = BytesIO()
            math_to_image("$" + expression + "$", buffer, prop=prop, dpi=_DPI, format="png")
        return buffer.getvalue()
    except Exception as exc:
        raise ValueError(f"LaTeX cannot be typeset unchanged: {expression[:100]}. {exc}. Use supported commands such as \\frac, \\sqrt, \\text, \\leq, \\geq, and \\neq; do not double-escape the decoded source.") from exc
