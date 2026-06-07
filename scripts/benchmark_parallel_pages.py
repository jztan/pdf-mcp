#!/usr/bin/env python
"""
scripts/benchmark_parallel_pages.py

Parallel-page-processing benchmark for pdf-mcp's CPU-bound page operations.

pdf-mcp processes pages sequentially (one shared `pymupdf.Document`, a `for`
loop over page numbers) in `pdf_render_pages`, `pdf_read_pages`, and
`pdf_read_all`. A sibling project (SylphxAI/pdf-reader-mcp, PDF.js) claims
"5-10x faster with parallel processing" — but that speedup comes from PDF.js's
async API overlapping on a single event loop, which does not transfer to our
synchronous PyMuPDF stack. Whether threading helps us depends entirely on
whether PyMuPDF releases the GIL during the C-level work, so this is an
empirical question. This script answers it with real numbers before any
implementation is committed.

It measures the two operations where the GIL is most likely released (raster
rendering and OCR) sequentially vs. threaded across worker counts, so we can
decide if a parallel port is worth it — and at what worker count it plateaus.

Thread-safety note: a PyMuPDF `Document` is NOT safe to share across threads,
so every threaded task opens its OWN `pymupdf.open(path)` handle (the cost of
that per-task open is included in the threaded timings — it is part of the real
port). The sequential baseline mirrors today's code: one shared handle, looped.

Usage:
    python scripts/benchmark_parallel_pages.py                      # full run
    python scripts/benchmark_parallel_pages.py --pages 48           # bigger doc
    python scripts/benchmark_parallel_pages.py --workers 1,2,4      # worker set
    python scripts/benchmark_parallel_pages.py --runs 5             # more repeats
    python scripts/benchmark_parallel_pages.py --dpi 300            # heavier render
    python scripts/benchmark_parallel_pages.py --no-ocr             # skip OCR
    python scripts/benchmark_parallel_pages.py --corpus             # real arXiv PDFs
    python scripts/benchmark_parallel_pages.py --output FILE        # write md table

The --corpus mode benchmarks non-OCR text/render against the same real arXiv
documents as benchmark_reading_order.py (fetched on demand, shared cache under
benchmark_data/.reading_order_pdfs/), instead of a synthetic PDF. It needs
network access on first run; it falls back to the synthetic corpus otherwise.

The OCR section needs a working Tesseract install; it is skipped automatically
(with a note) when Tesseract is unavailable, so the render benchmark always runs.
"""

from __future__ import annotations

import argparse
import os
import json
import multiprocessing
import statistics
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import pymupdf

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Reuse the reading-order benchmark's real arXiv corpus and on-disk cache so
# both benchmarks exercise the same documents (see benchmark_reading_order.py).
CORPUS = Path(__file__).parent.parent / "benchmark_data" / "reading_order_corpus.json"
PDF_CACHE = Path(__file__).parent.parent / "benchmark_data" / ".reading_order_pdfs"

from pdf_mcp.extractor import (  # noqa: E402
    check_tesseract_available,
    extract_text_from_page,
    ocr_page,
    render_page_as_png,
)


def _fetch_arxiv_pdf(arxiv_id: str) -> Path | None:
    """Fetch one arXiv PDF into the shared cache (same as reading-order bench)."""
    PDF_CACHE.mkdir(parents=True, exist_ok=True)
    pdf = PDF_CACHE / f"{arxiv_id}.pdf"
    if pdf.exists():
        return pdf
    try:
        req = urllib.request.Request(
            f"https://arxiv.org/pdf/{arxiv_id}",
            headers={"User-Agent": "Mozilla/5.0 (pdf-mcp parallel benchmark)"},
        )
        pdf.write_bytes(urllib.request.urlopen(req, timeout=30).read())
        time.sleep(1.2)  # be polite to arxiv.org
        return pdf
    except Exception:
        return None


def build_corpus_pdf(path: Path, limit: int | None) -> tuple[int, int]:
    """Merge real arXiv corpus PDFs into one document for a realistic non-OCR
    text/render benchmark. Returns (total_pages, documents_used).

    Raises RuntimeError if no corpus PDF can be fetched (e.g. no network) so the
    caller can fall back to the synthetic corpus with a clear message.
    """
    data = json.loads(CORPUS.read_text())
    ids = [*data.get("two_column", []), *data.get("one_column", [])]
    if limit:
        ids = ids[:limit]
    merged = pymupdf.open()
    used = 0
    for arxiv_id in ids:
        src_path = _fetch_arxiv_pdf(arxiv_id)
        if src_path is None:
            continue
        try:
            src = pymupdf.open(str(src_path))
            merged.insert_pdf(src)
            src.close()
            used += 1
        except Exception:
            continue
    if used == 0:
        merged.close()
        raise RuntimeError(
            "No corpus PDFs available — arxiv.org unreachable and cache empty. "
            "Run on a machine with network access (PDFs cache under "
            "benchmark_data/.reading_order_pdfs/)."
        )
    total = len(merged)
    merged.save(str(path))
    merged.close()
    return total, used


def build_text_pdf(path: Path, pages: int) -> None:
    """A text- and vector-heavy PDF so rendering does measurable work per page."""
    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page(width=595, height=842)  # A4
        for line in range(55):
            y = 40 + line * 14
            page.insert_text(
                (40, y),
                f"Page {n + 1} line {line + 1}: the quick brown fox jumps over "
                f"the lazy dog 0123456789 — rendering workload filler text.",
                fontsize=9,
            )
        # Vector graphics add rasterization cost beyond plain glyphs.
        shape = page.new_shape()
        for i in range(12):
            shape.draw_rect(pymupdf.Rect(300 + i, 60 + i * 10, 540 - i, 110 + i * 10))
        shape.finish(color=(0, 0, 0.6), width=0.4)
        shape.commit()
    doc.save(str(path))
    doc.close()


def build_scanned_pdf(src_text_pdf: Path, path: Path, pages: int, dpi: int) -> None:
    """Image-only PDF (text rasterized to pixmaps) so OCR has real work to do."""
    src = pymupdf.open(str(src_text_pdf))
    doc = pymupdf.open()
    for n in range(pages):
        pix = src[n % len(src)].get_pixmap(dpi=dpi)
        page = doc.new_page(width=pix.width, height=pix.height)
        page.insert_image(pymupdf.Rect(0, 0, pix.width, pix.height), pixmap=pix)
    src.close()
    doc.save(str(path))
    doc.close()


def time_sequential(
    path: str, page_nums: list[int], task: Callable[[pymupdf.Document, int], Any]
) -> float:
    """Today's pattern: one shared Document, looped. Returns wall seconds."""
    start = time.perf_counter()
    doc = pymupdf.open(path)
    try:
        for n in page_nums:
            task(doc, n)
    finally:
        doc.close()
    return time.perf_counter() - start


def time_threaded(
    path: str,
    page_nums: list[int],
    task: Callable[[pymupdf.Document, int], Any],
    workers: int,
) -> float:
    """Proposed pattern: one Document per task (thread-safe), bounded pool."""

    def run_one(n: int) -> Any:
        doc = pymupdf.open(path)  # per-task handle — see thread-safety note
        try:
            return task(doc, n)
        finally:
            doc.close()

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(run_one, page_nums))
    return time.perf_counter() - start


_PROC_OUT: Path | None = None  # set in child via initializer


def _proc_init(out_dir: str) -> None:
    global _PROC_OUT
    _PROC_OUT = Path(out_dir)


def _proc_render(args: tuple[str, int, int]) -> int:
    """Module-level (picklable) render worker for ProcessPoolExecutor."""
    path, page_num, dpi = args
    doc = pymupdf.open(path)
    try:
        assert _PROC_OUT is not None
        info = render_page_as_png(doc, page_num, _PROC_OUT, "bench", dpi=dpi)
        return int(info["size_bytes"])
    finally:
        doc.close()


def time_process(
    path: str, page_nums: list[int], dpi: int, workers: int, out_dir: Path
) -> float:
    """True multi-core: one process + Document per task. Returns wall seconds."""
    start = time.perf_counter()
    with ProcessPoolExecutor(
        max_workers=workers, initializer=_proc_init, initargs=(str(out_dir),)
    ) as pool:
        list(pool.map(_proc_render, [(path, n, dpi) for n in page_nums]))
    return time.perf_counter() - start


def _proc_ocr(args: tuple[str, int]) -> int:
    """Module-level (picklable) OCR worker for ProcessPoolExecutor."""
    path, page_num = args
    doc = pymupdf.open(path)
    try:
        return len(ocr_page(doc, page_num, lang="eng", dpi=300))
    finally:
        doc.close()


def time_process_ocr(path: str, page_nums: list[int], workers: int) -> float:
    """True multi-core OCR: one process + Document per task. Wall seconds."""
    start = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_proc_ocr, [(path, n) for n in page_nums]))
    return time.perf_counter() - start


def _proc_text(args: tuple[str, int]) -> int:
    """Module-level (picklable) text-extraction worker. Returns the extracted
    text length; the text itself crosses the process boundary (pickled), which
    is part of the real cost of parallelizing this cheap-per-page operation."""
    path, page_num = args
    doc = pymupdf.open(path)
    try:
        return len(extract_text_from_page(doc[page_num]))
    finally:
        doc.close()


def time_process_text(path: str, page_nums: list[int], workers: int) -> float:
    """Multi-core plain-text extraction. Returns wall seconds."""
    start = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_proc_text, [(path, n) for n in page_nums]))
    return time.perf_counter() - start


def _split_chunks(items: list[int], n: int) -> list[list[int]]:
    """Split into n contiguous, near-equal chunks (drop empties)."""
    k, m = divmod(len(items), n)
    out, i = [], 0
    for j in range(n):
        size = k + (1 if j < m else 0)
        if size:
            out.append(items[i : i + size])
            i += size
    return out


def _proc_render_chunk(args: tuple[str, list[int], int]) -> int:
    """Open the Document ONCE, render a whole page range — amortizes the
    (expensive, for real PDFs) per-task open across many pages."""
    path, pages, dpi = args
    doc = pymupdf.open(path)
    try:
        assert _PROC_OUT is not None
        return sum(
            int(render_page_as_png(doc, n, _PROC_OUT, "bench", dpi=dpi)["size_bytes"])
            for n in pages
        )
    finally:
        doc.close()


def time_process_render_chunked(
    path: str, page_nums: list[int], dpi: int, workers: int, out_dir: Path
) -> float:
    """Chunked render: one open per worker, not per page. Returns wall seconds."""
    chunks = _split_chunks(page_nums, workers)
    start = time.perf_counter()
    with ProcessPoolExecutor(
        max_workers=workers, initializer=_proc_init, initargs=(str(out_dir),)
    ) as pool:
        list(pool.map(_proc_render_chunk, [(path, c, dpi) for c in chunks]))
    return time.perf_counter() - start


def _proc_text_chunk(args: tuple[str, list[int]]) -> int:
    """Open the Document ONCE, extract text for a whole page range."""
    path, pages = args
    doc = pymupdf.open(path)
    try:
        return sum(len(extract_text_from_page(doc[n])) for n in pages)
    finally:
        doc.close()


def time_process_text_chunked(path: str, page_nums: list[int], workers: int) -> float:
    """Chunked text extraction: one open per worker. Returns wall seconds."""
    chunks = _split_chunks(page_nums, workers)
    start = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_proc_text_chunk, [(path, c) for c in chunks]))
    return time.perf_counter() - start


def time_read_pages_render(path: str, n_pages: int, max_workers: int) -> float:
    """End-to-end pdf_read_pages(render_dpi) wall time, incl. the serial
    text/images/tables work that stays in the parent. This is the path that
    actually ships render parallelism -- not render-in-isolation."""
    import time

    from pdf_mcp.server import cache, pdf_read_pages

    os.environ["PDF_MCP_MAX_WORKERS"] = str(max_workers)
    # NOTE: clears the module-level cache. PDF_MCP_CACHE_DIR must be set to a
    # temp dir before this script imports anything from pdf_mcp.server (done in
    # main() below) so the real user cache is never touched.
    cache.clear_all()
    pages = ",".join(str(i + 1) for i in range(n_pages))
    start = time.perf_counter()
    pdf_read_pages(path, pages, render_dpi=200)
    return time.perf_counter() - start


def best_of(fn: Callable[[], float], runs: int) -> tuple[float, float]:
    """Run fn `runs` times; return (min, median) seconds — min = least noisy."""
    samples = [fn() for _ in range(runs)]
    return min(samples), statistics.median(samples)


def text_benchmark(
    pdf: str, page_nums: list[int], workers: list[int], runs: int
) -> list[dict[str, Any]]:
    """Plain-text extraction — the common non-OCR path (pdf_read_pages /
    pdf_read_all). Per-page work is tiny (~ms), so this stresses whether
    parallel overhead is worth it at all for cheap operations."""

    def task(doc: pymupdf.Document, n: int) -> Any:
        return extract_text_from_page(doc[n])

    seq_min, seq_med = best_of(lambda: time_sequential(pdf, page_nums, task), runs)
    rows = [_row("sequential", seq_min, seq_med, len(page_nums), seq_min)]
    for w in workers:
        t_min, t_med = best_of(lambda w=w: time_threaded(pdf, page_nums, task, w), runs)
        rows.append(_row(f"threaded x{w}", t_min, t_med, len(page_nums), seq_min))
    for w in workers:
        if w == 1:
            continue
        t_min, t_med = best_of(lambda w=w: time_process_text(pdf, page_nums, w), runs)
        rows.append(_row(f"process x{w}", t_min, t_med, len(page_nums), seq_min))
    for w in workers:
        if w == 1:
            continue
        t_min, t_med = best_of(
            lambda w=w: time_process_text_chunked(pdf, page_nums, w), runs
        )
        rows.append(_row(f"process-chunk x{w}", t_min, t_med, len(page_nums), seq_min))
    return rows


def render_benchmark(
    pdf: str, page_nums: list[int], dpi: int, workers: list[int], runs: int
) -> list[dict[str, Any]]:
    out = Path(tempfile.mkdtemp(prefix="bench_render_"))

    def task(doc: pymupdf.Document, n: int) -> Any:
        return render_page_as_png(doc, n, out, "bench", dpi=dpi)

    seq_min, seq_med = best_of(lambda: time_sequential(pdf, page_nums, task), runs)
    rows = [_row("sequential", seq_min, seq_med, len(page_nums), seq_min)]
    for w in workers:
        t_min, t_med = best_of(lambda w=w: time_threaded(pdf, page_nums, task, w), runs)
        rows.append(_row(f"threaded x{w}", t_min, t_med, len(page_nums), seq_min))
    for w in workers:
        if w == 1:
            continue  # process x1 == sequential + spawn overhead; uninformative
        t_min, t_med = best_of(
            lambda w=w: time_process(pdf, page_nums, dpi, w, out), runs
        )
        rows.append(_row(f"process x{w}", t_min, t_med, len(page_nums), seq_min))
    for w in workers:
        if w == 1:
            continue
        t_min, t_med = best_of(
            lambda w=w: time_process_render_chunked(pdf, page_nums, dpi, w, out),
            runs,
        )
        rows.append(_row(f"process-chunk x{w}", t_min, t_med, len(page_nums), seq_min))
    return rows


def ocr_benchmark(
    pdf: str, page_nums: list[int], workers: list[int], runs: int
) -> list[dict[str, Any]]:
    def task(doc: pymupdf.Document, n: int) -> Any:
        return ocr_page(doc, n, lang="eng", dpi=300)

    seq_min, seq_med = best_of(lambda: time_sequential(pdf, page_nums, task), runs)
    rows = [_row("sequential", seq_min, seq_med, len(page_nums), seq_min)]
    # No threaded mode for OCR: PyMuPDF's OCR goes through Leptonica, whose
    # global state is not thread-safe even across separate Document handles
    # ("Attempt to use Leptonica from 2 threads at once!"). Threads crash, so
    # the process pool is the only viable parallel path for OCR.
    for w in workers:
        if w == 1:
            continue  # process x1 == sequential + spawn overhead; uninformative
        t_min, t_med = best_of(lambda w=w: time_process_ocr(pdf, page_nums, w), runs)
        rows.append(_row(f"process x{w}", t_min, t_med, len(page_nums), seq_min))
    return rows


def _row(
    label: str, t_min: float, t_med: float, pages: int, baseline: float
) -> dict[str, Any]:
    return {
        "mode": label,
        "min_s": t_min,
        "median_s": t_med,
        "pages_per_s": pages / t_min if t_min else 0.0,
        "speedup": baseline / t_min if t_min else 0.0,
    }


def render_table(title: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"### {title}",
        "",
        "| mode | min (s) | median (s) | pages/s | speedup |",
        "|------|--------:|-----------:|--------:|--------:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['mode']} | {r['min_s']:.3f} | {r['median_s']:.3f} "
            f"| {r['pages_per_s']:.1f} | {r['speedup']:.2f}x |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    # Set PDF_MCP_CACHE_DIR to a temp directory BEFORE any pdf_mcp.server import
    # so the benchmark never touches the developer's real SQLite cache. The lazy
    # import inside time_read_pages_render() picks this up at call time.
    _bench_cache_dir = tempfile.mkdtemp(prefix="bench_pdf_mcp_cache_")
    os.environ["PDF_MCP_CACHE_DIR"] = _bench_cache_dir

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=24, help="pages to process")
    parser.add_argument(
        "--workers", default="1,2,4,8", help="comma-separated worker counts"
    )
    parser.add_argument("--runs", type=int, default=3, help="repeats per config")
    parser.add_argument("--dpi", type=int, default=200, help="render DPI")
    parser.add_argument("--no-ocr", action="store_true", help="skip OCR benchmark")
    parser.add_argument(
        "--corpus",
        action="store_true",
        help="benchmark non-OCR text/render on the real arXiv corpus "
        "(reading_order_corpus.json) instead of a synthetic PDF; OCR is "
        "skipped since those documents already have a text layer",
    )
    parser.add_argument(
        "--corpus-limit", type=int, default=6, help="max corpus PDFs to merge"
    )
    parser.add_argument("--output", type=Path, help="write markdown report to FILE")
    args = parser.parse_args()

    workers = [int(w) for w in args.workers.split(",") if w.strip()]

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        text_pdf = tmpdir / "text.pdf"
        corpus_note = "synthetic"
        if args.corpus:
            try:
                total, used = build_corpus_pdf(text_pdf, args.corpus_limit)
                page_nums = list(range(total))
                corpus_note = f"arXiv corpus ({used} docs, {total} pages)"
                args.no_ocr = True  # real papers have a text layer
            except RuntimeError as exc:
                print(f"--corpus unavailable ({exc}); using synthetic PDF.\n")
                build_text_pdf(text_pdf, args.pages)
                page_nums = list(range(args.pages))
        else:
            build_text_pdf(text_pdf, args.pages)
            page_nums = list(range(args.pages))

        header = [
            "# Parallel page processing benchmark",
            "",
            f"- CPUs: {os.cpu_count()}",
            f"- PyMuPDF: {pymupdf.version[0]}",
            f"- Process start method: {multiprocessing.get_start_method()} "
            "(fork = cheap workers; spawn = ~0.3-0.5s/worker startup)",
            f"- Corpus: {corpus_note}",
            f"- Pages: {len(page_nums)} | runs: {args.runs} | render DPI: {args.dpi}",
            "- Sequential = one shared Document, looped (today's code).",
            "- Threaded = one Document opened per task (thread-safe), bounded pool.",
            "- speedup is vs. the sequential baseline, using best-of-runs (min).",
            "",
        ]
        print("\n".join(header))

        text_rows = text_benchmark(str(text_pdf), page_nums, workers, args.runs)
        text_md = render_table("Text extraction (non-OCR)", text_rows)
        print(text_md)

        render_rows = render_benchmark(
            str(text_pdf), page_nums, args.dpi, workers, args.runs
        )
        render_md = render_table(f"Render @ {args.dpi} DPI", render_rows)
        print(render_md)

        sections = [text_md, render_md]

        if args.no_ocr:
            note = "_OCR benchmark skipped (--no-ocr)._\n"
            print(note)
            sections.append(note)
        else:
            try:
                check_tesseract_available()
                ocr_pdf = tmpdir / "scanned.pdf"
                build_scanned_pdf(text_pdf, ocr_pdf, args.pages, dpi=150)
                ocr_rows = ocr_benchmark(str(ocr_pdf), page_nums, workers, args.runs)
                ocr_md = render_table("OCR @ 300 DPI (Tesseract)", ocr_rows)
                print(ocr_md)
                sections.append(ocr_md)
            except RuntimeError as exc:
                note = f"_OCR benchmark skipped: {exc}_\n"
                print(note)
                sections.append(note)

        # --- End-to-end pdf_read_pages(render_dpi) benchmark ---
        # Measures the REAL tool path: render dispatch + serial text/images/tables
        # per page (find_tables stays in the parent). The isolated render numbers
        # above overstate the gain; this section shows the honest Amdahl result.
        # Uses the same synthetic PDF (24 pages) as the render benchmark above.
        # Always run 1/4/8 workers for comparability; not gated by --workers.
        e2e_workers = [1, 4, 8]
        e2e_note = (
            "End-to-end pdf_read_pages(render_dpi=200): wall time including"
            " serial text/images/tables extraction per page."
        )
        print(f"\n### {e2e_note}")
        print("")
        print("| workers | wall (s) | speedup |")
        print("|--------:|---------:|--------:|")
        e2e_baseline: float | None = None
        e2e_rows: list[dict[str, Any]] = []
        for w in e2e_workers:
            t = time_read_pages_render(str(text_pdf), len(page_nums), w)
            if e2e_baseline is None:
                e2e_baseline = t
            speedup = e2e_baseline / t if t else 0.0
            print(f"| {w} | {t:.3f} | {speedup:.2f}x |")
            e2e_rows.append({"workers": w, "wall_s": t, "speedup": speedup})
        print("")
        e2e_md_lines = [
            f"### {e2e_note}",
            "",
            "| workers | wall (s) | speedup |",
            "|--------:|---------:|--------:|",
        ]
        for r in e2e_rows:
            e2e_md_lines.append(
                f"| {r['workers']} | {r['wall_s']:.3f} | {r['speedup']:.2f}x |"
            )
        e2e_md_lines.append("")
        e2e_md = "\n".join(e2e_md_lines)
        sections.append(e2e_md)

    if args.output:
        args.output.write_text("\n".join(header) + "\n" + "\n".join(sections))
        print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
