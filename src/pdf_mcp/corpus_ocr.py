"""OCR on corpus warm (issue #103).

Pure logic, no MCP dependency. Decides from cached state which pages of a
warmed document need OCR, and how far OCR has got. A page needs OCR when
extraction found no text on it but it carries a raster image; a page with
any text, or a blank page, is never OCR'd.

Completion is always computed from the cache, never from a thread, so a
server restart cannot make a corpus look finished.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

from concurrent.futures import CancelledError, ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

from .cache import normalize_ocr_lang
from .concurrency import BackgroundStopped, background_access
from .extractor import _ocr_page_worker
from .parallel import PageError, resolve_workers

logger = logging.getLogger(__name__)


def ocr_candidates(coverage: list[dict[str, int]]) -> list[int]:
    """0-indexed pages with no extracted text and at least one raster image."""
    return [
        pn
        for pn, c in enumerate(coverage)
        if c.get("text_chars", 0) == 0 and c.get("raster_images", 0) > 0
    ]


def _coverage(path: str, cache: Any) -> list[dict[str, int]]:
    meta = cache.get_metadata(path) or {}
    return list(meta.get("text_coverage") or [])


def ocr_status(
    path: str, cache: Any, ocr_lang: str
) -> tuple[dict[str, int], list[int]]:
    """Progress counts over the candidate pages (still pending, OCR'd with
    text, OCR'd with no text: a failed or empty read, never retried) and the
    pending pages, from one cache read."""
    candidates = ocr_candidates(_coverage(path, cache))
    lengths = cache.get_ocr_text_lengths(path, candidates, ocr_lang)
    done = sum(1 for n in lengths.values() if n)
    progress = {
        "pending": len(candidates) - len(lengths),
        "done": done,
        "no_text": len(lengths) - done,
    }
    return progress, [pn for pn in candidates if pn not in lengths]


def ocr_progress(path: str, cache: Any, ocr_lang: str) -> dict[str, int]:
    """Progress counts only; see ``ocr_status``."""
    return ocr_status(path, cache, ocr_lang)[0]


def ocr_pending_pages(path: str, cache: Any, ocr_lang: str) -> list[int]:
    """Candidate pages with no OCR row for ``ocr_lang`` (empty or not).
    A document with no metadata row has not been warmed: nothing pending."""
    return ocr_status(path, cache, ocr_lang)[1]


def effective_coverage(path: str, cache: Any) -> list[dict[str, int]]:
    """The cached coverage map, with OCR text (any language) counted as
    text, so a fully OCR'd scan reads as covered."""
    coverage = [dict(c) for c in _coverage(path, cache)]
    empty = [pn for pn, c in enumerate(coverage) if c.get("text_chars", 0) == 0]
    for pn, n in cache.get_ocr_text_lengths(path, empty, None).items():
        if n:
            coverage[pn]["text_chars"] = n
    return coverage


WARM_OCR_CAP = 4
OCR_BATCH_PAGES = 32
OCR_DPI = 300
OCR_PAGE_TIMEOUT = 600.0


def ocr_worker_count() -> int:
    """OCR processes for the background thread. Sized from the batch cap,
    never from one batch's page count, so a one-page batch still goes to a
    child process; 1 only when ``PDF_MCP_MAX_WORKERS`` or the CPU count
    forces it, and then OCR runs in the server process under the PDF lock."""
    return resolve_workers(OCR_BATCH_PAGES, 1, cap=WARM_OCR_CAP)


def _text_of(result: Any) -> str:
    """A worker returns (page, text | PageError); a fallback may return a
    bare PageError. Anything but non-blank text reads as no text."""
    text = result[1] if isinstance(result, tuple) else result
    return text if isinstance(text, str) and text.strip() else ""


class BackgroundOCR:
    """OCRs, between calls, the image-only pages a warm call found (#103).

    OCR is about 0.85 s a page, so a scanned corpus takes minutes to hours
    and cannot fit any call's budget. With more than one worker the thread
    OCRs in a long-lived process pool with no PDF lock held (each child
    opens its own engine); with one worker it OCRs in the server process,
    one page at a time under ``background_access``, because pdfium is not
    thread-safe. Every write happens in the server process under
    ``background_access``, re-checking for a stop before each page.

    Jobs for the running language join its queue, so a second corpus or a
    newly added doc is OCR'd without restarting anything. Only
    ``pdf_cache_clear``, server exit, and a request in another language
    stop it; a page read after a stop is never written.
    ``PDF_MCP_BACKGROUND_OCR=0`` turns it off.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._active = False
        self._lang: str | None = None
        self._queue: list[tuple[str, list[int]]] = []
        self._queued: set[tuple[str, int]] = set()
        self._pool: Any = None

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("PDF_MCP_BACKGROUND_OCR", "1").strip() != "0"

    def start(
        self,
        jobs: list[tuple[str, list[int]]],
        cache: Any,
        ocr_lang: str,
        tessdata: str | None,
        worker: Callable[[Any], Any] | None = None,
    ) -> int:
        """Queue ``jobs`` ((path, 0-indexed pages) pairs) for OCR, documents
        with the fewest pages first. Joins a running thread for the same
        language; otherwise starts one. Returns the pages newly queued (0
        when off or nothing new)."""
        if not self.enabled():
            return 0
        lang = normalize_ocr_lang(ocr_lang)
        with self._lock:
            if self._active and self._lang == lang:
                return self._enqueue(jobs)
        self.stop()
        stop = threading.Event()
        with self._lock:
            self._queue, self._queued = [], set()
            added = self._enqueue(jobs)
            if not added:
                return 0
            self._stop = stop
            self._lang = lang
            self._active = True
            # Looked up at call time, so a test can swap the module's worker.
            thread = threading.Thread(
                target=self._run,
                args=(cache, ocr_lang, tessdata, worker or _ocr_page_worker, stop),
                name="pdf-mcp-background-ocr",
                daemon=True,
            )
            self._thread = thread
        thread.start()
        return added

    def _enqueue(self, jobs: list[tuple[str, list[int]]]) -> int:
        """Add pages not already queued; caller holds ``_lock``."""
        added = 0
        for path, pages in jobs:
            new = [pn for pn in pages if (path, pn) not in self._queued]
            if not new:
                continue
            self._queued.update((path, pn) for pn in new)
            self._queue.append((path, new))
            added += len(new)
        self._queue.sort(key=lambda j: (len(j[1]), j[0]))
        return added

    def _next_job(self, stop: threading.Event) -> tuple[str, list[int]] | None:
        with self._lock:
            if stop.is_set() or self._stop is not stop:
                return None
            if not self._queue:
                # Under the lock, so a start() that sees _active joins a
                # thread that is still going to read its queue.
                self._active = False
                return None
            return self._queue.pop(0)

    def running(self, ocr_lang: str) -> bool:
        """True while a thread is OCR'ing for this language."""
        with self._lock:
            return self._active and self._lang == normalize_ocr_lang(ocr_lang)

    def stop(self) -> None:
        """Ask the thread to stop and cancel its pool. Never waits: the
        caller may hold the lock the thread needs to finish its step, and
        server exit must not wait for a batch of Tesseract runs."""
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            self._stop = None
            self._active = False
            self._queue, self._queued = [], set()
            pool, self._pool = self._pool, None
        _kill_pool(pool)

    def pending(self) -> int:
        with self._lock:
            return sum(len(pages) for _, pages in self._queue)

    def join(self, timeout: float) -> bool:
        """Wait for the thread to end (tests, shutdown); True if it did."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(
        self,
        cache: Any,
        ocr_lang: str,
        tessdata: str | None,
        worker: Callable[[Any], Any],
        stop: threading.Event,
    ) -> None:
        from .corpus import forget_warm_verdict

        workers = ocr_worker_count()
        try:
            while (job := self._next_job(stop)) is not None:
                path, pages = job
                try:
                    self._ocr_doc(
                        path, pages, cache, ocr_lang, tessdata, worker, workers, stop
                    )
                except BackgroundStopped:
                    return
                except Exception as exc:  # noqa: BLE001 - one doc; a call retries
                    logger.warning("background OCR of %s failed: %s", path, exc)
                forget_warm_verdict(path)
        finally:
            with self._lock:
                if self._stop is stop:
                    self._active = False
                    pool, self._pool = self._pool, None
                else:
                    pool = None
            _kill_pool(pool)

    def _ocr_doc(
        self,
        path: str,
        pages: list[int],
        cache: Any,
        ocr_lang: str,
        tessdata: str | None,
        worker: Callable[[Any], Any],
        workers: int,
        stop: threading.Event,
    ) -> None:
        tried: set[int] = set()
        while not stop.is_set():
            # Re-read the cache each batch: a pdf_read_pages(ocr=True) call
            # may have OCR'd some of these pages meanwhile.
            pending = set(ocr_pending_pages(path, cache, ocr_lang))
            todo = [p for p in pages if p in pending and p not in tried]
            if not todo:
                return
            batch = todo[:OCR_BATCH_PAGES]
            tried.update(batch)

            def args(pn: int) -> tuple[str, int, str, int, str | None, bool]:
                return (path, pn, ocr_lang, OCR_DPI, tessdata, False)

            def save(pn: int, result: Any) -> None:
                cache.save_page_text(
                    path, pn, _text_of(result), source="ocr", ocr_lang=ocr_lang
                )

            if workers <= 1:
                # In the server process: pdfium must run under the PDF lock.
                for pn in batch:
                    with background_access(stop):
                        result = worker(args(pn))
                        if stop.is_set():
                            return
                        save(pn, result)
                continue
            results = self._pool_map(worker, [args(pn) for pn in batch], workers, stop)
            if stop.is_set():
                return
            with background_access(stop):
                for pn, result in zip(batch, results):
                    if stop.is_set():
                        return
                    save(pn, result)

    def _pool_map(
        self,
        worker: Callable[[Any], Any],
        arg_list: list[Any],
        workers: int,
        stop: threading.Event,
    ) -> list[Any]:
        """Run a batch in the thread's long-lived pool. A page that raises,
        times out or takes the pool down comes back as a PageError, so it is
        recorded once instead of retried forever; a broken or timed-out pool
        is killed and the next batch starts a fresh one."""
        with self._lock:
            if self._stop is not stop:
                return [PageError("stopped")] * len(arg_list)
            if self._pool is None:
                self._pool = ProcessPoolExecutor(max_workers=workers)
            pool = self._pool
        results: list[Any] = [PageError("not run")] * len(arg_list)
        futures = {pool.submit(worker, a): i for i, a in enumerate(arg_list)}
        waves = -(-len(arg_list) // workers)
        try:
            for fut in as_completed(futures, timeout=OCR_PAGE_TIMEOUT * waves):
                try:
                    results[futures[fut]] = fut.result()
                except Exception as exc:  # noqa: BLE001 - per-page isolation
                    results[futures[fut]] = PageError(repr(exc))
        except (TimeoutError, BrokenProcessPool, CancelledError):
            with self._lock:
                if self._pool is pool:
                    self._pool = None
            _kill_pool(pool)
        return results


def _kill_pool(pool: Any) -> None:
    """Cancel queued work and end the pool's processes without waiting."""
    if pool is None:
        return
    found = getattr(pool, "_processes", None)
    procs = list(found.values()) if isinstance(found, dict) else []
    pool.shutdown(wait=False, cancel_futures=True)
    for proc in procs:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 - already gone
            pass


BACKGROUND_OCR = BackgroundOCR()
