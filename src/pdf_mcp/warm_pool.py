"""Extraction jobs that outlive the warm call that started them (issue #82).

A corpus warm extracts documents in spawn workers and must return by its
deadline: Claude Desktop cancels any tool call still running after 4
minutes. Before this module the warm waited for every in-flight
extraction, so one slow document (a long scan) held the call, and the
PDF lock, far past its budget. Abandoning the extraction instead would
leave a document slower than any budget impossible to warm: every retry
would start it over.

So a job is registered here, keyed by path and checked against the file's
identity (mtime and size), and survives the call that submitted it. A
call that reaches its deadline returns and leaves the job running. The
next warm call collects finished jobs (``take_finished``) and adopts a
job still running for a path it needs (``submit`` returns the existing
future), so repeated calls always make progress.

Workers never touch SQLite. A finished job's result waits in memory until
a later warm call writes it from the calling thread, under the PDF lock,
like every other cache write. The pool shuts down once no job is running
(``release_if_idle``); ``shutdown_now`` terminates leftover workers when
the server exits.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable


def _identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


@dataclass
class _Job:
    identity: tuple[int, int] | None
    want_sections: bool
    future: "Future[Any]"


class WarmPool:
    """One spawn pool plus the extraction jobs submitted to it."""

    def __init__(self, max_workers: Callable[[], int]) -> None:
        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._pool: ProcessPoolExecutor | None = None
        self._jobs: dict[str, _Job] = {}

    def submit(
        self, worker: Callable[..., Any], path: str, want_sections: bool
    ) -> "Future[Any]":
        """The job for ``path``: an existing one for the same file, or new.

        A job left by an earlier call is reused when the file is unchanged
        and the job extracts at least what this call needs (sections).
        """
        identity = _identity(path)
        with self._lock:
            job = self._jobs.get(path)
            if (
                job is not None
                and job.identity == identity
                and (job.want_sections or not want_sections)
            ):
                return job.future
            if self._pool is None:
                self._pool = ProcessPoolExecutor(
                    max_workers=max(1, self._max_workers()),
                    mp_context=multiprocessing.get_context("spawn"),
                )
            future = self._pool.submit(worker, path, want_sections)
            self._jobs[path] = _Job(identity, want_sections, future)
            return future

    def forget(self, path: str, future: "Future[Any]") -> None:
        """Drop the job once its result has been collected."""
        with self._lock:
            job = self._jobs.get(path)
            if job is not None and job.future is future:
                del self._jobs[path]

    def take_finished(self) -> list[tuple[str, "Future[Any]"]]:
        """Remove and return finished jobs whose file is unchanged.

        A finished job for a file that changed since it was submitted is
        dropped: its text belongs to bytes the cache would reject.
        """
        out: list[tuple[str, Future[Any]]] = []
        with self._lock:
            for path, job in list(self._jobs.items()):
                if not job.future.done():
                    continue
                del self._jobs[path]
                if job.identity == _identity(path):
                    out.append((path, job.future))
        return out

    def running(self) -> list[str]:
        with self._lock:
            return sorted(p for p, j in self._jobs.items() if not j.future.done())

    def release_if_idle(self) -> None:
        """Shut the pool down when no job is running, freeing its workers.

        Finished jobs keep their results: a Future does not need its pool.
        """
        with self._lock:
            if self._pool is None:
                return
            if any(not j.future.done() for j in self._jobs.values()):
                return
            pool, self._pool = self._pool, None
        pool.shutdown(wait=False)

    def reset(self) -> None:
        """Forget every job and drop the pool (after BrokenProcessPool)."""
        with self._lock:
            pool, self._pool = self._pool, None
            self._jobs.clear()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def shutdown_now(self) -> None:
        """Terminate running workers: the process is exiting, and an
        abandoned extraction must not hold up the server's shutdown."""
        with self._lock:
            pool, self._pool = self._pool, None
            self._jobs.clear()
        if pool is None:
            return
        processes = list(getattr(pool, "_processes", {}).values())
        pool.shutdown(wait=False, cancel_futures=True)
        for proc in processes:
            if proc.is_alive():
                proc.terminate()
