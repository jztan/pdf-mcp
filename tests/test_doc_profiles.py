"""Document profiles: the cached per-doc head vector + term counts that
back the hybrid corpus search's document arm and overview `about`."""

import os
import time

import numpy as np
import pymupdf
import pytest

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
