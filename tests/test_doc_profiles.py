"""Document profiles: the cached per-doc head vector + term counts that
back the hybrid corpus search's document arm and overview `about`."""

import os
import time

import numpy as np
import pymupdf
import pytest

from pdf_mcp import corpus
from pdf_mcp.cache import PDFCache


def _make_pdf(path, pages):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 50), text, fontsize=11)
    doc.save(str(path))
    doc.close()


@pytest.fixture
def cache(tmp_path):
    return PDFCache(cache_dir=tmp_path / "cache", ttl_hours=1)


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    _make_pdf(p, ["alpha budget report", "second page budget"])
    return str(p)


class TestDocProfileTable:
    def test_round_trip_vector_and_terms(self, cache, pdf):
        vec = np.array([1.0, 0.0], dtype=np.float32).tobytes()
        cache.save_doc_profile(pdf, 1500, vec, {"budget": 2, "report": 1}, "m1")
        assert cache.get_doc_profiles([pdf], "m1") == {pdf: vec}
        assert cache.get_doc_terms([pdf]) == {pdf: {"budget": 2, "report": 1}}

    def test_null_vector_row_is_valid_but_none(self, cache, pdf):
        cache.save_doc_profile(pdf, 1500, None, {}, "m1")
        assert cache.get_doc_profiles([pdf], "m1") == {pdf: None}

    def test_model_mismatch_is_absent(self, cache, pdf):
        cache.save_doc_profile(pdf, 1500, b"\x00" * 8, {"x": 1}, "m1")
        assert cache.get_doc_profiles([pdf], "m2") == {}
        # terms are model-independent
        assert pdf in cache.get_doc_terms([pdf])

    def test_stale_mtime_is_absent(self, cache, pdf):
        cache.save_doc_profile(pdf, 1500, b"\x00" * 8, {"x": 1}, "m1")
        future = time.time() + 5
        os.utime(pdf, (future, future))
        assert cache.get_doc_profiles([pdf], "m1") == {}
        assert cache.get_doc_terms([pdf]) == {}

    def test_replace_overwrites(self, cache, pdf):
        cache.save_doc_profile(pdf, 1500, b"\x01" * 8, {"a": 1}, "m1")
        cache.save_doc_profile(pdf, 1500, b"\x02" * 8, {"b": 1}, "m2")
        assert cache.get_doc_profiles([pdf], "m1") == {}
        assert cache.get_doc_profiles([pdf], "m2") == {pdf: b"\x02" * 8}
        assert cache.get_doc_terms([pdf]) == {pdf: {"b": 1}}

    def test_missing_file_is_skipped_not_raised(self, cache, tmp_path):
        assert cache.get_doc_profiles([str(tmp_path / "ghost.pdf")], "m1") == {}
        assert cache.get_doc_terms([str(tmp_path / "ghost.pdf")]) == {}

    def test_write_inside_caller_transaction(self, cache, pdf):
        with cache.write_transaction() as conn:
            cache.save_doc_profile(pdf, 1500, b"\x01" * 8, {"a": 1}, "m1", conn=conn)
        assert pdf in cache.get_doc_profiles([pdf], "m1")

    def test_dropped_on_extraction_version_bump(self, cache, pdf, tmp_path):
        import sqlite3

        cache.save_doc_profile(pdf, 1500, b"\x01" * 8, {"a": 1}, "m1")
        # Simulate an older extraction version on disk, then reopen.
        with sqlite3.connect(cache.db_path) as conn:
            conn.execute("PRAGMA user_version = 1")
        reopened = PDFCache(cache_dir=tmp_path / "cache", ttl_hours=1)
        assert reopened.get_doc_profiles([pdf], "m1") == {}

    def test_invalidate_removes_profile(self, cache, pdf):
        """_invalidate_file must clear doc_profiles along with the rest of
        the per-file cache, not just page_text/page_embeddings."""
        cache.save_doc_profile(pdf, 1500, b"\x01" * 8, {"a": 1}, "m1")
        cache._invalidate_file(pdf)
        assert cache.get_doc_profiles([pdf], "m1") == {}
        assert cache.get_doc_terms([pdf]) == {}

    def test_clear_all_removes_profile(self, cache, pdf):
        cache.save_doc_profile(pdf, 1500, b"\x01" * 8, {"a": 1}, "m1")
        cache.clear_all()
        assert cache.get_doc_profiles([pdf], "m1") == {}
        assert cache.get_doc_terms([pdf]) == {}

    def test_clear_expired_removes_profile(self, cache, pdf):
        cache.save_metadata(pdf, 1, {}, [])
        cache.save_doc_profile(pdf, 1500, b"\x01" * 8, {"a": 1}, "m1")

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            conn.execute(
                "UPDATE pdf_metadata SET accessed_at = '2000-01-01'"
                " WHERE file_path = ?",
                (pdf,),
            )

        cache.clear_expired()
        assert cache.get_doc_profiles([pdf], "m1") == {}
        assert cache.get_doc_terms([pdf]) == {}


def _embed_len(texts):
    """Deterministic fake: unit vector keyed on text length parity."""
    out = []
    for t in texts:
        v = np.array([1.0, float(len(t) % 2)], dtype=np.float32)
        out.append((v / np.linalg.norm(v)).tobytes())
    return out


class TestBuildDocProfile:
    def test_head_is_page1_first_1500_chars(self):
        seen = []

        def spy(texts):
            seen.extend(texts)
            return _embed_len(texts)

        texts = {0: "x" * 2000, 1: "ignored"}
        vec, _terms, _head_page = corpus.build_doc_profile(texts, spy)
        assert seen == ["x" * 1500]
        assert vec is not None

    def test_empty_page1_gives_no_vector_and_no_encode(self):
        def boom(texts):
            raise AssertionError("must not encode an empty head")

        vec, terms, _head_page = corpus.build_doc_profile({0: "   "}, boom)
        assert vec is None
        assert terms == {}

    def test_empty_page1_uses_first_page_with_text(self):
        seen = []

        def spy(texts):
            seen.extend(texts)
            return _embed_len(texts)

        profile = corpus.build_doc_profile({0: "", 1: "\f\n", 2: "y" * 2000}, spy)
        assert seen == ["y" * 1500]
        assert profile.vector is not None
        assert profile.head_page == 2

    def test_page1_text_is_unchanged(self):
        seen = []

        def spy(texts):
            seen.extend(texts)
            return _embed_len(texts)

        profile = corpus.build_doc_profile({0: "x" * 2000}, spy)
        assert seen == ["x" * 1500]
        assert profile.head_page == 0

    def test_no_text_anywhere(self):
        def boom(texts):
            raise AssertionError("must not encode")

        profile = corpus.build_doc_profile({0: "", 1: "  "}, boom)
        assert profile.vector is None
        assert profile.head_page == -1

    def test_whitespace_only_pages_are_blank(self):
        def boom(texts):
            raise AssertionError("must not encode")

        profile = corpus.build_doc_profile({0: " \t\n\r\f\v"}, boom)
        assert profile.vector is None
        assert profile.head_page == -1

    def test_terms_are_4plus_chars_across_all_pages_capped(self):
        texts = {0: "the cat budget", 1: "Budget REPORT report a"}
        terms = corpus.build_doc_profile(texts, _embed_len).terms
        assert terms == {"budget": 2, "report": 2}
        many = {0: " ".join(f"term{i:04d}" for i in range(500))}
        terms = corpus.build_doc_profile(many, _embed_len).terms
        assert len(terms) == corpus.PROFILE_TERM_LIMIT

    def test_constants(self):
        assert corpus.CORPUS_DOC_ARM_WEIGHT == 0.25
        assert corpus.CORPUS_KW_FULL_WEIGHT == 2.0
        assert corpus.PROFILE_HEAD_CHARS == 1500
        assert corpus.CORPUS_TERM_RE.findall("a1 b-c") == ["a1", "b", "c"]


class TestProfileWrittenAtWarm:
    def test_profile_lands_with_pages(self, cache, pdf):
        corpus.warm_docs(
            [pdf], 600, cache, embeddings=True, model_name="m1", embed=_embed_len
        )
        prof = cache.get_doc_profiles([pdf], "m1")
        assert pdf in prof and prof[pdf] is not None
        assert "budget" in cache.get_doc_terms([pdf])[pdf]

    def test_text_only_warm_writes_no_profile(self, cache, pdf):
        corpus.warm_docs([pdf], 600, cache, embeddings=False, model_name="m1")
        assert cache.get_doc_profiles([pdf], "m1") == {}

    def test_encode_failure_is_not_a_warm_failure(self, cache, pdf):
        calls = {"n": 0}

        def flaky(texts):
            calls["n"] += 1
            if calls["n"] == 2:  # first call embeds pages, second is the head
                raise RuntimeError("encoder hiccup")
            return _embed_len(texts)

        out = corpus.warm_docs(
            [pdf], 600, cache, embeddings=True, model_name="m1", embed=flaky
        )
        assert out["skipped"] == []
        assert out["warmed_this_call"] == 1
        assert cache.get_doc_profiles([pdf], "m1") == {}
        assert cache.embeddings_complete(pdf, "m1")


class TestBackfill:
    def _warm_text_only(self, cache, paths):
        corpus.warm_docs(paths, 600, cache, embeddings=False, model_name="m1")

    def test_backfills_exactly_the_missing_docs(self, cache, tmp_path):
        a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
        _make_pdf(a, ["alpha budget"])
        _make_pdf(b, ["bravo budget"])
        paths = [str(a), str(b)]
        self._warm_text_only(cache, paths)
        cache.save_doc_profile(
            str(a), 1500, b"\x01" * 8, {"alpha": 1}, "m1", head_page=0
        )
        seen = []

        def spy(texts):
            seen.extend(texts)
            return _embed_len(texts)

        n = corpus.backfill_doc_profiles(paths, cache, "m1", spy)
        assert n == 1
        # Only b.pdf's head was encoded; a.pdf already had a valid row.
        assert len(seen) == 1 and "bravo" in seen[0] and "alpha" not in seen[0]
        assert set(cache.get_doc_profiles(paths, "m1")) == set(paths)

    def test_full_coverage_writes_nothing(self, cache, pdf):
        self._warm_text_only(cache, [pdf])
        corpus.backfill_doc_profiles([pdf], cache, "m1", _embed_len)

        def boom(texts):
            raise AssertionError("no encode expected")

        assert corpus.backfill_doc_profiles([pdf], cache, "m1", boom) == 0

    def test_empty_page1_is_profiled_from_page_two(self, cache, tmp_path):
        p = tmp_path / "blank.pdf"
        _make_pdf(p, ["", "budget on page two"])
        self._warm_text_only(cache, [str(p)])
        assert corpus.backfill_doc_profiles([str(p)], cache, "m1", _embed_len) == 1
        assert cache.get_doc_profiles([str(p)], "m1")[str(p)] is not None
        assert cache.get_doc_profile_heads([str(p)], "m1") == {str(p): 1}
        assert corpus.backfill_doc_profiles([str(p)], cache, "m1", _embed_len) == 0

    def test_model_change_rewrites(self, cache, pdf):
        self._warm_text_only(cache, [pdf])
        corpus.backfill_doc_profiles([pdf], cache, "m1", _embed_len)
        assert corpus.backfill_doc_profiles([pdf], cache, "m2", _embed_len) == 1
        assert cache.get_doc_profiles([pdf], "m1") == {}

    def test_unwarmed_doc_is_skipped(self, cache, pdf):
        assert corpus.backfill_doc_profiles([pdf], cache, "m1", _embed_len) == 0

    def test_one_failing_encode_does_not_block_the_rest(self, cache, tmp_path):
        a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
        _make_pdf(a, ["alpha budget"])
        _make_pdf(b, ["bravo budget"])
        paths = [str(a), str(b)]
        self._warm_text_only(cache, paths)

        def flaky(texts):
            if "alpha" in texts[0]:
                raise RuntimeError("nope")
            return _embed_len(texts)

        assert corpus.backfill_doc_profiles(paths, cache, "m1", flaky) == 1
        assert list(cache.get_doc_profiles(paths, "m1")) == [str(b)]

    def test_warm_with_embeddings_backfills_cached_docs(self, cache, pdf):
        self._warm_text_only(cache, [pdf])
        out = corpus.warm_docs(
            [pdf], 600, cache, embeddings=True, model_name="m1", embed=_embed_len
        )
        # doc was text-warm but had no embeddings, so it re-warms with them
        assert pdf in cache.get_doc_profiles([pdf], "m1")
        # now fully cached: a second embeddings warm still repairs a lost profile
        cache.save_doc_profile(pdf, 1500, b"\x00" * 8, {}, "other-model")
        out = corpus.warm_docs(
            [pdf], 600, cache, embeddings=True, model_name="m1", embed=_embed_len
        )
        assert out["docs"][0]["status"] == "cached"
        assert pdf in cache.get_doc_profiles([pdf], "m1")


class TestAboutTerms:
    def test_distinctive_terms_rank_first(self, cache, tmp_path):
        paths = []
        for name, terms in [
            ("a.pdf", {"shared": 9, "alpha": 3}),
            ("b.pdf", {"shared": 9, "bravo": 3}),
            ("c.pdf", {"shared": 9, "charlie": 3}),
        ]:
            p = tmp_path / name
            _make_pdf(p, ["x"])
            cache.save_doc_profile(str(p), 1500, None, terms, "m1")
            paths.append(str(p))
        about = corpus.about_terms(cache, paths)
        # shared: 9*log(1+3/3)=6.24 ; alpha: 3*log(1+3/1)=4.16 -> shared first
        assert about[paths[0]] == ["shared", "alpha"]
        assert about[paths[1]] == ["shared", "bravo"]

    def test_limit_and_unprofiled(self, cache, tmp_path):
        p = tmp_path / "a.pdf"
        _make_pdf(p, ["x"])
        cache.save_doc_profile(
            str(p), 1500, None, {f"term{i:02d}": 1 for i in range(20)}, "m1"
        )
        q = tmp_path / "b.pdf"
        _make_pdf(q, ["x"])
        about = corpus.about_terms(cache, [str(p), str(q)])
        assert len(about[str(p)]) == 8
        assert about[str(q)] == []

    def test_card_carries_about(self, cache, pdf):
        corpus.warm_docs(
            [pdf], 600, cache, embeddings=True, model_name="m1", embed=_embed_len
        )
        about = corpus.about_terms(cache, [pdf])
        card = corpus.build_overview_card(pdf, cache, from_cache=True, about=about[pdf])
        assert card["about"] == about[pdf]
        assert "budget" in card["about"]
        bare = corpus.build_overview_card(pdf, cache, from_cache=True)
        assert bare["about"] == []


def test_census_blank_chars_match_cache():
    from pdf_mcp import cache as cache_mod
    from scripts import doc_profile_census

    assert doc_profile_census.TEXT_BLANK_CHARS == cache_mod.TEXT_BLANK_CHARS


def _seed(cache, tmp_path, texts, name="doc.pdf"):
    """A PDF with len(texts) pages whose cached page_text is exactly
    ``texts`` (written directly, no extraction)."""
    p = tmp_path / name
    _make_pdf(p, [""] * len(texts))
    path = str(p)
    cache.save_metadata(path, len(texts), {}, [])
    cache.save_pages_text(path, dict(enumerate(texts)))
    return path


def _spy():
    seen: list[str] = []

    def embed(texts):
        seen.extend(texts)
        return _embed_len(texts)

    return seen, embed


class TestProfileStaleness:
    def test_legacy_null_row_heals_once(self, cache, tmp_path):
        p = _seed(cache, tmp_path, ["", "budget on page two"])
        cache.save_doc_profile(p, 1500, None, {}, "m1", head_page=None)
        seen, embed = _spy()
        assert corpus.backfill_doc_profiles([p], cache, "m1", embed) == 1
        assert cache.get_doc_profile_heads([p], "m1") == {p: 1}
        assert cache.get_doc_profiles([p], "m1")[p] is not None
        assert len(seen) == 1
        assert corpus.backfill_doc_profiles([p], cache, "m1", embed) == 0
        assert len(seen) == 1

    def test_no_text_doc_never_reencodes(self, cache, tmp_path):
        p = _seed(cache, tmp_path, ["", ""])
        seen, embed = _spy()
        for _ in range(3):
            corpus.backfill_doc_profiles([p], cache, "m1", embed)
        assert seen == []
        assert cache.get_doc_profile_heads([p], "m1") == {p: -1}

    def test_formfeed_only_doc_is_stable(self, cache, tmp_path):
        p = _seed(cache, tmp_path, ["\f", "\n\t"])
        seen, embed = _spy()
        written = [corpus.backfill_doc_profiles([p], cache, "m1", embed)]
        written += [corpus.backfill_doc_profiles([p], cache, "m1", embed)]
        written += [corpus.backfill_doc_profiles([p], cache, "m1", embed)]
        assert seen == []
        assert written == [1, 0, 0]
        assert cache.get_doc_profile_heads([p], "m1") == {p: -1}

    def test_ocr_on_earlier_page_moves_head(self, cache, tmp_path):
        p = _seed(cache, tmp_path, ["", "", "budget on page three"])
        corpus.backfill_doc_profiles([p], cache, "m1", _embed_len)
        assert cache.get_doc_profile_heads([p], "m1") == {p: 2}
        cache.save_page_text(p, 0, "cover title text", source="ocr", ocr_lang="eng")
        seen, embed = _spy()
        assert corpus.backfill_doc_profiles([p], cache, "m1", embed) == 1
        assert seen == ["cover title text"]
        assert cache.get_doc_profile_heads([p], "m1") == {p: 0}
        # page 0 now holds two rows, so it is checked, but nothing moved
        assert corpus.backfill_doc_profiles([p], cache, "m1", embed) == 0
        assert len(seen) == 1

    def test_empty_ocr_row_hides_page0_text(self, cache, tmp_path):
        p = _seed(cache, tmp_path, ["", "budget on page two"])
        cache.save_page_text(p, 0, "deutscher titel", source="ocr", ocr_lang="deu")
        corpus.backfill_doc_profiles([p], cache, "m1", _embed_len)
        assert cache.get_doc_profile_heads([p], "m1") == {p: 0}
        # A later empty OCR row is now the latest row for page 0, which is
        # what get_pages_text (no language) reads.
        cache.save_page_text(p, 0, "", source="ocr", ocr_lang="eng")
        seen, embed = _spy()
        assert corpus.backfill_doc_profiles([p], cache, "m1", embed) == 1
        assert seen == ["budget on page two"]
        assert cache.get_doc_profile_heads([p], "m1") == {p: 1}

    def test_single_row_page0_is_never_checked(self, cache, tmp_path, monkeypatch):
        p = _seed(cache, tmp_path, ["budget on page one", "more"])
        corpus.backfill_doc_profiles([p], cache, "m1", _embed_len)
        assert cache.get_doc_profile_heads([p], "m1") == {p: 0}

        def boom(paths):
            raise AssertionError("first_text_pages must not run")

        monkeypatch.setattr(cache, "first_text_pages", boom)
        assert corpus.backfill_doc_profiles([p], cache, "m1", _embed_len) == 0

    def test_stale_writer_after_ocr_is_corrected(self, cache, tmp_path):
        p = _seed(cache, tmp_path, ["", "", "budget on page three"])
        cache.save_page_text(p, 0, "cover title text", source="ocr", ocr_lang="eng")
        # An embed pass that read the text before the OCR write lands last.
        cache.save_doc_profile(p, 1500, b"\x01" * 8, {}, "m1", head_page=2)
        seen, embed = _spy()
        assert corpus.backfill_doc_profiles([p], cache, "m1", embed) == 1
        assert seen == ["cover title text"]
        assert cache.get_doc_profile_heads([p], "m1") == {p: 0}

    def test_about_terms_survive_ocr_write(self, cache, tmp_path):
        p = _seed(cache, tmp_path, ["", "budget on page two"])
        cache.save_doc_profile(p, 1500, None, {"budget": 3}, "m1", head_page=1)
        cache.save_page_text(p, 0, "cover title text", source="ocr", ocr_lang="eng")
        assert cache.get_doc_terms([p]) == {p: {"budget": 3}}


class TestFirstTextPages:
    def test_lowest_nonblank_latest_row(self, cache, tmp_path):
        a = _seed(cache, tmp_path, ["\f", " \t", "text"], name="a.pdf")
        b = _seed(cache, tmp_path, ["\n", ""], name="b.pdf")
        c = tmp_path / "c.pdf"
        _make_pdf(c, ["x"])
        assert cache.first_text_pages([a, b, str(c)]) == {a: 2, b: -1}

    def test_embedded_nul_is_text(self, cache, tmp_path):
        p = _seed(cache, tmp_path, ["\x00abc", "text"])
        assert cache.first_text_pages([p]) == {p: 0}

    def test_stale_mtime_rows_are_ignored(self, cache, tmp_path):
        p = _seed(cache, tmp_path, ["text"])
        future = time.time() + 5
        os.utime(p, (future, future))
        assert cache.first_text_pages([p]) == {}

    def test_multi_row_pages(self, cache, tmp_path):
        a = _seed(cache, tmp_path, ["one"], name="a.pdf")
        b = _seed(cache, tmp_path, ["one"], name="b.pdf")
        cache.save_page_text(a, 0, "ocr", source="ocr", ocr_lang="eng")
        assert cache.multi_row_pages([a, b], 0) == {a}
