"""Corpus-wide keyword arm: one BM25 ranking over the shared FTS index.

The per-document arm ranked pages inside each document and fused the lists
by RRF, so every document's best page tied and the top of the ranking was
one page per document. A document that clearly matched best could not take
a second slot. `PDFCache.search_fts_corpus` ranks every page of the corpus
in one query against the shared `pdf_search_fts` table, so scores compare
across documents.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pdf_mcp.cache import PDFCache


def _make_pdfs(tmp_path: Path, names: list[str]) -> list[str]:
    paths = []
    for name in names:
        doc = pymupdf.open()
        doc.new_page()
        p = tmp_path / name
        doc.save(p)
        doc.close()
        paths.append(str(p))
    return paths


def _save(cache: PDFCache, path: str, pages: list[str]) -> None:
    for i, text in enumerate(pages):
        cache.save_page_text(path, i, text)


@pytest.fixture
def fts_cache(cache):
    if not cache.fts_available:
        pytest.skip("FTS5 not available in this SQLite build")
    return cache


FILLER = "The committee reviewed the annual schedule and adjourned early."


class TestSearchFtsCorpusRanking:
    def test_strong_document_takes_several_top_slots(self, fts_cache, tmp_path):
        """The per-document arm interleaved one page per document; here a
        document matching on three pages outranks a weak single mention."""
        strong, weak = _make_pdfs(tmp_path, ["strong.pdf", "weak.pdf"])
        _save(
            fts_cache,
            strong,
            [
                "Glacier calving rates and glacier calving fronts. " * 3,
                "Glacier calving observed again, glacier calving measured.",
                "Glacier calving season summary with calving glacier notes.",
            ],
        )
        _save(
            fts_cache,
            weak,
            [
                FILLER + " A glacier calving footnote. " + FILLER * 8,
                FILLER,
            ],
        )

        res = fts_cache.search_fts_corpus(
            [strong, weak], "glacier calving", max_results=10, context_chars=100
        )

        assert res is not None
        top3 = [(m["path"], m["page"]) for m in res.matches[:3]]
        assert all(p == strong for p, _ in top3), top3

    def test_exact_phrase_outranks_scattered_terms(self, fts_cache, tmp_path):
        """A page holding the query as written beats one holding the same
        words apart, even when the scattered page repeats them more."""
        a, b = _make_pdfs(tmp_path, ["a.pdf", "b.pdf"])
        _save(fts_cache, a, [FILLER + " the double marginalization problem. " + FILLER])
        _save(
            fts_cache,
            b,
            [
                "marginalization of priors; double counting; double check; "
                "marginalization again and double again."
            ],
        )

        res = fts_cache.search_fts_corpus(
            [a, b], "double marginalization", max_results=10, context_chars=100
        )

        assert res is not None
        assert (res.matches[0]["path"], res.matches[0]["page"]) == (a, 1)

    def test_only_listed_documents_are_searched(self, fts_cache, tmp_path):
        inside, outside = _make_pdfs(tmp_path, ["in.pdf", "out.pdf"])
        _save(fts_cache, inside, ["quasar spectra table"])
        _save(fts_cache, outside, ["quasar spectra quasar spectra"])

        res = fts_cache.search_fts_corpus(
            [inside], "quasar spectra", max_results=10, context_chars=100
        )

        assert res is not None
        assert {m["path"] for m in res.matches} == {inside}

    def test_match_carries_excerpt_score_and_1_indexed_page(self, fts_cache, tmp_path):
        (a,) = _make_pdfs(tmp_path, ["a.pdf"])
        _save(fts_cache, a, [FILLER, FILLER + " The pulsar timing array. " + FILLER])

        res = fts_cache.search_fts_corpus(
            [a], "pulsar timing", max_results=10, context_chars=100
        )

        assert res is not None
        (m,) = res.matches
        assert m["page"] == 2
        assert "pulsar" in m["excerpt"].lower()
        assert m["score"] > 0


class TestSearchFtsCorpusPartialMatching:
    QUESTION = "how does the reactor coolant loop handle pressure spikes"

    def test_partial_matches_rank_when_nothing_matches_every_term(
        self, fts_cache, tmp_path
    ):
        """Paraphrase queries rarely carry every term on one page; strict
        AND found nothing and the arm contributed nothing."""
        a, b = _make_pdfs(tmp_path, ["a.pdf", "b.pdf"])
        _save(fts_cache, a, ["The reactor coolant loop absorbs pressure transients."])
        _save(fts_cache, b, [FILLER])

        res = fts_cache.search_fts_corpus(
            [a, b], self.QUESTION, max_results=10, context_chars=100
        )

        assert res is not None
        assert [m["path"] for m in res.matches] == [a]
        assert res.and_doc_count == 0
        assert res.partial is True

    def test_partial_matches_are_excluded_when_a_full_match_exists(
        self, fts_cache, tmp_path
    ):
        """Measured: adding partial matches next to full ones put one-term
        pages on both fused lists and pushed exact needle hits down."""
        full, partial = _make_pdfs(tmp_path, ["full.pdf", "partial.pdf"])
        _save(fts_cache, full, ["neutron flux detector calibration"])
        _save(fts_cache, partial, ["neutron neutron neutron flux flux"])

        res = fts_cache.search_fts_corpus(
            [full, partial],
            "neutron flux detector calibration",
            max_results=10,
            context_chars=100,
        )

        assert res is not None
        assert [m["path"] for m in res.matches] == [full]
        assert res.and_doc_count == 1
        assert res.partial is False

    def test_stopwords_do_not_match_on_their_own(self, fts_cache, tmp_path):
        a, b = _make_pdfs(tmp_path, ["a.pdf", "b.pdf"])
        _save(fts_cache, a, ["The reactor coolant loop."])
        _save(fts_cache, b, ["how does the thing work when it is here"])

        res = fts_cache.search_fts_corpus(
            [a, b], self.QUESTION, max_results=10, context_chars=100
        )

        assert res is not None
        assert b not in {m["path"] for m in res.matches}

    def test_two_term_query_stays_a_conjunction(self, fts_cache, tmp_path):
        """Same rule as `_fts5_or_fallback`: two terms is deliberate."""
        (a,) = _make_pdfs(tmp_path, ["a.pdf"])
        _save(fts_cache, a, ["pgvector index tuning"])

        res = fts_cache.search_fts_corpus(
            [a], "pgvector unicorn", max_results=10, context_chars=100
        )

        assert res is not None
        assert res.matches == []


class TestSearchFtsCorpusCounts:
    def test_doc_counts_cover_every_matching_page_capped(self, fts_cache, tmp_path):
        """Counts are per-document matching pages, independent of how many
        matches the ranking returns, capped at max_results per document."""
        a, b = _make_pdfs(tmp_path, ["a.pdf", "b.pdf"])
        _save(fts_cache, a, [f"magnetar burst {i}" for i in range(6)])
        _save(fts_cache, b, ["magnetar burst", FILLER])

        res = fts_cache.search_fts_corpus(
            [a, b], "magnetar burst", max_results=4, context_chars=100
        )

        assert res is not None
        assert len(res.matches) == 4
        assert res.doc_match_counts == {a: 4, b: 1}
        assert res.and_doc_match_counts == {a: 4, b: 1}
        assert res.and_doc_count == 2

    def test_partial_tier_counts_are_kept_apart_from_full_matches(
        self, fts_cache, tmp_path
    ):
        (a,) = _make_pdfs(tmp_path, ["a.pdf"])
        _save(fts_cache, a, ["The reactor coolant loop."])

        res = fts_cache.search_fts_corpus(
            [a],
            "how does the reactor coolant loop handle pressure spikes",
            max_results=10,
            context_chars=100,
        )

        assert res is not None
        assert res.doc_match_counts == {a: 1}
        assert res.and_doc_match_counts == {}


class TestSearchFtsCorpusDeclines:
    """None means "use the per-document arm": the shared index cannot
    answer this query faithfully."""

    def test_declines_a_cjk_query(self, fts_cache, tmp_path):
        (a,) = _make_pdfs(tmp_path, ["a.pdf"])
        _save(fts_cache, a, ["量子 計算"])
        assert (
            fts_cache.search_fts_corpus([a], "量子", max_results=10, context_chars=50)
            is None
        )

    def test_declines_german_mode(self, fts_cache, tmp_path):
        (a,) = _make_pdfs(tmp_path, ["a.pdf"])
        _save(fts_cache, a, ["Kündigungsfrist"])
        fts_cache.fts_language = "de"
        assert (
            fts_cache.search_fts_corpus(
                [a], "Kündigungsfrist", max_results=10, context_chars=50
            )
            is None
        )

    def test_declines_when_a_document_is_missing_from_the_shared_index(
        self, fts_cache, tmp_path
    ):
        a, b = _make_pdfs(tmp_path, ["a.pdf", "b.pdf"])
        _save(fts_cache, a, ["kelvin wave"])
        _save(fts_cache, b, ["kelvin wave", "kelvin"])
        with fts_cache._connect() as conn:
            conn.execute(
                "DELETE FROM pdf_search_fts WHERE file_path = ? AND page_num = 1",
                (b,),
            )
        assert (
            fts_cache.search_fts_corpus(
                [a, b], "kelvin wave", max_results=10, context_chars=50
            )
            is None
        )

    def test_declines_without_fts5(self, cache, tmp_path):
        (a,) = _make_pdfs(tmp_path, ["a.pdf"])
        cache.fts_available = False
        assert (
            cache.search_fts_corpus([a], "anything", max_results=10, context_chars=50)
            is None
        )

    def test_empty_query_returns_no_matches(self, fts_cache, tmp_path):
        (a,) = _make_pdfs(tmp_path, ["a.pdf"])
        _save(fts_cache, a, ["text"])
        res = fts_cache.search_fts_corpus([a], " ** ", max_results=10, context_chars=50)
        assert res is not None
        assert res.matches == []


def _write_pdf(path: Path, pages: list[str]) -> str:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text)
    doc.save(path)
    doc.close()
    return str(path)


@pytest.fixture
def ranked_corpus(tmp_path, isolated_server):
    cache, _ = isolated_server
    if not cache.fts_available:
        pytest.skip("FTS5 not available in this SQLite build")
    d = tmp_path / "corpus"
    d.mkdir()
    strong = _write_pdf(
        d / "a_strong.pdf",
        [
            "Glacier calving rates and glacier calving fronts were measured.",
            "Glacier calving observed again; glacier calving measured twice.",
            "Glacier calving season summary with calving glacier notes.",
        ],
    )
    others = [
        _write_pdf(
            d / f"b_other{i}.pdf",
            [FILLER + " One glacier calving footnote. " + FILLER * 6, FILLER],
        )
        for i in range(3)
    ]
    from pdf_mcp.server import pdf_corpus_warm

    pdf_corpus_warm([strong, *others], embeddings=False)
    return cache, strong, others


class TestCorpusSearchUsesCorpusWideRanking:
    def test_keyword_mode_lets_a_strong_document_hold_several_slots(
        self, ranked_corpus
    ):
        """Before: one page per document, in RRF-tie order, so the strong
        document held rank 1 only and the footnotes filled ranks 2-4."""
        from pdf_mcp.server import pdf_corpus_search

        _cache, strong, _others = ranked_corpus
        res = pdf_corpus_search(
            str(Path(strong).parent), "glacier calving", mode="keyword"
        )

        assert "error" not in res, res
        top3 = [m["path"] for m in res["matches"][:3]]
        assert top3 == [strong] * 3, top3

    def test_hybrid_counts_only_documents_matching_every_term(self, ranked_corpus):
        """The partial tier ranks pages but must not list every document that
        shares one word in `doc_match_counts`, the field agents fan out on."""
        from pdf_mcp.tools.corpus_tools import _corpus_keyword_arm

        _cache, strong, others = ranked_corpus
        files = [strong, *others]
        query = "how are glacier calving rates measured here"

        ranked, counts, _payload, and_docs, partial = _corpus_keyword_arm(
            files, query, 10, 200, "auto"
        )
        assert ranked, "partial tier should rank pages in hybrid mode too"
        assert counts == {} and and_docs == 0
        # Hybrid mode builds these pages' excerpts from the semantic path:
        # a partial match's snippet sits on one shared word, not the answer.
        assert partial is True

        _r, kw_counts, _p, _a, _pt = _corpus_keyword_arm(
            files, query, 10, 200, "keyword"
        )
        assert set(kw_counts) == set(files)

    def test_falls_back_to_per_document_arm_when_index_is_incomplete(
        self, ranked_corpus
    ):
        from pdf_mcp.server import pdf_corpus_search

        cache, strong, _others = ranked_corpus
        with cache._connect() as conn:
            conn.execute("DELETE FROM pdf_search_fts WHERE file_path = ?", (strong,))
        res = pdf_corpus_search(
            str(Path(strong).parent), "glacier calving", mode="keyword"
        )

        assert "error" not in res, res
        # Per-document RRF: one page per document at the top.
        top = [m["path"] for m in res["matches"][:4]]
        assert len(set(top)) == 4, top
