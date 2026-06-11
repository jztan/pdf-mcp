"""Generate the browser demo's sample PDF (pages/sample.pdf).

A fictional ~216-page services agreement with realistic multi-block
prose; key terms repeat across blocks on the same page. A few pages are
image-only to exercise the demo's "scanned" state.

Deterministic: fixed content, fixed metadata — no timestamps, no
randomness — so repeated runs produce identical page text.

Local development:
    uv run python scripts/generate_demo_sample_pdf.py pages/sample.pdf

CI runs this in deploy-demo.yml before the Pages artifact upload; the
output is gitignored and never committed.
"""

import sys
from pathlib import Path

import pymupdf

PAGE_COUNT = 216
SCANNED_PAGES = (40, 41, 42, 130)  # 0-indexed image-only pages
MAX_BYTES = 3 * 1024 * 1024

_PARTIES = [
    "Meridian Analytics Pte Ltd",
    "Northbridge Logistics Sdn Bhd",
    "the Service Provider",
    "the Customer",
]

_CLAUSES = [
    (
        "Termination for Convenience",
        "Either party may terminate this Agreement upon ninety (90) days'"
        " prior written notice to the other party. Termination under this"
        " Section shall not relieve {a} of its obligation to pay all"
        " Service Fees accrued prior to the effective date of termination.",
    ),
    (
        "Payment Terms",
        "All invoices are payable within thirty (30) days of receipt."
        " Late payment shall accrue interest at one and one-half percent"
        " (1.5%) per month. {a} may suspend Services if payment is more"
        " than sixty (60) days overdue, following written notice to {b}.",
    ),
    (
        "Limitation of Liability",
        "In no event shall either party's aggregate liability under this"
        " Agreement exceed the total Service Fees paid in the twelve (12)"
        " months preceding the claim. Nothing in this Section limits"
        " liability for gross negligence or willful misconduct of {a}.",
    ),
    (
        "Data Return upon Termination",
        "In the event of termination under Section 14, the data return"
        " period shall not exceed thirty (30) days from the effective"
        " date. {a} shall provide all Customer Data to {b} in a"
        " commercially standard format at no additional charge.",
    ),
    (
        "Service Levels",
        "The Service Provider shall maintain monthly availability of"
        " 99.9% measured across calendar months. Service credits accrue"
        " at five percent (5%) of monthly fees per full percentage point"
        " below target, payable on the next invoice issued to {b}.",
    ),
    (
        "Confidentiality",
        "Each party shall protect the other's Confidential Information"
        " with no less than reasonable care, and shall not disclose it"
        " except to personnel of {a} with a need to know who are bound"
        " by obligations no less protective than this Section.",
    ),
]

_RECITAL = (
    'This Master Services Agreement (the "Agreement") is entered into'
    " by and between {a} and {b}, effective as of the Commencement Date"
    " set out in Schedule 1, and governs all Statements of Work executed"
    " hereunder, including provisions for payment, termination, and"
    " liability allocated between the parties."
)

_MARGIN = 56
_PAGE_RECT = pymupdf.paper_rect("a4")


def _page_blocks(page_index: int) -> list[str]:
    """Three prose blocks per page; terms repeat across blocks."""
    blocks = []
    section = page_index + 1
    for j in range(3):
        title, body = _CLAUSES[(page_index * 3 + j) % len(_CLAUSES)]
        a = _PARTIES[(page_index + j) % len(_PARTIES)]
        b = _PARTIES[(page_index + j + 1) % len(_PARTIES)]
        text = body.format(a=a, b=b)
        blocks.append(f"§{section}.{j + 1} {title}. {text} {text}")
    blocks.insert(0, _RECITAL.format(a=_PARTIES[0], b=_PARTIES[1]))
    return blocks


def _scanned_pixmap() -> pymupdf.Pixmap:
    """A small deterministic grayscale 'scan' texture."""
    width, height = 120, 168
    samples = bytes(
        180 + ((x * 7 + y * 13) % 40) for y in range(height) for x in range(width)
    )
    return pymupdf.Pixmap(pymupdf.csGRAY, width, height, samples, False)


def generate(out_path: Path) -> None:
    doc = pymupdf.open()
    try:
        pix = _scanned_pixmap()
        for i in range(PAGE_COUNT):
            page = doc.new_page(width=_PAGE_RECT.width, height=_PAGE_RECT.height)
            if i in SCANNED_PAGES:
                page.insert_image(
                    pymupdf.Rect(
                        _MARGIN,
                        _MARGIN,
                        _PAGE_RECT.width - _MARGIN,
                        _PAGE_RECT.height - _MARGIN,
                    ),
                    pixmap=pix,
                )
                continue
            rect = pymupdf.Rect(
                _MARGIN,
                _MARGIN,
                _PAGE_RECT.width - _MARGIN,
                _PAGE_RECT.height - _MARGIN,
            )
            page.insert_textbox(
                rect,
                "\n\n".join(_page_blocks(i)),
                fontsize=10,
                fontname="helv",
                align=pymupdf.TEXT_ALIGN_JUSTIFY,
            )
        doc.set_metadata(
            {
                "title": "Master Services Agreement (Sample)",
                "author": "pdf-mcp demo",
                "creationDate": "D:20260101000000Z",
                "modDate": "D:20260101000000Z",
            }
        )
        doc.save(str(out_path), garbage=4, deflate=True)
    finally:
        doc.close()

    size = out_path.stat().st_size
    if size > MAX_BYTES:
        raise SystemExit(f"sample.pdf is {size} bytes, exceeds budget of {MAX_BYTES}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("pages/sample.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    generate(target)
    print(f"wrote {target} ({target.stat().st_size} bytes)")
