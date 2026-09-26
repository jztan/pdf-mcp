"""The process-wide pdfium lock: fair, re-entrant, yieldable."""

import threading
import time

import pytest

from pdf_mcp import concurrency
from pdf_mcp.concurrency import FairLock, pdf_access, yield_pdf_access


def _start(fn):
    t = threading.Thread(target=fn)
    t.start()
    return t


def _wait_for_waiters(lock, n, timeout=5.0):
    end = time.monotonic() + timeout
    while lock.waiters() < n:
        assert time.monotonic() < end, "waiters never queued"
        time.sleep(0.005)


def test_waiters_are_served_in_arrival_order():
    lock = FairLock()
    order = []
    lock.acquire()
    threads = []
    for n in range(4):

        def take(n=n):
            with lock:
                order.append(n)

        threads.append(_start(take))
        _wait_for_waiters(lock, n + 1)
    lock.release()
    for t in threads:
        t.join()
    assert order == [0, 1, 2, 3]


def test_the_holder_can_reenter_without_deadlock():
    lock = FairLock()
    with lock:
        with lock:
            assert lock.held_by_current_thread()
        assert lock.held_by_current_thread()
    assert not lock.held_by_current_thread()


def test_release_by_a_thread_that_does_not_hold_it_raises():
    lock = FairLock()
    with pytest.raises(RuntimeError):
        lock.release()


def test_yield_hands_the_lock_to_the_head_waiter_then_resumes():
    lock = FairLock()
    events = []
    lock.acquire()
    lock.acquire()  # depth 2: the yield must restore it

    def waiter():
        with lock:
            events.append("waiter ran")

    t = _start(waiter)
    _wait_for_waiters(lock, 1)
    lock.yield_turn()
    events.append("holder resumed")
    assert lock.held_by_current_thread()
    lock.release()
    assert lock.held_by_current_thread()  # still depth 1
    lock.release()
    t.join()
    assert events == ["waiter ran", "holder resumed"]


def test_yield_with_nobody_waiting_keeps_the_lock():
    lock = FairLock()
    with lock:
        lock.yield_turn()
        assert lock.held_by_current_thread()


def test_module_yield_is_a_noop_without_the_lock():
    yield_pdf_access()  # e.g. the offline pdf-mcp-warm CLI: must not raise


def test_decorator_holds_the_shared_lock_and_marks_the_function():
    seen = []

    @pdf_access
    def tool(x: int) -> int:
        seen.append(concurrency.PDF_ACCESS.held_by_current_thread())
        return x + 1

    assert tool(1) == 2
    assert seen == [True]
    assert tool.__pdf_access__ is True
    assert tool.__wrapped__.__name__ == "tool"
    assert not concurrency.PDF_ACCESS.held_by_current_thread()


def test_decorated_calls_never_overlap():
    active = []
    overlap = []

    @pdf_access
    def tool() -> None:
        active.append(1)
        if len(active) > 1:
            overlap.append(True)
        time.sleep(0.01)
        active.pop()

    threads = [_start(tool) for _ in range(8)]
    for t in threads:
        t.join()
    assert overlap == []


LOCKED = {
    "pdf_info",
    "pdf_get_toc",
    "pdf_read_pages",
    "pdf_read_all",
    "pdf_search",
    "pdf_render_pages",
    "pdf_extract_chart",
    "pdf_corpus_warm",
    "pdf_corpus_overview",
    "pdf_corpus_search",
}
EXEMPT = {"server_info", "pdf_cache_stats", "pdf_cache_clear"}


def _registered_tools():
    import asyncio

    import pdf_mcp.server  # noqa: F401 - registers every tool
    from pdf_mcp import _core

    return asyncio.run(_core.mcp.list_tools())


def test_every_tool_is_classified_locked_or_exempt():
    names = {t.name for t in _registered_tools()}
    assert names == LOCKED | EXEMPT, (
        f"unclassified: {sorted(names - LOCKED - EXEMPT)}; "
        f"missing: {sorted((LOCKED | EXEMPT) - names)}. A new tool that opens "
        "a PDF must be decorated with @pdf_access and added to LOCKED."
    )


def test_locked_tools_run_under_the_lock_and_exempt_ones_do_not():
    for tool in _registered_tools():
        marked = getattr(tool.fn, "__pdf_access__", False)
        assert marked is (tool.name in LOCKED), tool.name


def test_the_decorator_leaves_every_schema_unchanged():
    import inspect

    from fastmcp.tools import FunctionTool

    for tool in _registered_tools():
        if tool.name in LOCKED:
            bare = FunctionTool.from_function(inspect.unwrap(tool.fn))
            assert tool.parameters == bare.parameters, tool.name


def test_a_long_read_all_lets_a_queued_call_run_between_pages(
    tmp_path, isolated_server, monkeypatch
):
    from tests._pdfium_race_workload import make_pdf

    from pdf_mcp import concurrency
    from pdf_mcp.tools import read as read_mod

    pdf_path = str(tmp_path / "long.pdf")
    make_pdf(pdf_path, n_pages=4)

    events: list[str] = []
    long_started = threading.Event()
    real_extract = read_mod.extract_text_from_page

    def fake_extract(page, *args, **kwargs):
        page_num = page.number
        events.append(f"page {page_num}")
        if page_num == 0:
            long_started.set()
            end = time.monotonic() + 5.0
            while concurrency.PDF_ACCESS.waiters() < 1:
                assert time.monotonic() < end, "short call never queued"
                time.sleep(0.005)
        return real_extract(page, *args, **kwargs)

    monkeypatch.setattr(read_mod, "extract_text_from_page", fake_extract)

    def long_call():
        read_mod.pdf_read_all(pdf_path, max_pages=4)

    def short_call():
        long_started.wait(5)
        with concurrency.PDF_ACCESS:
            events.append("short call")

    t1, t2 = _start(long_call), _start(short_call)
    t1.join(10)
    t2.join(10)
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert events == ["page 0", "short call", "page 1", "page 2", "page 3"]


def test_resolve_hidden_flags_lets_a_queued_call_run_between_pages(
    tmp_path, isolated_server, monkeypatch
):
    """_core._resolve_hidden_flags (reached from pdf_read_all and
    pdf_search) is a page-at-a-time pdfium loop of its own, and profiling
    showed it accounts for most of pdf_read_all's non-extraction hold, so
    it needs the same between-pages yield."""
    import pymupdf

    from tests._pdfium_race_workload import make_pdf

    from pdf_mcp import _core, concurrency, content_trust

    pdf_path = str(tmp_path / "hidden.pdf")
    make_pdf(pdf_path, n_pages=4)

    events: list[str] = []
    long_started = threading.Event()
    real_check = content_trust.page_has_hidden_text

    def fake_check(page, *args, **kwargs):
        page_num = page.number
        events.append(f"page {page_num}")
        if page_num == 0:
            long_started.set()
            end = time.monotonic() + 5.0
            while concurrency.PDF_ACCESS.waiters() < 1:
                assert time.monotonic() < end, "short call never queued"
                time.sleep(0.005)
        return real_check(page, *args, **kwargs)

    monkeypatch.setattr(content_trust, "page_has_hidden_text", fake_check)

    doc = pymupdf.open(pdf_path)
    try:

        def long_call():
            with concurrency.PDF_ACCESS:
                _core._resolve_hidden_flags(pdf_path, doc, [0, 1, 2, 3])

        def short_call():
            long_started.wait(5)
            with concurrency.PDF_ACCESS:
                events.append("short call")

        t1, t2 = _start(long_call), _start(short_call)
        t1.join(10)
        t2.join(10)
        assert not t1.is_alive()
        assert not t2.is_alive()
        assert events == ["page 0", "short call", "page 1", "page 2", "page 3"]
    finally:
        doc.close()


def test_a_long_keyword_search_lets_a_queued_call_run_between_pages(
    tmp_path, isolated_server, monkeypatch
):
    """pdf_search twin of the pdf_read_all test above, pinning the yield
    in search.py's keyword-mode text-extraction loop: it is the loop that
    does real per-page pdfium work for a cold hybrid/auto search, since
    mode='auto' runs this loop first and caches every page's text before
    the semantic/hybrid loops ever see a miss."""
    from tests._pdfium_race_workload import make_pdf

    from pdf_mcp import concurrency
    from pdf_mcp.tools import search as search_mod

    pdf_path = str(tmp_path / "search.pdf")
    make_pdf(pdf_path, n_pages=4)

    events: list[str] = []
    long_started = threading.Event()
    real_extract = search_mod.extract_text_from_page

    def fake_extract(page, *args, **kwargs):
        page_num = page.number
        events.append(f"page {page_num}")
        if page_num == 0:
            long_started.set()
            end = time.monotonic() + 5.0
            while concurrency.PDF_ACCESS.waiters() < 1:
                assert time.monotonic() < end, "short call never queued"
                time.sleep(0.005)
        return real_extract(page, *args, **kwargs)

    monkeypatch.setattr(search_mod, "extract_text_from_page", fake_extract)

    def long_call():
        search_mod.pdf_search(pdf_path, "supply voltage", mode="keyword")

    def short_call():
        long_started.wait(5)
        with concurrency.PDF_ACCESS:
            events.append("short call")

    t1, t2 = _start(long_call), _start(short_call)
    t1.join(10)
    t2.join(10)
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert events == ["page 0", "short call", "page 1", "page 2", "page 3"]


def test_a_long_warm_lets_a_queued_call_run_between_documents(monkeypatch):
    from pdf_mcp import concurrency, corpus

    events = []
    started = threading.Event()

    def fake_warm_one(path, *a, **k):
        events.append(f"warm {path}")
        if path == "a.pdf":
            started.set()
            end = time.monotonic() + 5.0
            while concurrency.PDF_ACCESS.waiters() < 1:
                assert time.monotonic() < end, "short call never queued"
                time.sleep(0.005)
        raise RuntimeError("skip")  # _warm_sequential records it as skipped

    monkeypatch.setattr(corpus, "_warm_one_doc", fake_warm_one)
    monkeypatch.setattr(corpus, "_cached_pages", lambda *a, **k: None)

    def warm():
        with concurrency.PDF_ACCESS:
            corpus._warm_sequential(
                [("a.pdf", 1), ("b.pdf", 1)],
                60.0,
                time.monotonic(),
                time.monotonic,
                cache=None,
                embeddings=False,
                model_name=None,
                embed=None,
                docs=[],
                skipped=[],
                emb_cached=lambda p: False,
            )

    def short_call():
        started.wait(5)
        with concurrency.PDF_ACCESS:
            events.append("short call")

    t1, t2 = _start(warm), _start(short_call)
    t1.join(10)
    t2.join(10)
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert events == ["warm a.pdf", "short call", "warm b.pdf"]


def test_a_long_section_backfill_lets_a_queued_call_run_between_documents(
    monkeypatch,
):
    """backfill_sections twin of the warm-loop test above: it runs
    derive_sections doc after doc (pdfium work) for pdf_corpus_warm's
    sections=True pass, so it needs the same between-documents yield."""
    from pdf_mcp import concurrency, corpus, section_detector

    events = []
    started = threading.Event()

    def fake_derive(path, *a, **k):
        events.append(f"backfill {path}")
        if path == "a.pdf":
            started.set()
            end = time.monotonic() + 5.0
            while concurrency.PDF_ACCESS.waiters() < 1:
                assert time.monotonic() < end, "short call never queued"
                time.sleep(0.005)
        raise RuntimeError("skip")  # backfill_sections logs and continues

    monkeypatch.setattr(section_detector, "derive_sections", fake_derive)

    class FakeCache:
        def get_section_fts_coverage(self, path):
            return 0

    def backfill():
        with concurrency.PDF_ACCESS:
            corpus.backfill_sections(["a.pdf", "b.pdf"], FakeCache())

    def short_call():
        started.wait(5)
        with concurrency.PDF_ACCESS:
            events.append("short call")

    t1, t2 = _start(backfill), _start(short_call)
    t1.join(10)
    t2.join(10)
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert events == ["backfill a.pdf", "short call", "backfill b.pdf"]
