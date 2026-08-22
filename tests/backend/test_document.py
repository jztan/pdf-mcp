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
