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
