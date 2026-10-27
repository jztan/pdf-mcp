"""Extraction jobs outlive the warm call that started them (issue #82)."""

import os
import time
from pathlib import Path

import pymupdf
import pytest

from pdf_mcp import corpus, extractor
from pdf_mcp.cache import PDFCache
from pdf_mcp.warm_pool import WarmPool

SLOW_SECONDS = 3.0


def _slow_extract(path, want_sections=False):
    """A long scan stand-in. Module-level so a spawn worker can import it."""
    time.sleep(SLOW_SECONDS)
    return extractor._warm_extract_worker(path, want_sections)


def _echo(path, want_sections=False):
    return (path, want_sections)


def _sleep_forever(path, want_sections=False):
    time.sleep(600)


def _pdf(path: Path, text: str = "warm pool body text") -> str:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def pool():
    p = WarmPool(lambda: 2)
    yield p
    p.shutdown_now()


class TestWarmPool:
    def test_same_file_reuses_the_job(self, pool, tmp_path):
        path = _pdf(tmp_path / "a.pdf")
        first = pool.submit(_echo, path, False)
        assert pool.submit(_echo, path, False) is first

    def test_job_without_sections_is_not_reused_for_sections(self, pool, tmp_path):
        path = _pdf(tmp_path / "a.pdf")
        plain = pool.submit(_echo, path, False)
        with_sections = pool.submit(_echo, path, True)
        assert with_sections is not plain
        assert pool.submit(_echo, path, False) is with_sections

    def test_changed_file_gets_a_new_job(self, pool, tmp_path):
        path = _pdf(tmp_path / "a.pdf")
        first = pool.submit(_echo, path, False)
        first.result(timeout=60)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        assert pool.submit(_echo, path, False) is not first

    def test_take_finished_drops_results_for_changed_files(self, pool, tmp_path):
        kept = _pdf(tmp_path / "kept.pdf")
        changed = _pdf(tmp_path / "changed.pdf")
        for path in (kept, changed):
            pool.submit(_echo, path, False).result(timeout=60)
        st = os.stat(changed)
        os.utime(changed, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        assert [p for p, _ in pool.take_finished()] == [kept]
        assert pool.take_finished() == []

    def test_release_if_idle_keeps_a_running_pool(self, pool, tmp_path):
        path = _pdf(tmp_path / "a.pdf")
        pool.submit(_sleep_forever, path, False)
        pool.release_if_idle()
        assert pool.running() == [path]

    def test_shutdown_now_terminates_running_workers(self, tmp_path):
        p = WarmPool(lambda: 1)
        fut = p.submit(_sleep_forever, _pdf(tmp_path / "a.pdf"), False)
        deadline = time.monotonic() + 60
        while not fut.running() and time.monotonic() < deadline:
            time.sleep(0.05)
        procs = list(p._pool._processes.values())
        start = time.monotonic()
        p.shutdown_now()
        for proc in procs:
            proc.join(timeout=30)
            assert not proc.is_alive()
        assert time.monotonic() - start < 30
        assert p.running() == []


class TestWarmDeadline:
    """warm_docs returns at its deadline with a slow doc still extracting."""

    def test_returns_at_deadline_and_next_call_collects(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PDF_MCP_MAX_WORKERS", raising=False)
        monkeypatch.setattr(corpus, "_warm_extract_worker", _slow_extract)
        cache = PDFCache(cache_dir=tmp_path / "cache", ttl_hours=1)
        path = _pdf(tmp_path / "slow.pdf")

        start = time.monotonic()
        out = corpus.warm_docs([path], 1, cache)
        elapsed = time.monotonic() - start
        assert elapsed < SLOW_SECONDS, elapsed
        assert out["unprocessed"] == [path]
        assert out["budget_exhausted"] is True
        assert out["warm_complete"] is False
        assert corpus.WARM_POOL.running() == [path]

        # The job keeps running after the call returned; the next call
        # writes its result instead of extracting the doc again.
        deadline = time.monotonic() + 60
        while corpus.WARM_POOL.running() and time.monotonic() < deadline:
            time.sleep(0.1)
        monkeypatch.setattr(
            corpus,
            "_warm_extract_worker",
            lambda *a: pytest.fail("re-extracted a doc the pool already had"),
        )
        done = corpus.warm_docs([path], 30, cache)
        assert done["warm_complete"] is True
        assert [d["status"] for d in done["docs"]] == ["cached"]
        assert cache.get_pages_text(path, [0])[0].startswith("warm pool body")

    def test_next_call_adopts_a_job_still_running(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PDF_MCP_MAX_WORKERS", raising=False)
        monkeypatch.setattr(corpus, "_warm_extract_worker", _slow_extract)
        cache = PDFCache(cache_dir=tmp_path / "cache", ttl_hours=1)
        path = _pdf(tmp_path / "slow.pdf")

        first = corpus.warm_docs([path], 1, cache)
        assert first["unprocessed"] == [path]
        job = corpus.WARM_POOL.submit(corpus._warm_extract_worker, path, False)

        done = corpus.warm_docs([path], 60, cache)
        assert done["warm_complete"] is True
        assert [d["status"] for d in done["docs"]] == ["warmed"]
        assert job.done()


def test_infinite_budget_waits_for_a_slow_doc(tmp_path, monkeypatch):
    """pdf-mcp-warm passes budget_seconds=inf; the pool wait must accept it."""
    monkeypatch.delenv("PDF_MCP_MAX_WORKERS", raising=False)
    monkeypatch.setattr(corpus, "_warm_extract_worker", _slow_extract)
    cache = PDFCache(cache_dir=tmp_path / "cache", ttl_hours=1)
    path = _pdf(tmp_path / "slow.pdf")
    out = corpus.warm_docs([path], float("inf"), cache)
    assert out["warm_complete"] is True
    assert [d["status"] for d in out["docs"]] == ["warmed"]
