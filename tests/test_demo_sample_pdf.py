"""Tests for the demo sample-PDF generator."""

from pathlib import Path

import pymupdf

from scripts.generate_demo_sample_pdf import (
    MAX_BYTES,
    PAGE_COUNT,
    SCANNED_PAGES,
    generate,
)


def test_sample_pdf_shape(tmp_path: Path) -> None:
    out = tmp_path / "sample.pdf"
    generate(out)

    assert out.stat().st_size <= MAX_BYTES

    doc = pymupdf.open(out)
    try:
        assert doc.page_count == PAGE_COUNT

        # Scanned pages: near-zero text, at least one raster image
        # (matches the demo's amber heuristic: chars < 40 and raster > 0).
        for idx in SCANNED_PAGES:
            page = doc[idx]
            assert len(page.get_text().strip()) < 40
            assert len(page.get_images()) > 0

        # Text pages carry substantial multi-block prose with repeated
        # query terms across blocks (project realistic-PDF rule).
        text_pages = [i for i in range(doc.page_count) if i not in SCANNED_PAGES]
        for idx in text_pages[:10]:
            text = doc[idx].get_text()
            assert len(text) > 1500
        full = "".join(doc[i].get_text().lower() for i in text_pages)
        for term in ("termination", "payment", "liability"):
            assert full.count(term) > 20
    finally:
        doc.close()


def test_sample_pdf_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    generate(a)
    generate(b)
    doc_a, doc_b = pymupdf.open(a), pymupdf.open(b)
    try:
        assert doc_a.page_count == doc_b.page_count
        for i in range(doc_a.page_count):
            assert doc_a[i].get_text() == doc_b[i].get_text()
    finally:
        doc_a.close()
        doc_b.close()
