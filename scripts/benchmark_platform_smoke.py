"""Product-path timings for cross-platform comparison.

The Windows CI job runs the test suite ~2.3x slower than Linux and
parallelises at 1.05x where Linux gets 41%. The test suite is not the
product, but it exercises the product, so the question this script
answers directly is: which of those costs does a Windows USER feel?

It times the user-facing paths a real session hits, on fixtures built
in a temp dir, and prints one JSON object. Run it on two platforms via
the platform-bench workflow and compare like for like. No assertions:
this is a measurement harness, not a gate.

Paths timed:
  open_cold        first open_pdf + pdf_info on a 500-page PDF (cache cold)
  info_warm        pdf_info again (cache hit)
  read_pages       pdf_read_pages, 10 text pages
  search_keyword   pdf_search keyword mode, cold then warm
  render           pdf_render_pages, 4 pages at 150dpi (spawn pool path)
  ocr_page         one 200dpi scanned page through the OCR path
  warm_corpus      pdf_corpus_warm over 6 small docs (text only,
                   embeddings off via a stub to keep the model download
                   out of the measurement)
  spawn_roundtrip  one ProcessPoolExecutor submit/result round trip,
                   the unit cost every parallel path pays per worker

Result so far: this harness found that cold pdf_search and corpus warm
were both dominated by SQLite commit cost on Windows (fsync per commit),
not by extraction, which measures FASTER there than on Linux. Both are
now batched into one transaction each. Reach for the pooled/sequential/
extract-only split below before theorising about a platform gap.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _build_fixtures(root: Path) -> dict[str, str]:
    import pymupdf  # dev dependency; fixture building only

    big = root / "big.pdf"
    doc = pymupdf.open()
    for i in range(500):
        page = doc.new_page()
        page.insert_text(
            (50, 50),
            f"Page {i + 1}. Retrieval corpus text with searchable terms: "
            f"latency budget alpha{i % 7} throughput.",
        )
    doc.save(str(big))
    doc.close()

    corpus = root / "corpus"
    corpus.mkdir()
    for d in range(6):
        doc = pymupdf.open()
        for i in range(12):
            page = doc.new_page()
            page.insert_text(
                (50, 50),
                f"Doc {d} page {i + 1}: quarterly filing revenue segment "
                f"cash flow item{d}-{i}.",
            )
        doc.save(str(corpus / f"doc{d}.pdf"))
        doc.close()

    scan = root / "scan.pdf"
    src = pymupdf.open(str(corpus / "doc0.pdf"))
    pix = src[0].get_pixmap(dpi=200)
    out = pymupdf.open()
    page = out.new_page(width=pix.width * 72 / 200, height=pix.height * 72 / 200)
    page.insert_image(page.rect, pixmap=pix)
    out.save(str(scan))
    src.close()
    out.close()
    return {"big": str(big), "corpus": str(corpus), "scan": str(scan)}


def _timed(fn):
    t0 = time.perf_counter()
    fn()
    return round(time.perf_counter() - t0, 3)


def main() -> int:
    results: dict[str, object] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
    }
    workdir = Path(tempfile.mkdtemp(prefix="pdfmcp-bench-"))
    os.environ["PDF_MCP_CACHE_DIR"] = str(workdir / "cache")
    try:
        fixtures = _build_fixtures(workdir)

        # Import AFTER the cache env var is set.
        import pdf_mcp.server as server
        from pdf_mcp.cache import PDFCache

        server.cache = PDFCache(cache_dir=workdir / "cache")

        results["open_cold"] = _timed(lambda: server.pdf_info(fixtures["big"]))
        results["info_warm"] = _timed(lambda: server.pdf_info(fixtures["big"]))
        results["read_pages"] = _timed(
            lambda: server.pdf_read_pages(fixtures["big"], "1-10")
        )
        results["search_cold"] = _timed(
            lambda: server.pdf_search(fixtures["big"], "latency budget", mode="keyword")
        )
        results["search_warm"] = _timed(
            lambda: server.pdf_search(
                fixtures["big"], "throughput alpha3", mode="keyword"
            )
        )
        results["render_4p"] = _timed(
            lambda: server.pdf_render_pages(fixtures["big"], "1-4", dpi=150)
        )

        if shutil.which("tesseract"):
            results["ocr_page"] = _timed(
                lambda: server.pdf_read_pages(fixtures["scan"], "1", ocr=True)
            )
        else:
            results["ocr_page"] = None

        # Corpus warm, text only: stub the embedder so the model download
        # and ONNX inference stay out of a platform I/O comparison.
        from pdf_mcp import corpus as corpus_mod

        t0 = time.perf_counter()
        resolved = corpus_mod.resolve_corpus(fixtures["corpus"], recursive=False)
        files = resolved["files"]
        warm = corpus_mod.warm_docs(
            files,
            budget_seconds=120,
            cache=server.cache,
            embeddings=False,
            model_name=None,
            embed=None,
        )
        results["warm_corpus_6docs"] = round(time.perf_counter() - t0, 3)
        results["warm_docs_done"] = sum(
            1 for d in warm["docs"] if d.get("status") == "warmed"
        )

        # Warm is 4.6x slower on Windows and the cause is not established.
        # Split it three ways so the next run says WHICH part is slow
        # instead of leaving it to reasoning.
        #
        #   warm_pooled      the shipped path (>= 4 uncached docs uses a pool)
        #   warm_sequential  same work, pool forced off, so the delta is the
        #                    pool's cost on this OS
        #   warm_extract     extraction alone in-process, no SQLite writes,
        #                    so warm_sequential minus this is the write cost
        #
        # Each runs on its own copy of the corpus, because a warmed cache
        # makes the next run free and would silently measure nothing.
        import shutil as _shutil

        from pdf_mcp.extractor import _warm_extract_worker

        def _fresh_corpus(tag: str) -> list[str]:
            dst = workdir / f"corpus_{tag}"
            _shutil.copytree(fixtures["corpus"], dst)
            return corpus_mod.resolve_corpus(str(dst), recursive=False)["files"]

        def _timed_warm(tag: str, max_workers: str | None) -> float:
            files_t = _fresh_corpus(tag)
            prev = os.environ.get("PDF_MCP_MAX_WORKERS")
            if max_workers is None:
                os.environ.pop("PDF_MCP_MAX_WORKERS", None)
            else:
                os.environ["PDF_MCP_MAX_WORKERS"] = max_workers
            try:
                t = time.perf_counter()
                corpus_mod.warm_docs(
                    files_t,
                    budget_seconds=300,
                    cache=server.cache,
                    embeddings=False,
                    model_name=None,
                    embed=None,
                )
                return round(time.perf_counter() - t, 3)
            finally:
                if prev is None:
                    os.environ.pop("PDF_MCP_MAX_WORKERS", None)
                else:
                    os.environ["PDF_MCP_MAX_WORKERS"] = prev

        results["warm_pooled"] = _timed_warm("pooled", None)
        results["warm_sequential"] = _timed_warm("seq", "1")

        # Batching four commits into one cut the Windows write cost by only
        # ~20%, so the rest is not commit COUNT. Attribute it per call
        # instead of theorising: wrap each writer and the transaction exit
        # (where the single remaining commit lands) and report the totals.
        timings: dict[str, float] = {}

        def _instrument(name: str):
            real = getattr(server.cache, name)

            def wrapper(*a, **k):
                t = time.perf_counter()
                try:
                    return real(*a, **k)
                finally:
                    timings[name] = round(
                        timings.get(name, 0.0) + time.perf_counter() - t, 3
                    )

            return real, wrapper

        import contextlib as _ctx

        originals = {}
        for _name in (
            "save_metadata",
            "save_pages_text",
            "save_page_blocks",
            "save_pages_hidden_flag",
        ):
            originals[_name], _w = _instrument(_name)
            setattr(server.cache, _name, _w)

        real_tx = server.cache.write_transaction

        @_ctx.contextmanager
        def timed_tx():
            t_open = time.perf_counter()
            with real_tx() as conn:
                timings["tx_open"] = round(
                    timings.get("tx_open", 0.0) + time.perf_counter() - t_open, 3
                )
                t_body = time.perf_counter()
                yield conn
                timings["tx_body"] = round(
                    timings.get("tx_body", 0.0) + time.perf_counter() - t_body, 3
                )
                t_commit = time.perf_counter()
            timings["tx_commit"] = round(
                timings.get("tx_commit", 0.0) + time.perf_counter() - t_commit, 3
            )

        server.cache.write_transaction = timed_tx
        try:
            files_i = _fresh_corpus("instrumented")
            os.environ["PDF_MCP_MAX_WORKERS"] = "1"
            t = time.perf_counter()
            corpus_mod.warm_docs(
                files_i,
                budget_seconds=300,
                cache=server.cache,
                embeddings=False,
                model_name=None,
                embed=None,
            )
            timings["warm_total"] = round(time.perf_counter() - t, 3)
        finally:
            os.environ.pop("PDF_MCP_MAX_WORKERS", None)
            for _name, _real in originals.items():
                setattr(server.cache, _name, _real)
            server.cache.write_transaction = real_tx
        results["warm_write_breakdown"] = timings

        extract_files = _fresh_corpus("extract")
        t0 = time.perf_counter()
        for f in extract_files:
            _warm_extract_worker(f)
        results["warm_extract_only"] = round(time.perf_counter() - t0, 3)

        # The unit cost every parallel feature pays per worker on this OS.
        from concurrent.futures import ProcessPoolExecutor

        t0 = time.perf_counter()
        with ProcessPoolExecutor(max_workers=1) as pool:
            pool.submit(int, 1).result()
        results["spawn_roundtrip"] = round(time.perf_counter() - t0, 3)

        print(json.dumps(results, indent=2))
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
