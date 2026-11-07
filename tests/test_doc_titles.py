"""Document titles: junk metadata is dropped and page 1 fills the gap.

`pdf_corpus_overview` cards and `pdf_corpus_search` hits share one never-null
title. Empty or junk metadata used to fall straight back to the filename, which
tells an agent nothing about a file named after an arXiv id.
"""

import sqlite3

import pymupdf
import pytest

from pdf_mcp import corpus
from pdf_mcp.cache import PDFCache
from pdf_mcp.docopen import open_pdf
from pdf_mcp.extractor import guess_page_title

BODY = "Ordinary body text runs across the page at the size most text is set in."


def _make_pdf(path, title_meta, draws):
    doc = pymupdf.open()
    page = doc.new_page()
    for y, size, text in draws:
        page.insert_text((72, y), text, fontsize=size)
    doc.set_metadata({"title": title_meta})
    doc.save(str(path))
    doc.close()
    return str(path)


def _guess(path):
    doc = open_pdf(path)
    try:
        return guess_page_title(doc[0])
    finally:
        doc.close()


def _paper(path, title_meta=""):
    return _make_pdf(
        path,
        title_meta,
        [(90, 20, "Attention Is All You Need"), (140, 10, BODY), (155, 10, BODY)],
    )


class TestGuessPageTitle:
    def test_takes_the_largest_text(self, tmp_path):
        assert _guess(_paper(tmp_path / "a.pdf")) == "Attention Is All You Need"

    def test_joins_a_title_set_over_two_lines(self, tmp_path):
        pdf = _make_pdf(
            tmp_path / "a.pdf",
            "",
            [
                (90, 20, "Serially Concatenated"),
                (114, 20, "IRA Codes"),
                (160, 10, BODY),
                (175, 10, BODY),
            ],
        )
        assert _guess(pdf) == "Serially Concatenated IRA Codes"

    def test_stops_at_a_same_size_line_further_down(self, tmp_path):
        pdf = _make_pdf(
            tmp_path / "a.pdf",
            "",
            [
                (90, 20, "The Real Title"),
                (140, 10, BODY),
                (155, 10, BODY),
                (300, 20, "Section Banner"),
            ],
        )
        assert _guess(pdf) == "The Real Title"

    @pytest.mark.parametrize(
        "banner, size",
        [("BMC Genetics", 24), ("ARTICLE", 16), ("Clinical research", 14)],
    )
    def test_a_journal_banner_gives_way_to_the_title(self, tmp_path, banner, size):
        pdf = _make_pdf(
            tmp_path / "a.pdf",
            "",
            [
                (50, size, banner),
                (130, 16, "Screening the genome to detect an association"),
                (180, 10, BODY),
                (195, 10, BODY),
            ],
        )
        assert _guess(pdf) == "Screening the genome to detect an association"

    @pytest.mark.parametrize(
        "short, below",
        [
            ("Random valuations", "Andrii Ilienko1,2 and Ilya Molchanov1"),
            ("Exchangeable graphs", "Stefan Weber * Leibniz University Hannover"),
            ("Foot Locker, Inc.", "330 West 34th Street, New York, New York"),
            ("NIKE, Inc.", "(Exact name of Registrant as specified in its charter)"),
            ("FORM 10-K", "ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE ACT"),
        ],
    )
    def test_a_short_title_keeps_its_place(self, tmp_path, short, below):
        pdf = _make_pdf(
            tmp_path / "a.pdf",
            "",
            [
                (70, 20, short),
                (110, 14, below),
                (180, 10, BODY),
                (195, 10, BODY),
            ],
        )
        assert _guess(pdf) == short

    def test_a_longer_line_above_a_short_title_is_ignored(self, tmp_path):
        pdf = _make_pdf(
            tmp_path / "a.pdf",
            "",
            [
                (50, 14, "UNITED STATES SECURITIES AND EXCHANGE COMMISSION"),
                (110, 20, "Apple Inc."),
                (180, 10, BODY),
                (195, 10, BODY),
            ],
        )
        assert _guess(pdf) == "Apple Inc."

    def test_a_long_line_low_on_the_page_is_not_a_title(self, tmp_path):
        pdf = _make_pdf(
            tmp_path / "a.pdf",
            "",
            [
                (70, 20, "Deep Learning"),
                (120, 10, BODY),
                (135, 10, BODY),
                (500, 14, "A heading much further down the first page"),
            ],
        )
        assert _guess(pdf) == "Deep Learning"

    def test_page_of_one_size_has_no_title(self, tmp_path):
        pdf = _make_pdf(tmp_path / "a.pdf", "", [(90, 10, BODY), (105, 10, BODY)])
        assert _guess(pdf) is None

    def test_blank_page_has_no_title(self, tmp_path):
        assert _guess(_make_pdf(tmp_path / "a.pdf", "", [])) is None

    def test_a_large_page_number_is_passed_over(self, tmp_path):
        pdf = _make_pdf(
            tmp_path / "a.pdf",
            "",
            [
                (60, 30, "1"),
                (100, 18, "Controlling Spiral Turbulence"),
                (150, 10, BODY),
                (165, 10, BODY),
            ],
        )
        assert _guess(pdf) == "Controlling Spiral Turbulence"

    def test_a_drop_cap_does_not_make_the_body_line_a_title(self, tmp_path):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "M", fontsize=30)
        page.insert_text((100, 100), "EDICAL image captioning is hard", fontsize=10)
        page.insert_text((72, 115), BODY, fontsize=10)
        page.insert_text((72, 130), BODY, fontsize=10)
        path = tmp_path / "a.pdf"
        doc.save(str(path))
        doc.close()
        assert _guess(str(path)) is None

    def test_symbol_soup_is_not_a_title(self, tmp_path):
        pdf = _make_pdf(
            tmp_path / "a.pdf",
            "",
            [(90, 20, "M , p , G = 1 + 2"), (140, 10, BODY), (155, 10, BODY)],
        )
        assert _guess(pdf) is None


class TestCleanTitle:
    @pytest.mark.parametrize(
        "junk",
        [
            "printmgr file",
            "arXiv:0902.1533v1  [astro-ph.HE]  9 Feb 2009",
            "Microsoft Word - Manuscript Draft_arxive.docx",
            "0001326801-23-000013",
            "s2_2502kiji_01-13_20.indd",
            "104.ren",
            "fig10.eps",
            "gkm094 1..11",
            "plme-03-06-08 845..855",
            "1",
            "",
        ],
    )
    def test_junk_is_dropped(self, junk):
        assert corpus._clean_title(junk) is None

    @pytest.mark.parametrize(
        "real",
        [
            "GOOG 10-K 2024",
            "BAV16W/1N4148W",
            "10-K - 02/28/2023 - AMC Entertainment Holdings, Inc.",
            "民法",
            "A 5/8 Lower Bound on the Banach-Mazur Distance",
        ],
    )
    def test_real_titles_survive(self, real):
        assert corpus._clean_title(real) == real


class TestDocTitle:
    def _cached(self, tmp_path, pdf):
        cache = PDFCache(cache_dir=tmp_path / "cache")
        doc = open_pdf(pdf)
        cache.save_metadata(pdf, len(doc), {"title": doc.metadata.get("title")}, [])
        doc.close()
        return cache

    def test_good_metadata_wins_without_opening_the_pdf(self, tmp_path, monkeypatch):
        pdf = _paper(tmp_path / "2607.08291.pdf", "Embedded Title")
        cache = self._cached(tmp_path, pdf)
        monkeypatch.setattr(corpus, "open_pdf", None)
        assert corpus.doc_title(pdf, cache.get_metadata(pdf), cache) == "Embedded Title"

    def test_page_one_fills_empty_metadata(self, tmp_path):
        pdf = _paper(tmp_path / "2607.08291.pdf")
        cache = self._cached(tmp_path, pdf)
        title = corpus.doc_title(pdf, cache.get_metadata(pdf), cache)
        assert title == "Attention Is All You Need"

    def test_guess_is_cached_and_page_one_read_once(self, tmp_path, monkeypatch):
        pdf = _paper(tmp_path / "2607.08291.pdf")
        cache = self._cached(tmp_path, pdf)
        corpus.doc_title(pdf, cache.get_metadata(pdf), cache)
        monkeypatch.setattr(corpus, "open_pdf", None)
        title = corpus.doc_title(pdf, cache.get_metadata(pdf), cache)
        assert title == "Attention Is All You Need"

    def test_filename_is_the_last_resort_and_is_cached(self, tmp_path, monkeypatch):
        pdf = _make_pdf(tmp_path / "scan-01.pdf", "", [(90, 10, BODY)])
        cache = self._cached(tmp_path, pdf)
        assert corpus.doc_title(pdf, cache.get_metadata(pdf), cache) == "scan-01"
        monkeypatch.setattr(corpus, "open_pdf", None)
        assert corpus.doc_title(pdf, cache.get_metadata(pdf), cache) == "scan-01"

    def test_unreadable_page_falls_back_and_is_not_cached(self, tmp_path, monkeypatch):
        pdf = _paper(tmp_path / "2607.08291.pdf")
        cache = self._cached(tmp_path, pdf)

        def boom(_path):
            raise RuntimeError("engine failed")

        monkeypatch.setattr(corpus, "open_pdf", boom)
        assert corpus.doc_title(pdf, cache.get_metadata(pdf), cache) == "2607.08291"
        assert cache.get_metadata(pdf)["title_guess"] is None

    def test_no_metadata_row_uses_the_filename(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path / "cache")
        assert corpus.doc_title("/x/report.pdf", None, cache) == "report"

    def test_rewriting_metadata_forgets_the_guess(self, tmp_path):
        pdf = _paper(tmp_path / "a.pdf")
        cache = self._cached(tmp_path, pdf)
        corpus.doc_title(pdf, cache.get_metadata(pdf), cache)
        cache.save_metadata(pdf, 1, {"title": ""}, [])
        assert cache.get_metadata(pdf)["title_guess"] is None

    def test_old_cache_gains_the_column_and_keeps_its_rows(self, tmp_path):
        pdf = _paper(tmp_path / "a.pdf")
        cache = self._cached(tmp_path, pdf)
        with sqlite3.connect(cache.db_path) as conn:
            conn.execute("ALTER TABLE pdf_metadata DROP COLUMN title_guess")
        reopened = PDFCache(cache_dir=tmp_path / "cache")
        meta = reopened.get_metadata(pdf)
        assert meta["page_count"] == 1
        assert meta["title_guess"] is None


def test_overview_card_and_search_share_the_title(tmp_path):
    pdf = _paper(tmp_path / "2607.08291.pdf")
    cache = PDFCache(cache_dir=tmp_path / "cache")
    doc = open_pdf(pdf)
    cache.save_metadata(pdf, 1, {"title": ""}, [], text_coverage=[])
    doc.close()
    card = corpus.build_overview_card(pdf, cache, from_cache=True)
    assert card["title"] == "Attention Is All You Need"
    meta = cache.get_metadata(pdf)
    assert corpus.doc_title(pdf, meta, cache) == card["title"]
