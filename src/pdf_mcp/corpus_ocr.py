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

from .concurrency import BackgroundStopped, background_access
from .extractor import _ocr_page_worker
from .parallel import resolve_workers, run_pages

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


def ocr_progress(path: str, cache: Any, ocr_lang: str) -> dict[str, int]:
    """Counts over the candidate pages: still pending, OCR'd with text,
    and OCR'd with no text (a failed or empty read, never retried)."""
    candidates = ocr_candidates(_coverage(path, cache))
    rows = cache.get_ocr_rows(path, candidates, ocr_lang)
    done = sum(1 for t in rows.values() if t.strip())
    return {
        "pending": len(candidates) - len(rows),
        "done": done,
        "no_text": len(rows) - done,
    }


def ocr_pending_pages(path: str, cache: Any, ocr_lang: str) -> list[int]:
    """Candidate pages with no OCR row for ``ocr_lang`` (empty or not).
    A document with no metadata row has not been warmed: nothing pending."""
    candidates = ocr_candidates(_coverage(path, cache))
    have = cache.get_ocr_rows(path, candidates, ocr_lang)
    return [pn for pn in candidates if pn not in have]


def effective_coverage(path: str, cache: Any) -> list[dict[str, int]]:
    """The cached coverage map, with OCR text (any language) counted as
    text, so a fully OCR'd scan reads as covered."""
    coverage = [dict(c) for c in _coverage(path, cache)]
    empty = [pn for pn, c in enumerate(coverage) if c.get("text_chars", 0) == 0]
    for pn, text in cache.get_ocr_rows(path, empty, None).items():
        if text.strip():
            coverage[pn]["text_chars"] = len(text.strip())
    return coverage


WARM_OCR_CAP = 4
OCR_BATCH_PAGES = 32
OCR_DPI = 300


class BackgroundOCR:
    """OCRs, between calls, the image-only pages a warm call found (#103).

    OCR is about 0.85 s a page, so a scanned corpus takes minutes to hours
    and cannot fit any call's budget. This thread OCRs pending pages in a
    process pool with no PDF lock held (each worker opens its own engine),
    then writes each batch from the server process under
    ``background_access``, which takes the lock only when no call needs it.

    Only ``pdf_cache_clear`` and server exit stop it. Warm calls never do:
    an agent looping on ``warm_complete`` would otherwise cancel OCR every
    call, and a batch finished after a stop is dropped, never written.
    ``PDF_MCP_BACKGROUND_OCR=0`` turns it off.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._lang: str | None = None
        self._pending = 0

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
        """OCR ``jobs`` ((path, 0-indexed pages) pairs) in the background,
        documents with the fewest pages first; returns the pages handed over
        (0 when off or nothing is pending)."""
        self.stop()
        jobs = [(p, list(pages)) for p, pages in jobs if pages]
        total = sum(len(pages) for _, pages in jobs)
        if not total or not self.enabled():
            return 0
        jobs.sort(key=lambda j: (len(j[1]), j[0]))
        # Looked up at call time, so a test can swap the module's worker.
        worker = worker or _ocr_page_worker
        stop = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(jobs, cache, ocr_lang, tessdata, worker, stop),
            name="pdf-mcp-background-ocr",
            daemon=True,
        )
        with self._lock:
            self._stop = stop
            self._thread = thread
            self._lang = ocr_lang
            self._pending = total
        thread.start()
        return total

    def running(self, ocr_lang: str) -> bool:
        """True while a thread is OCR'ing for this language."""
        with self._lock:
            return (
                self._thread is not None
                and self._thread.is_alive()
                and self._stop is not None
                and not self._stop.is_set()
                and self._lang == ocr_lang
            )

    def stop(self) -> None:
        """Ask the thread to stop. Never waits: the caller may hold the
        lock the thread needs to finish its current step."""
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            self._stop = None
            self._pending = 0

    def pending(self) -> int:
        with self._lock:
            return self._pending

    def join(self, timeout: float) -> bool:
        """Wait for the thread to end (tests, shutdown); True if it did."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _wrote(self, stop: threading.Event, n: int) -> None:
        with self._lock:
            if self._stop is stop:
                self._pending = max(0, self._pending - n)

    def _run(
        self,
        jobs: list[tuple[str, list[int]]],
        cache: Any,
        ocr_lang: str,
        tessdata: str | None,
        worker: Callable[[Any], Any],
        stop: threading.Event,
    ) -> None:
        from .corpus import forget_warm_verdict

        for path, pages in jobs:
            tried: set[int] = set()
            try:
                while not stop.is_set():
                    # Re-read the cache each batch: a pdf_read_pages(ocr=True)
                    # call may have OCR'd some of these pages meanwhile.
                    pending = set(ocr_pending_pages(path, cache, ocr_lang))
                    todo = [p for p in pages if p in pending and p not in tried]
                    if not todo:
                        break
                    batch = todo[:OCR_BATCH_PAGES]
                    tried.update(batch)
                    args = [
                        (path, pn, ocr_lang, OCR_DPI, tessdata, False) for pn in batch
                    ]
                    workers = resolve_workers(len(batch), 1, cap=WARM_OCR_CAP)
                    results = run_pages(worker, args, workers, page_timeout=600)
                    if stop.is_set():
                        return
                    with background_access(stop):
                        for res in results:
                            pn, text = res if isinstance(res, tuple) else (None, None)
                            if pn is None:
                                continue
                            if not isinstance(text, str) or not text.strip():
                                text = ""
                            cache.save_page_text(
                                path, pn, text, source="ocr", ocr_lang=ocr_lang
                            )
                        forget_warm_verdict(path)
                    self._wrote(stop, len(batch))
            except BackgroundStopped:
                return
            except Exception as exc:  # noqa: BLE001 - one doc; a call retries it
                logger.warning("background OCR of %s failed: %s", path, exc)


BACKGROUND_OCR = BackgroundOCR()
