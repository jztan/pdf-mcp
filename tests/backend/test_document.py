import sys
import pymupdf
import pytest

from pdf_mcp.backend.document import open_document
from tests.backend.differential import assert_equivalent, assert_non_empty


def test_toc_matches_pymupdf_exactly(corpus_pdfs):
    """TOC has no representational ambiguity, so it must match exactly."""
    checked = 0
    for pdf in corpus_pdfs("pages"):
        ref_doc = pymupdf.open(str(pdf))
        ref_toc = ref_doc.get_toc()
        ref_doc.close()
        if not ref_toc:
            continue
        doc = open_document(str(pdf))
        got = doc.get_toc()
        doc.close()
        assert_non_empty(got, f"{pdf.name} toc")
        assert_equivalent(ref_toc, got, label=f"{pdf.name} toc")
        checked += 1
    if checked == 0:
        pytest.skip("no document in sample had a TOC")


def test_page_count_and_title_match(corpus_pdfs):
    for pdf in corpus_pdfs("charts")[:5]:
        ref_doc = pymupdf.open(str(pdf))
        ref_count = ref_doc.page_count
        ref_title = ref_doc.metadata.get("title", "")
        ref_doc.close()
        doc = open_document(str(pdf))
        assert doc.page_count == ref_count
        assert doc.metadata["title"] == ref_title
        doc.close()


def test_metadata_has_every_key_extractor_reads():
    """extractor.extract_metadata reads PyMuPDF's raw key names, including
    the camelCase creationDate/modDate. Renaming them here would silently
    blank two response fields rather than raise."""
    doc = open_document("benchmark_data/chart_extraction/syn_corpus/bar_simple.pdf")
    for key in (
        "format",
        "title",
        "author",
        "subject",
        "keywords",
        "creator",
        "producer",
        "creationDate",
        "modDate",
        "encryption",
    ):
        assert key in doc.metadata, f"missing metadata key {key}"
    doc.close()


def test_metadata_values_match_pymupdf(corpus_pdfs):
    """Values, not just keys: a key-set-only check passed in the spike
    while both sides returned empty strings."""
    fields = ("title", "author", "creator", "producer", "creationDate", "modDate")
    for pdf in corpus_pdfs("pages"):
        ref_doc = pymupdf.open(str(pdf))
        ref = dict(ref_doc.metadata or {})
        ref_doc.close()
        doc = open_document(str(pdf))
        got = doc.metadata
        doc.close()
        assert_non_empty([v for v in ref.values() if v], f"{pdf.name} ref metadata")
        for field in fields:
            assert got[field] == ref.get(field, ""), f"{pdf.name}.{field}"


def test_pdf_version_string_matches_pymupdf(corpus_pdfs):
    for pdf in corpus_pdfs("pages"):
        ref_doc = pymupdf.open(str(pdf))
        ref_format = (ref_doc.metadata or {}).get("format", "")
        ref_doc.close()
        doc = open_document(str(pdf))
        got = doc.metadata["format"]
        doc.close()
        assert got == ref_format, f"{pdf.name}: {got!r} != {ref_format!r}"


def test_page_rect_matches_pymupdf(corpus_pdfs):
    for pdf in corpus_pdfs("pages")[:3]:
        ref_doc = pymupdf.open(str(pdf))
        ref = ref_doc[0].rect
        ref_doc.close()
        doc = open_document(str(pdf))
        got = doc[0].rect
        doc.close()
        assert abs(got.width - ref.width) < 0.05
        assert abs(got.height - ref.height) < 0.05


class TestCloseSurvivesGcRace:
    """pypdfium2's PdfDocument.close() iterates its `_kids` weakref set;
    a child trapped in a reference cycle is removed from that set by its
    finalizer only when the cyclic GC runs, which can land mid-iteration
    ("RuntimeError: Set changed size during iteration", seen on CI's
    3.14 leg). The guard below must make close() immune to that timing.

    The race needs the collector to fire inside that loop, which CPython
    only does from 3.12 on (measured: 3.10 0/1000 trials; 3.12 and 3.13
    200/200; 3.14 ~5% per trial, so it needs many trials to show up).
    """

    @staticmethod
    def _doc_with_cyclic_kid():
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument.new()
        pdf.new_page(100, 100)
        live = pdf[0]
        garbage = pdf[0]
        garbage.__dict__["_cycle"] = garbage  # only the cyclic GC frees it
        del garbage
        return pdf, live

    @staticmethod
    def _close_under_gc_pressure(close, trials: int = 200) -> int:
        import gc

        failures = 0
        old = gc.get_threshold()
        for _ in range(trials):
            pdf, live = TestCloseSurvivesGcRace._doc_with_cyclic_kid()
            gc.set_threshold(1, 1, 1)
            try:
                close(pdf)
            except RuntimeError as exc:
                assert "changed size" in str(exc)
                failures += 1
            finally:
                gc.set_threshold(*old)
            del live
        return failures

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="CPython < 3.12 never runs the GC inside close()'s kids loop",
    )
    def test_upstream_close_is_racy(self):
        """Documents the defect the guard exists for; if this stops
        failing on 3.12+, pypdfium2 fixed it upstream and the guard can
        go. 2000 trials: at 3.14's ~5% hit rate a clean run is ~e^-100.

        The probe needs an uninstrumented interpreter: a sys.settrace
        hook (coverage's C tracer, the default on <= 3.13) allocates on
        every line event, so the threshold-1 collector fires before
        close() enters its kids loop instead of inside it, and the race
        never lands (measured: 0/2000 on 3.13 under `--cov`, 2000/2000
        without). That is the failure that stopped the v3.1.0 publish
        run. coverage's sys.monitoring core (the 3.14 default) installs
        no trace function and leaves the timing alone, so only a real
        trace hook disqualifies the run.
        """
        if sys.gettrace() is not None:
            pytest.skip(
                "a sys.settrace hook (e.g. coverage's C tracer) shifts GC timing"
            )
        failures = self._close_under_gc_pressure(lambda pdf: pdf.close(), trials=2000)
        assert failures > 0

    def test_close_pdfium_is_race_free(self):
        from pdf_mcp.backend.document import close_pdfium

        assert self._close_under_gc_pressure(close_pdfium) == 0

    def test_close_pdfium_releases_handles(self):
        from pdf_mcp.backend.document import close_pdfium

        pdf, live = self._doc_with_cyclic_kid()
        close_pdfium(pdf)
        assert pdf.raw is None
        assert live.raw is None
