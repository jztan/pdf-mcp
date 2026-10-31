"""Cells of a scanned table whose OCR text breaks its column's format.

A scanner's OCR layer misreads decimal marks in ways that look like data:
a raised-dot decimal (5·4) comes back as "5-4", "5 4", "54" or "5*4", and
a 1 or 0 as a letter. Fisher's 1936 Table I, as downloaded from the
publisher, carries 51 such cells among 600. Nothing in the text says which.

A column of fixed-format decimals says it for us: when most of its cells
read digit, separator, digit with the same number of decimals, a short
cell that does not is the one to check against the render. Geometry only,
so no table detection is needed, and the page's own words are the input.

Pure logic, no PDF access: callers pass word tuples
(x0, y0, x1, y1, text, ...), as Page.get_text("words") returns them.

Run it only on text that may be wrong (scanner OCR, our OCR). A born-digital
text layer is exact, so every flag there would be a false alarm.
"""

from __future__ import annotations

import re
from collections import Counter
from statistics import median
from typing import Any

#: Fewer cells than this is not enough to call a format dominant.
MIN_COLUMN = 8
#: Share of a column's cells that must share one decimal format.
MIN_DOMINANCE = 0.6
#: Longest word considered a cell; longer ones are labels or prose.
_MAX_CELL_CHARS = 10

_DECIMAL = re.compile(r"^(\d+)([^\d\s])(\d+)$")

Word = tuple[str, float, float, float, float]


def decimal_key(text: str) -> tuple[str, int] | None:
    """(separator, fraction digits) for a two-group decimal, else None.

    The integer part's length varies legitimately (2.8 beside 15.3), so it
    is not part of the key. A comma before exactly three digits is
    thousands grouping (52,903), not a decimal mark.
    """
    m = _DECIMAL.match(text)
    if not m or (m.group(2) == "," and len(m.group(3)) == 3):
        return None
    return (m.group(2), len(m.group(3)))


def _is_fragment_pair(left: str, right: str) -> bool:
    """True when two adjacent words on a line are halves of one cell.

    OCR splits a misread cell at the bad mark ("4-" "6", "1" "7"). Two
    complete cells ("5.1" "3.5") never qualify: one side must end or start
    with punctuation, or both must be short bare digits.
    """
    if not left or not right:
        return False
    if not left[-1].isalnum() or not right[0].isalnum():
        return True
    return left.isdigit() and right.isdigit() and len(left) <= 2 and len(right) <= 2


def _join_fragments(words: list[Word]) -> list[Word]:
    """Rejoin cells the OCR split in two on one line."""
    words = sorted(words, key=lambda w: (round((w[2] + w[4]) / 2), w[1]))
    out: list[Word] = []
    for w in words:
        if out:
            p = out[-1]
            height = min(p[4] - p[2], w[4] - w[2])
            same_line = abs((p[2] + p[4]) / 2 - (w[2] + w[4]) / 2) < 0.5 * height
            close = 0 <= w[1] - p[3] < 0.8 * height
            if same_line and close and _is_fragment_pair(p[0], w[0]):
                out[-1] = (
                    p[0] + w[0],
                    p[1],
                    min(p[2], w[2]),
                    max(p[3], w[3]),
                    max(p[4], w[4]),
                )
                continue
        out.append(w)
    return out


def _columns(words: list[Word]) -> list[list[Word]]:
    """Digit-bearing words grouped into x-aligned columns, top to bottom."""
    short = [w for w in words if len(w[0]) <= _MAX_CELL_CHARS]
    cells = [w for w in _join_fragments(short) if re.search(r"\d", w[0])]
    if not cells:
        return []
    tol = 0.5 * median(w[3] - w[1] for w in cells)
    cells.sort(key=lambda w: (w[1] + w[3]) / 2)
    cols: list[list[Word]] = []
    current: list[Word] = []
    last = None
    for w in cells:
        centre = (w[1] + w[3]) / 2
        if last is not None and centre - last > tol:
            cols.append(current)
            current = []
        current.append(w)
        last = centre
    cols.append(current)
    for col in cols:
        col.sort(key=lambda w: (w[2] + w[4]) / 2)
    return cols


def find_suspect_cells(words: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """Short cells that break a column of fixed-format decimals.

    Returns [{"text", "bbox", "expected"}], where expected is the column's
    format with digits as 9 ("9.9", "9-99"). Wrong or missing separators
    and letters standing in for digits are caught; a wrong digit in a
    well-formed cell (6.4 for 5.4) is not, since its format is right.
    """
    norm: list[Word] = [
        (str(w[4]), float(w[0]), float(w[1]), float(w[2]), float(w[3])) for w in words
    ]
    found = []
    for col in _columns(norm):
        if len(col) < MIN_COLUMN:
            continue
        keys = Counter(k for k in (decimal_key(w[0]) for w in col) if k is not None)
        if not keys:
            continue
        (sep, frac), n = keys.most_common(1)[0]
        if n / len(col) < MIN_DOMINANCE:
            continue
        expected = "9" + sep + "9" * frac
        for w in col:
            if decimal_key(w[0]) != (sep, frac) and len(w[0]) <= len(expected) + 1:
                found.append(
                    {
                        "text": w[0],
                        "bbox": [round(v, 1) for v in w[1:]],
                        "expected": expected,
                    }
                )
    found.sort(key=lambda c: (c["bbox"][1], c["bbox"][0]))
    return found
