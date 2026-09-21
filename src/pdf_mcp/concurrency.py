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

Stdlib only: imported by ``corpus`` (pure logic) and by every tool module.
"""

from __future__ import annotations

import functools
import threading
from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class FairLock:
    """A re-entrant ticket lock: waiters are served in arrival order."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._next_ticket = 0
        self._serving = 0
        self._owner: int | None = None
        self._depth = 0

    def _wait_for(self, ticket: int) -> None:
        while not (self._owner is None and self._serving == ticket):
            self._cond.wait()

    def acquire(self) -> None:
        me = threading.get_ident()
        with self._cond:
            if self._owner == me:
                self._depth += 1
                return
            ticket = self._next_ticket
            self._next_ticket += 1
            self._wait_for(ticket)
            self._owner = me
            self._depth = 1

    def release(self) -> None:
        with self._cond:
            if self._owner != threading.get_ident():
                raise RuntimeError(
                    "FairLock released by a thread that does not hold it"
                )
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._serving += 1
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
            queued = self._next_ticket - self._serving
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
            if self._next_ticket - self._serving - 1 <= 0:
                return
            depth = self._depth
            self._owner = None
            self._depth = 0
            self._serving += 1
            ticket = self._next_ticket
            self._next_ticket += 1
            self._cond.notify_all()
            self._wait_for(ticket)
            self._owner = me
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


def yield_pdf_access() -> None:
    """Between documents in a long loop: let waiting calls in, then resume.

    A no-op when the current thread does not hold the lock (the offline
    ``pdf-mcp-warm`` CLI calls the same loops without it) or nobody waits.
    """
    if PDF_ACCESS.held_by_current_thread():
        PDF_ACCESS.yield_turn()
