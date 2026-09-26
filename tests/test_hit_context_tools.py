"""section_path / lead_in at the tool boundary, cache-isolated (issue #66)."""

import pymupdf
import pytest

from pdf_mcp.server import pdf_corpus_search, pdf_search

_LEAD = "The following table presents revenue by reportable segment:"
_PROSE = (
    "Cloud revenue is expected to grow in the coming year because demand"
    " keeps rising across regions."
)


def _segment_page(page):
    page.insert_text((72, 72), "2 Segment results", fontsize=14)
    page.insert_text((72, 110), _LEAD, fontsize=10)
    page.insert_text((72, 140), "(In millions)", fontsize=9)
    page.insert_text((300, 160), "2024      2023", fontsize=9)
    page.insert_text((72, 185), "Cloud revenue      4,210      3,905", fontsize=10)
    page.insert_text((72, 205), "Devices revenue    1,020      1,110", fontsize=10)
    page.insert_text((72, 260), "3 Outlook", fontsize=14)
    page.insert_text((72, 290), _PROSE, fontsize=10)


def _write(path, with_outline):
    doc = pymupdf.open()
    _segment_page(doc.new_page())
    doc.new_page().insert_text(
        (72, 72), "4 Appendix: definitions of terms", fontsize=12
    )
    if with_outline:
        doc.set_toc(
            [[1, "2 Segment results", 1], [1, "3 Outlook", 1], [1, "4 Appendix", 2]]
        )
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def outlined_pdf(tmp_path):
    return _write(tmp_path / "outlined.pdf", with_outline=True)


@pytest.fixture
def plain_pdf(tmp_path):
    return _write(tmp_path / "plain.pdf", with_outline=False)


def _hit(result, needle):
    assert "error" not in result, result
    for m in result["matches"]:
        if needle in m["excerpt"]:
            return m
    raise AssertionError(f"no hit containing {needle!r}: {result['matches']}")


def test_table_hit_carries_section_path_and_lead_in(outlined_pdf, isolated_server):
    hit = _hit(pdf_search(outlined_pdf, "devices revenue", mode="keyword"), "1,020")
    assert hit["section_path"] == ["2 Segment results"]
    assert hit["lead_in"] == _LEAD


def test_prose_hit_gets_its_section_but_no_lead_in(outlined_pdf, isolated_server):
    hit = _hit(pdf_search(outlined_pdf, "demand regions", mode="keyword"), "demand")
    assert hit["section_path"] == ["3 Outlook"]
    assert "lead_in" not in hit


def test_no_outline_means_no_section_path(plain_pdf, isolated_server):
    hit = _hit(pdf_search(plain_pdf, "devices revenue", mode="keyword"), "1,020")
    assert "section_path" not in hit
    assert hit["lead_in"] == _LEAD  # lead-in does not need an outline


def test_corpus_hits_carry_the_same_context(outlined_pdf, isolated_server):
    folder = str(pymupdf.os.path.dirname(outlined_pdf))
    result = pdf_corpus_search(folder, "devices revenue", mode="keyword")
    hit = _hit(result, "1,020")
    assert hit["section_path"] == ["2 Segment results"]
    assert hit["lead_in"] == _LEAD
