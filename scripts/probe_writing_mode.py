"""Report per-page vertical/horizontal glyph fractions and born-digital status.

Admission gate for the vertical-script test corpus, and the "measure reading
order" QA tool. Usage: python scripts/probe_writing_mode.py <pdf> [<pdf> ...]
"""

import sys

import pymupdf


def page_vertical_fraction(page) -> tuple[float, int]:
    vertical = horizontal = 0
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            nchars = sum(len(s.get("text", "")) for s in line.get("spans", []))
            if abs(dy) > abs(dx):
                vertical += nchars
            else:
                horizontal += nchars
    total = vertical + horizontal
    return (vertical / total if total else 0.0, total)


def probe(path: str) -> None:
    doc = pymupdf.open(path)
    best_frac = best_chars = 0
    best_page = -1
    doc_chars = 0
    for i in range(doc.page_count):
        frac, chars = page_vertical_fraction(doc[i])
        doc_chars += chars
        if chars >= 30 and frac > best_frac:
            best_frac, best_chars, best_page = frac, chars, i + 1
    print(
        f"{path}: pages={doc.page_count} born_digital={doc_chars > 20} "
        f"peak_vertical_page=p{best_page} frac={best_frac:.0%} ({best_chars} chars)"
    )


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        probe(arg)
