"""Native (on-device) quote rendering for non-reference panel sizes.

The shipped quote corpus is pre-rendered by ``image-gen/quote_to_image.php``
at 800x400 for the 7.5" panel. Scaling those PNGs down to a small panel
destroys them: at ~33% the body text's thin strokes fall below one pixel
and the 1-bit threshold shreds what's left — only the bold time-phrase
survives (2.7" hardware QA, 2026-07).

Text drawn at the panel's own size stays crisp, because the rasterizer
hints the glyphs for the size actually being displayed. The corpus CSV
carries the quote text, the time phrase to embolden, and the attribution,
so nothing about the pre-rendered art is needed — we lay the quote out
directly.

The 7.5" reference panel keeps using the pre-rendered PNGs (byte-identical
output, matching the corpus's typography); this module drives every other
size. See ``literary_clock._paste_quote_image``.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

# Auto-fit search bounds for the quote body. Descending: the first size
# whose wrap fits the box wins, so quotes render as large as they can.
# The floor is a legibility judgement — below ~9px, 1-bit Literata stops
# being readable at arm's length and we'd rather truncate than show mush.
BODY_SIZE_MAX = 34
BODY_SIZE_MIN = 9
LINE_SPACING = 1.22

# Credits ("— Title, Author") sit under the quote in a smaller face.
CREDITS_RATIO = 0.72
CREDITS_SIZE_MIN = 8
CREDITS_GAP = 4

SIDE_MARGIN_RATIO = 0.04
ELLIPSIS = "…"


def _bold_words(quote: str, timestring: str) -> list[tuple[str, bool]]:
    """Split ``quote`` into ``(word, is_bold)`` pairs, emboldening the words
    overlapping the first case-insensitive occurrence of ``timestring``.

    Character-level flagging (rather than a substring split) keeps the match
    robust when the time phrase starts or ends mid-word; such a word renders
    fully bold, which reads better than a hairline seam inside one word.
    """
    if not quote:
        return []
    flags = [False] * len(quote)
    if timestring:
        start = quote.lower().find(timestring.lower())
        if start >= 0:
            for i in range(start, min(start + len(timestring), len(quote))):
                flags[i] = True

    words: list[tuple[str, bool]] = []
    current = ""
    current_bold = False
    for char, flag in zip(quote, flags, strict=True):
        if char.isspace():
            if current:
                words.append((current, current_bold))
                current, current_bold = "", False
        else:
            current += char
            current_bold = current_bold or flag
    if current:
        words.append((current, current_bold))
    return words


def _wrap(words, draw, font_regular, font_bold, max_width: int):
    """Greedy word-wrap across two faces. Returns lines of
    ``(word, is_bold, width)``; a single word wider than ``max_width`` gets
    its own line rather than being dropped."""
    space_w = draw.textlength(" ", font=font_regular)
    lines: list[list[tuple[str, bool, float]]] = []
    current: list[tuple[str, bool, float]] = []
    current_w = 0.0
    for word, bold in words:
        font = font_bold if bold else font_regular
        word_w = draw.textlength(word, font=font)
        candidate = word_w if not current else current_w + space_w + word_w
        if current and candidate > max_width:
            lines.append(current)
            current, current_w = [(word, bold, word_w)], word_w
        else:
            current.append((word, bold, word_w))
            current_w = candidate
    if current:
        lines.append(current)
    return lines


def _fit_credits(text: str, draw, font_path: str, start_size: int, max_width: int):
    """Fit the attribution on one line, shrinking the face before resorting
    to truncation — "Murder in the Mews, Agatha Christie" reads better a
    point smaller than it does ellipsized to "Agatha C…". Returns
    ``(text, font, size)``."""
    for size in range(start_size, CREDITS_SIZE_MIN - 1, -1):
        try:
            font = ImageFont.truetype(font_path, size)
        except OSError:
            font = ImageFont.load_default()
        if draw.textlength(text, font=font) <= max_width:
            return text, font, size
    trimmed = text
    while trimmed and draw.textlength(trimmed + ELLIPSIS, font=font) > max_width:
        trimmed = trimmed[:-1]
    return ((trimmed + ELLIPSIS) if trimmed else ""), font, CREDITS_SIZE_MIN


def render_quote(
    size: tuple[int, int],
    quote: str,
    timestring: str,
    title: str,
    author: str,
    font_path: str,
    font_bold_path: str,
) -> Image.Image:
    """Lay out ``quote`` (with ``timestring`` emboldened) plus attribution
    into a 1-bit image of ``size``.

    Drawn straight onto mode "1" so glyph edges stay hard — an antialiased
    render thresholded afterwards is exactly the mush this replaces.
    """
    width, height = size
    image = Image.new("1", size, 255)
    draw = ImageDraw.Draw(image)
    if not quote:
        return image

    margin = max(4, int(width * SIDE_MARGIN_RATIO))
    max_width = width - 2 * margin
    words = _bold_words(quote, timestring)
    credits_text = "— " + ", ".join(p for p in (title, author) if p) if (title or author) else ""

    chosen = None
    for body_size in range(BODY_SIZE_MAX, BODY_SIZE_MIN - 1, -1):
        try:
            font_regular = ImageFont.truetype(font_path, body_size)
            font_bold = ImageFont.truetype(font_bold_path, body_size)
        except OSError:
            font_regular = font_bold = ImageFont.load_default()

        credits_size = max(CREDITS_SIZE_MIN, int(body_size * CREDITS_RATIO))
        try:
            font_credits = ImageFont.truetype(font_path, credits_size)
        except OSError:
            font_credits = ImageFont.load_default()

        line_h = int(body_size * LINE_SPACING)
        credits_h = (credits_size + CREDITS_GAP) if credits_text else 0
        lines = _wrap(words, draw, font_regular, font_bold, max_width)
        if len(lines) * line_h + credits_h <= height:
            chosen = (lines, font_regular, font_bold, font_credits, line_h, credits_size)
            break

    if chosen is None:
        # Even the floor size overflows — keep the largest number of whole
        # lines that fit and mark the cut, rather than overprinting.
        font_regular = ImageFont.truetype(font_path, BODY_SIZE_MIN)
        font_bold = ImageFont.truetype(font_bold_path, BODY_SIZE_MIN)
        font_credits = ImageFont.truetype(font_path, max(CREDITS_SIZE_MIN, int(BODY_SIZE_MIN * CREDITS_RATIO)))
        line_h = int(BODY_SIZE_MIN * LINE_SPACING)
        credits_size = max(CREDITS_SIZE_MIN, int(BODY_SIZE_MIN * CREDITS_RATIO))
        lines = _wrap(words, draw, font_regular, font_bold, max_width)
        budget = max(1, (height - (credits_size + CREDITS_GAP if credits_text else 0)) // line_h)
        if len(lines) > budget:
            lines = lines[:budget]
            if lines:
                last_word, last_bold, last_w = lines[-1][-1]
                lines[-1][-1] = (last_word + ELLIPSIS, last_bold, last_w)
        chosen = (lines, font_regular, font_bold, font_credits, line_h, credits_size)

    lines, font_regular, font_bold, font_credits, line_h, credits_size = chosen

    y = 0
    for line in lines:
        x = margin
        space_w = draw.textlength(" ", font=font_regular)
        for word, bold, word_w in line:
            draw.text((x, y), word, font=(font_bold if bold else font_regular), fill=0)
            x += word_w + space_w
        y += line_h

    if credits_text:
        fitted, font_credits, credits_size = _fit_credits(credits_text, draw, font_path, credits_size, max_width)
        if fitted:
            credits_w = draw.textlength(fitted, font=font_credits)
            credits_y = min(y + CREDITS_GAP, height - credits_size - 1)
            draw.text((width - margin - credits_w, credits_y), fitted, font=font_credits, fill=0)

    return image
