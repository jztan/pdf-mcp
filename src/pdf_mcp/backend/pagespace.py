"""The transform from pdfium's PDF space into PyMuPDF's page space.

pdfium reports geometry in raw PDF user space (y up, origin at the
mediabox origin). PyMuPDF normalises everything to the VISIBLE page: the
intersection of mediabox and cropbox, with (0, 0) at its top-left and y
running down. The two agree only when the cropbox starts at (0, 0),
which is exactly often enough to hide the difference.

It hid twice here. Berkshire's annual report has a mediabox origin of
(18, 18), which shifted every pdfplumber table bbox 18pt. Then the
Vishay datasheet, with cropbox (0, 25, 604, 817) inside a 842pt-tall
mediabox, shifted every glyph, drawing and image bbox 25pt: a paragraph
block's reported bbox no longer contained its own text under a PyMuPDF
clip, which is how the excerpt gate's bbox-fidelity clause caught it.

Every backend module that emits coordinates must flip through this, not
through get_size().
"""

from __future__ import annotations

from typing import Any


def page_transform(page: Any) -> tuple[float, float]:
    """(x_offset, y_top) such that visible-space coordinates are
    x' = x - x_offset and y' = y_top - y."""
    media = page.get_mediabox()
    crop = page.get_cropbox() or media
    x_offset = max(media[0], crop[0])
    y_top = min(media[3], crop[3])
    return float(x_offset), float(y_top)
