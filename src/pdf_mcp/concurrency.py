"""One PDF engine call at a time, across every tool call in the process.

pdfium is not thread-safe, and fastmcp runs each synchronous tool call on
its own worker thread (anyio.to_thread). Two calls touching pdfium at once
crashed the server (SIGSEGV / SIGABRT) or left it unable to open any PDF
until restart. Every tool that opens a PDF therefore runs under
``PDF_ACCESS``.

The lock is FIFO, not a plain ``threading.Lock``: long corpus loops hand it
to waiting calls between documents (``yield_pdf_access``), and with an
unfair lock the yielding thread usually wins it straight back. It is
re-entrant because tool bodies share helpers.

A corpus call waits for the lock no longer than its own budget
(``pdf_access_within``): queued behind another corpus call, it would
otherwise sit out that call's budget before starting its own and reach
the client's timeout (issue #82, where Claude Desktop cancelled two queued
calls at 4 minutes). A waiter that gives up leaves its ticket behind;
the lock skips abandoned tickets when it moves to the next one.

Background work (issue #89, embeddings between warm calls) holds the same
lock but never queues for it: it takes the lock only when no call holds it
or waits for it (``background_access``), and inside it every yield gives
the lock up until the waiting calls are all done.

Stdlib only: imported by ``corpus`` (pure logic) and by every tool module.
"""

from __future__ import annotations

import functools
import inspect
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class BackgroundStopped(Exception):
    """A background job was stopped while it waited for the lock."""


class FairLock:
    """A re-entrant ticket lock: waiters are served in arrival order."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._next_ticket = 0
        self._serving = 0
        self._owner: int | None = None
        self._depth = 0
        self._abandoned: set[int] = set()

    def _wait_for(self, ticket: int, deadline: float | None = None) -> bool:
        while not (self._owner is None and self._serving == ticket):
            if deadline is None:
                self._cond.wait()
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._cond.wait(remaining)
        return True

    def _advance(self) -> None:
        """Serve the next ticket, skipping ones whose waiter gave up."""
        self._serving += 1
        while self._serving in self._abandoned:
            self._abandoned.discard(self._serving)
            self._serving += 1

    def acquire(self, timeout: float | None = None) -> bool:
        """Take the lock in arrival order; False if ``timeout`` ran out."""
        me = threading.get_ident()
        with self._cond:
            if self._owner == me:
                self._depth += 1
                return True
            ticket = self._next_ticket
            self._next_ticket += 1
            deadline = None if timeout is None else time.monotonic() + timeout
            if not self._wait_for(ticket, deadline):
                self._abandoned.add(ticket)
                return False
            self._owner = me
            self._depth = 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._owner != threading.get_ident():
                raise RuntimeError(
                    "FairLock released by a thread that does not hold it"
                )
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._advance()
                self._cond.notify_all()

    def __enter__(self) -> FairLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def held_by_current_thread(self) -> bool:
        with self._cond:
            return self._owner == threading.get_ident()

    def waiters(self) -> int:
        """Threads queued behind the holder (0 when free or uncontended)."""
        with self._cond:
            queued = self._next_ticket - self._serving - len(self._abandoned)
            return queued - 1 if self._owner is not None else queued

    def yield_turn(self) -> None:
        """Let every thread already waiting run once, then resume.

        A no-op when nobody waits. Keeps the caller's re-entry depth, so a
        yield from inside nested helpers resumes exactly where it was.
        """
        me = threading.get_ident()
        with self._cond:
            if self._owner != me:
                raise RuntimeError("yield_turn by a thread that does not hold the lock")
            if self._next_ticket - self._serving - len(self._abandoned) - 1 <= 0:
                return
            depth = self._depth
            self._owner = None
            self._depth = 0
            self._advance()
            ticket = self._next_ticket
            self._next_ticket += 1
            self._cond.notify_all()
            self._wait_for(ticket)
            self._owner = me
            self._depth = depth

    def acquire_when_idle(self, stop: threading.Event, poll: float = 0.05) -> bool:
        """Take the lock once nobody holds it or waits for it.

        For background work: it never takes a ticket while a call is
        active, so a call never queues behind it. False once ``stop`` is
        set, without the lock.
        """
        me = threading.get_ident()
        with self._cond:
            while not stop.is_set():
                queued = self._next_ticket - self._serving - len(self._abandoned)
                if self._owner is None and queued == 0:
                    # Every ticket left in the range was abandoned.
                    self._abandoned.clear()
                    self._serving = self._next_ticket
                    self._next_ticket += 1
                    self._owner = me
                    self._depth = 1
                    return True
                self._cond.wait(poll)
            return False

    def yield_to_waiters(self, stop: threading.Event) -> None:
        """``yield_turn`` for background work: when anyone waits, release
        and take the lock back only once it is idle again, so a call that
        yields mid-way (between pages) finishes before background work
        resumes. Raises BackgroundStopped, without the lock, once ``stop``
        is set.
        """
        me = threading.get_ident()
        with self._cond:
            if self._owner != me:
                raise RuntimeError(
                    "yield_to_waiters by a thread that does not hold the lock"
                )
            waiting = self._next_ticket - self._serving - len(self._abandoned) - 1
            if waiting <= 0 and not stop.is_set():
                return
            depth = self._depth
            self._owner = None
            self._depth = 0
            self._advance()
            self._cond.notify_all()
        if not self.acquire_when_idle(stop):
            raise BackgroundStopped
        with self._cond:
            self._depth = depth


PDF_ACCESS = FairLock()


def pdf_access(fn: Callable[P, R]) -> Callable[P, R]:
    """Run a tool body under ``PDF_ACCESS``. Apply beneath ``@mcp.tool``.

    ``functools.wraps`` keeps the signature fastmcp builds the schema
    from. The global is read at call time, so tests can swap it.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with PDF_ACCESS:
            return fn(*args, **kwargs)

    wrapper.__pdf_access__ = True  # type: ignore[attr-defined]
    return wrapper


_wait = threading.local()


def lock_wait_seconds() -> float:
    """How long the current ``pdf_access_within`` call queued for the lock.

    A corpus tool subtracts it from its budget, so queueing and warming
    together stay within ``budget_seconds``.
    """
    return float(getattr(_wait, "seconds", 0.0))


def pdf_access_within(
    budget_param: str, busy: Callable[[float], R]
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """``pdf_access`` for a call with a time budget: wait no longer than it.

    ``budget_param`` names the tool argument holding the budget in
    seconds (clamped to 1..300, as the corpus tools clamp it). When the
    lock is still taken at the end of the budget, the call returns
    ``busy(waited_seconds)`` instead of running.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = signature.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            try:
                budget = float(bound.arguments[budget_param])
            except (KeyError, TypeError, ValueError):
                budget = 300.0
            budget = min(max(budget, 1.0), 300.0)
            began = time.monotonic()
            if not PDF_ACCESS.acquire(timeout=budget):
                return busy(time.monotonic() - began)
            _wait.seconds = time.monotonic() - began
            try:
                return fn(*args, **kwargs)
            finally:
                _wait.seconds = 0.0
                PDF_ACCESS.release()

        wrapper.__pdf_access__ = True  # type: ignore[attr-defined]
        return wrapper

    return decorate


def yield_pdf_access() -> None:
    """Between documents in a long loop: let waiting calls in, then resume.

    A no-op when the current thread does not hold the lock (the offline
    ``pdf-mcp-warm`` CLI calls the same loops without it) or nobody waits.
    """
    if PDF_ACCESS.held_by_current_thread():
        stop = getattr(_background, "stop", None)
        if stop is None:
            PDF_ACCESS.yield_turn()
        else:
            PDF_ACCESS.yield_to_waiters(stop)


_background = threading.local()


@contextmanager
def background_access(stop: threading.Event) -> Iterator[None]:
    """Hold ``PDF_ACCESS`` for one piece of background work.

    Taken only when the lock is idle, and every ``yield_pdf_access`` inside
    gives it up until waiting calls are done. Raises BackgroundStopped once
    ``stop`` is set, at the start or at any yield.
    """
    if not PDF_ACCESS.acquire_when_idle(stop):
        raise BackgroundStopped
    _background.stop = stop
    try:
        yield
    finally:
        _background.stop = None
        if PDF_ACCESS.held_by_current_thread():
            PDF_ACCESS.release()
