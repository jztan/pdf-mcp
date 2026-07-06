"""Process-pool helper for per-page parallelism (OCR, render).

Stdlib-only on purpose: workers in extractor.py import PageError from here, and
keeping this module free of PyMuPDF/project imports keeps the spawn re-import
path cheap.
"""

import os
from concurrent.futures import ProcessPoolExecutor, as_completed, TimeoutError
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable


class PageError:
    """Marker a worker returns when processing one page raised.

    Carries the repr of the exception so the parent can surface/log it. Picklable
    (plain attribute) so it survives the process boundary.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail

    def __repr__(self) -> str:
        return f"PageError({self.detail!r})"


def resolve_workers(n_pages: int, gate: int, cap: int = 8) -> int:
    """Worker count, or 1 to signal 'run sequentially'.

    - n_pages < gate            -> 1 (sequential; no pool, no spawn cost)
    - else min(os.cpu_count() or 1, n_pages, cap)
    - PDF_MCP_MAX_WORKERS env clamps: a value <= 1 (including negative) forces
      sequential; a positive int caps the pool (cannot raise above the computed
      value); invalid/absent env is ignored.

    No cgroup/affinity detection: single-user STDIO deployment, cap bounds
    oversubscription, env var is the constrained-host escape hatch.
    """
    if n_pages < gate:
        return 1

    workers = min(os.cpu_count() or 1, n_pages, cap)

    env = os.environ.get("PDF_MCP_MAX_WORKERS")
    if env is not None:
        try:
            env_cap = int(env)
        except ValueError:
            env_cap = None
        if env_cap is not None:
            if env_cap <= 1:
                return 1
            workers = min(workers, env_cap)

    return workers


def run_pages(
    worker: Callable[[Any], Any],
    arg_list: list[Any],
    max_workers: int,
    timeout: float = 300,
) -> list[Any]:
    """Map `worker` over `arg_list`, preserving order.

    max_workers <= 1 -> sequential list comprehension (no pool, no spawn cost).
    Else a fresh per-call ProcessPoolExecutor with per-future timeout.
    On BrokenProcessPool or TimeoutError (worker segfault, hang, OOM-kill),
    keep results from already-completed futures and re-run only the
    incomplete indices sequentially in-parent so the call still completes
    without waiting on orphaned/hung workers.
    """
    if max_workers <= 1:
        return [worker(a) for a in arg_list]

    pool: ProcessPoolExecutor | None = None
    results: list[Any] = [None] * len(arg_list)
    completed: set[int] = set()

    try:
        pool = ProcessPoolExecutor(max_workers=max_workers)
        try:
            futures = [pool.submit(worker, a) for a in arg_list]
            for f in as_completed(futures, timeout=timeout):
                idx = futures.index(f)
                results[idx] = f.result()
                completed.add(idx)
        except (BrokenProcessPool, TimeoutError):
            pass
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    for idx in range(len(arg_list)):
        if idx not in completed:
            results[idx] = worker(arg_list[idx])

    return results
