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
        vec, _terms = corpus.build_doc_profile(texts, spy)
        assert seen == ["x" * 1500]
        assert vec is not None

    def test_empty_page1_gives_no_vector_and_no_encode(self):
        def boom(texts):
            raise AssertionError("must not encode an empty head")

        vec, terms = corpus.build_doc_profile({0: "   ", 1: "budget budget"}, boom)
        assert vec is None
        assert terms == {"budget": 2}

    def test_terms_are_4plus_chars_across_all_pages_capped(self):
        texts = {0: "the cat budget", 1: "Budget REPORT report a"}
        _vec, terms = corpus.build_doc_profile(texts, _embed_len)
        assert terms == {"budget": 2, "report": 2}
        many = {0: " ".join(f"term{i:04d}" for i in range(500))}
        _vec, terms = corpus.build_doc_profile(many, _embed_len)
        assert len(terms) == corpus.PROFILE_TERM_LIMIT

    def test_constants(self):
        assert corpus.CORPUS_DOC_ARM_WEIGHT == 0.25
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
