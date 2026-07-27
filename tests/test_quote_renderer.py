"""Tests for src/quote_renderer.py — native on-device quote rendering.

Small panels can't use the pre-rendered 800x400 corpus art (downscaling
shreds the body text at 1-bit), so the quote is laid out natively at the
panel's own size. These lock the contracts that make that readable:
auto-fit sizing, time-phrase emboldening, attribution fitting, and
staying inside the box.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

_HAS_PIL = True
try:
    from PIL import Image  # noqa: F401
except ImportError:
    _HAS_PIL = False

pytestmark = pytest.mark.skipif(not _HAS_PIL, reason="PIL not available")

if _HAS_PIL:
    import quote_renderer

FONT = str(REPO_ROOT / "fonts" / "Literata72pt-Regular.ttf")
FONT_BOLD = str(REPO_ROOT / "fonts" / "Literata72pt-Black.ttf")

QUOTE = "Major Riddle cut him short. 'What time was that?' 'It was exactly eight minutes past eight, sir.'"
TIMESTRING = "eight minutes past eight"
TITLE = "Murder in the Mews"
AUTHOR = "Agatha Christie"

# 2.7" panel quote area: full width, panel height minus the top strip.
SMALL = (264, 147)


def _render(size=SMALL, quote=QUOTE, timestring=TIMESTRING, title=TITLE, author=AUTHOR):
    return quote_renderer.render_quote(size, quote, timestring, title, author, FONT, FONT_BOLD)


def _ink_columns(img):
    """Bounding box of black pixels, or None when the image is blank."""
    return img.point(lambda p: 255 - p).getbbox()


class TestBoldWords:
    def test_timestring_words_are_bold(self):
        words = quote_renderer._bold_words(QUOTE, TIMESTRING)
        bold = [w for w, is_bold in words if is_bold]
        assert bold == ["eight", "minutes", "past", "eight,"]

    def test_case_insensitive_match(self):
        words = quote_renderer._bold_words("It was MIDNIGHT then", "midnight")
        assert [w for w, b in words if b] == ["MIDNIGHT"]

    def test_absent_timestring_bolds_nothing(self):
        words = quote_renderer._bold_words(QUOTE, "not in the text")
        assert not any(b for _, b in words)

    def test_empty_timestring_is_safe(self):
        words = quote_renderer._bold_words(QUOTE, "")
        assert words and not any(b for _, b in words)

    def test_empty_quote_returns_empty(self):
        assert quote_renderer._bold_words("", TIMESTRING) == []


class TestRender:
    def test_returns_1bit_image_of_requested_size(self):
        img = _render()
        assert img.size == SMALL
        assert img.mode == "1"

    def test_ink_stays_inside_the_box(self):
        bbox = _ink_columns(_render())
        assert bbox is not None, "nothing was drawn"
        left, top, right, bottom = bbox
        assert left >= 0 and top >= 0
        assert right <= SMALL[0] and bottom <= SMALL[1]

    def test_empty_quote_renders_blank_not_crash(self):
        img = _render(quote="")
        assert _ink_columns(img) is None

    def test_missing_attribution_is_omitted(self):
        assert _render(title="", author="").size == SMALL

    @pytest.mark.parametrize("size", [(264, 147), (250, 100), (400, 250), (648, 400), (800, 400)])
    def test_fits_a_range_of_panel_geometries(self, size):
        bbox = _ink_columns(_render(size=size))
        assert bbox is not None
        assert bbox[2] <= size[0] and bbox[3] <= size[1]

    def test_pathological_quote_still_bounded(self):
        """A quote far longer than any panel can hold must clip cleanly
        rather than overprint past the edge."""
        long_quote = " ".join(["interminable"] * 400)
        bbox = _ink_columns(_render(size=SMALL, quote=long_quote, timestring=""))
        assert bbox is not None
        assert bbox[2] <= SMALL[0] and bbox[3] <= SMALL[1]

    def test_unbreakable_word_does_not_escape(self):
        bbox = _ink_columns(_render(size=SMALL, quote="a" * 200, timestring=""))
        assert bbox is not None
        assert bbox[3] <= SMALL[1]

    def test_larger_panel_uses_larger_type(self):
        """Auto-fit must scale up: the same quote on a bigger canvas should
        occupy more vertical ink, not render tiny in the corner."""
        small_bbox = _ink_columns(_render(size=(264, 147)))
        large_bbox = _ink_columns(_render(size=(528, 294)))
        assert large_bbox[3] > small_bbox[3]


class TestCredits:
    def test_full_attribution_preferred_over_truncation(self):
        """The renderer shrinks the credits face before ellipsizing —
        'Murder in the Mews, Agatha Christie' reads better a point smaller
        than truncated to 'Agatha C…'."""
        from PIL import ImageDraw

        probe = ImageDraw.Draw(Image.new("1", (10, 10)))
        text = f"— {TITLE}, {AUTHOR}"
        fitted, _font, _size = quote_renderer._fit_credits(text, probe, FONT, 14, 244)
        assert fitted == text
        assert quote_renderer.ELLIPSIS not in fitted

    def test_truncates_when_even_the_floor_overflows(self):
        from PIL import ImageDraw

        probe = ImageDraw.Draw(Image.new("1", (10, 10)))
        fitted, _font, _size = quote_renderer._fit_credits("— " + "x" * 300, probe, FONT, 14, 60)
        assert fitted.endswith(quote_renderer.ELLIPSIS)
