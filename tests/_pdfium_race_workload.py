"""Subprocess workload: parallel tool calls on generated PDFs, then a probe.

Run as ``python -m tests._pdfium_race_workload [--no-lock]`` from the repo
root with PDF_MCP_CACHE_DIR pointing at an empty directory. Exit 0 when
every call succeeded and a PDF never touched before still opens; 1
otherwise. Without the lock this process usually dies (SIGSEGV/SIGABRT),
which is why the tests run it in a subprocess. ``--no-lock`` exists only
to prove the test bites; it swaps the lock for a no-op in this process.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading

import pymupdf


def make_pdf(path: str, n_pages: int = 6) -> None:
    doc = pymupdf.open()
    for p in range(n_pages):
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(50, 50, 300, 150), color=(0, 0, 0))
        for x in (130, 190, 245):
            page.draw_line(pymupdf.Point(x, 50), pymupdf.Point(x, 150), color=(0, 0, 0))
        for y in (83, 116):
            page.draw_line(pymupdf.Point(50, y), pymupdf.Point(300, y), color=(0, 0, 0))
        rows = [
            "Parameter Min Max Unit",
            f"Supply Voltage 4.5 {16 + p} V",
            "Reset Voltage 0.4 1.0 V",
        ]
        for i, line in enumerate(rows):
            page.insert_text((55, 75 + 33 * i), line)
        for i in range(30):
            page.insert_text(
                (50, 200 + 18 * i),
                f"Page {p} line {i}: the supply voltage range is specified here.",
            )
    doc.save(path)
    doc.close()


class _NoLock(contextlib.nullcontext):  # type: ignore[type-arg]
    def held_by_current_thread(self) -> bool:
        return False


def main() -> int:
    if "--no-lock" in sys.argv:
        from pdf_mcp import concurrency

        concurrency.PDF_ACCESS = _NoLock()  # type: ignore[assignment]

    from pdf_mcp.tools.info import pdf_info
    from pdf_mcp.tools.render import pdf_render_pages
    from pdf_mcp.tools.search import pdf_search

    tmp = tempfile.mkdtemp()
    pdfs = [os.path.join(tmp, f"doc{i}.pdf") for i in range(3)]
    probe = os.path.join(tmp, "probe.pdf")
    for path in pdfs + [probe]:
        make_pdf(path)
    errors: list[str] = []

    def reader(path: str) -> None:
        try:
            for i in range(15):
                pdf_info(path)
                pdf_render_pages(path, pages=str(1 + i % 3), dpi=72)
                pdf_search(path, "supply voltage", max_results=3)
        except Exception as exc:  # noqa: BLE001 - the test reports it
            errors.append(repr(exc)[:200])

    threads = [threading.Thread(target=reader, args=(p,)) for p in pdfs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        pdf_info(probe)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"probe: {exc!r}"[:200])
    print("errors:", errors)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
