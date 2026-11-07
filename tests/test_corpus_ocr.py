"""OCR on corpus warm (issue #103): pending pages, coverage, background
thread, and the pdf_corpus_warm `ocr` parameter."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdf_mcp import corpus
from pdf_mcp.cache import PDFCache


def _blank_pdf(path: Path, pages: int) -> str:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(200, 200)
    doc.save(str(path))
    doc.close()
    return str(path)


def _cov(*pairs: tuple[int, int]) -> list[dict[str, int]]:
    return [{"text_chars": t, "raster_images": r} for t, r in pairs]


@pytest.fixture
def cache(tmp_path):
    return PDFCache(cache_dir=tmp_path / "cache")


@pytest.fixture
def scan3(tmp_path, cache):
    """Page 0 has text, page 1 is an image, page 2 is blank."""
    path = _blank_pdf(tmp_path / "scan3.pdf", 3)
    cache.save_metadata(path, 3, {}, [], text_coverage=_cov((10, 1), (0, 1), (0, 0)))
    return path


@pytest.fixture
def scan2(tmp_path, cache):
    """Two image-only pages."""
    path = _blank_pdf(tmp_path / "scan2.pdf", 2)
    cache.save_metadata(path, 2, {}, [], text_coverage=_cov((0, 1), (0, 1)))
    cache.save_pages_text(path, {0: "", 1: ""})
    return path


class TestPendingPages:
    def test_candidates_only_textless_image_pages(self):
        from pdf_mcp.corpus_ocr import ocr_candidates

        assert ocr_candidates(_cov((10, 1), (0, 1), (0, 0))) == [1]

    def test_pending_excludes_pages_with_any_ocr_row_for_lang(self, cache, scan2):
        from pdf_mcp.corpus_ocr import ocr_pending_pages

        cache.save_page_text(scan2, 0, "x", source="ocr", ocr_lang="eng")
        cache.save_page_text(scan2, 1, "", source="ocr", ocr_lang="eng")
        assert ocr_pending_pages(scan2, cache, "eng") == []
        assert ocr_pending_pages(scan2, cache, "deu") == [0, 1]

    def test_pending_without_metadata_is_empty(self, cache, tmp_path):
        from pdf_mcp.corpus_ocr import ocr_pending_pages

        path = _blank_pdf(tmp_path / "cold.pdf", 1)
        assert ocr_pending_pages(path, cache, "eng") == []

    def test_progress_counts(self, cache, scan2):
        from pdf_mcp.corpus_ocr import ocr_progress

        cache.save_page_text(scan2, 0, "x", source="ocr", ocr_lang="eng")
        cache.save_page_text(scan2, 1, "", source="ocr", ocr_lang="eng")
        assert ocr_progress(scan2, cache, "eng") == {
            "pending": 0,
            "done": 1,
            "no_text": 1,
        }

    def test_lang_normalised(self, cache, scan2):
        from pdf_mcp.corpus_ocr import ocr_pending_pages

        cache.save_page_text(scan2, 0, "x", source="ocr", ocr_lang="ENG ")
        assert ocr_pending_pages(scan2, cache, "eng") == [1]

    def test_get_ocr_rows_any_language(self, cache, scan2):
        cache.save_page_text(scan2, 0, "", source="ocr", ocr_lang="deu")
        cache.save_page_text(scan2, 0, "hallo", source="ocr", ocr_lang="eng")
        assert cache.get_ocr_rows(scan2, [0, 1], None) == {0: "hallo"}
        assert cache.get_ocr_rows(scan2, [0, 1], "deu") == {0: ""}

    def test_blank_and_text_pages_never_pending(self, cache, scan3):
        from pdf_mcp.corpus_ocr import ocr_pending_pages

        assert ocr_pending_pages(scan3, cache, "eng") == [1]


class TestCoverage:
    def test_effective_coverage_counts_ocr_text(self, cache, scan2):
        from pdf_mcp.corpus_ocr import effective_coverage

        cache.save_page_text(scan2, 1, "abcd", source="ocr", ocr_lang="eng")
        cov = effective_coverage(scan2, cache)
        assert [c["text_chars"] for c in cov] == [0, 4]

    def test_coverage_label_full_after_ocr(self, cache, scan2):
        assert corpus._doc_coverage_label(scan2, cache) == "none"
        cache.save_page_text(scan2, 0, "one", source="ocr", ocr_lang="eng")
        assert corpus._doc_coverage_label(scan2, cache) == "partial"
        cache.save_page_text(scan2, 1, "two", source="ocr", ocr_lang="eng")
        assert corpus._doc_coverage_label(scan2, cache) == "full"

    def test_overview_card_counts_ocr_text(self, cache, scan2):
        cache.save_page_text(scan2, 0, "one", source="ocr", ocr_lang="eng")
        cache.save_page_text(scan2, 1, "two", source="ocr", ocr_lang="eng")
        card = corpus.build_overview_card(scan2, cache, from_cache=True)
        assert card["text_coverage"] == "full"


class TestOcrRowsInCorpusSearch:
    """The spec's #71 claim, proven before anything is built on it: an OCR
    row written by save_page_text is what corpus keyword search reads."""

    def _ocr_scan(self, path):
        from pdf_mcp import _core
        from pdf_mcp.server import pdf_corpus_warm

        warm = pdf_corpus_warm([path])
        assert "error" not in warm
        _core.cache.save_page_text(
            path, 0, "quarterly zebra revenue", source="ocr", ocr_lang="eng"
        )
        return _core.cache

    def test_ocr_row_found_by_corpus_keyword_search(self, sample_pdf_synthetic_scan):
        from pdf_mcp.server import pdf_corpus_search

        self._ocr_scan(sample_pdf_synthetic_scan)
        res = pdf_corpus_search([sample_pdf_synthetic_scan], "zebra", mode="keyword")
        assert [m["page"] for m in res["matches"]] == [1]
        assert res["matches"][0]["path"] == sample_pdf_synthetic_scan

    def test_rewarm_keeps_ocr_row(self, sample_pdf_synthetic_scan):
        from pdf_mcp.server import pdf_corpus_warm

        cache = self._ocr_scan(sample_pdf_synthetic_scan)
        pdf_corpus_warm([sample_pdf_synthetic_scan])
        assert cache.get_ocr_rows(sample_pdf_synthetic_scan, [0], "eng") == {
            0: "quarterly zebra revenue"
        }

    def test_low_text_coverage_clears_after_ocr(self, sample_pdf_synthetic_scan):
        from pdf_mcp.server import pdf_corpus_search

        self._ocr_scan(sample_pdf_synthetic_scan)
        res = pdf_corpus_search([sample_pdf_synthetic_scan], "zebra", mode="keyword")
        assert sample_pdf_synthetic_scan not in (res.get("low_text_coverage") or {})
