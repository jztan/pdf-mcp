#!/usr/bin/env python
"""
scripts/benchmark_warm_concurrency.py

Spike: does concurrent (process-pool) warm beat the current sequential
`corpus.warm_docs`, and by how much, on a real corpus?

Zero production-code changes. This script prototypes a concurrent warm
(extraction in workers, ALL SQLite writes in the parent, mirroring the
project's all-writes-in-parent rule) and times it against the real
sequential `warm_docs`.

Two modes:
  --mode text        text-only warm (default)
  --mode embeddings  text + embeddings warm (requires the [semantic] extra;
                     concurrent variant extracts in workers, then does ONE
                     parent-side batched encode)

Correctness note: pdf-mcp's column-aware `extract_text_from_page` is itself
nondeterministic on some multi-column pages (an intermittent PyMuPDF
get_text(clip, sort=True) heisenbug), independent of concurrency. So a
concurrent-vs-sequential text difference is the extractor's own
nondeterminism, not corruption. The real corruption gate here is: every
doc warmed, page counts match, and no page is empty where sequential had
text. Text differences are reported separately as information.

Run:
    uv run python scripts/benchmark_warm_concurrency.py --docs 100
    uv run python scripts/benchmark_warm_concurrency.py --mode embeddings --docs 40
Always exits 0 (informational; no CI gate).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import pymupdf  # noqa: E402
from pdf_mcp import embedder  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.config import PDFConfig  # noqa: E402
from pdf_mcp.corpus import warm_docs  # noqa: E402  (sequential baseline)
from pdf_mcp.extractor import (  # noqa: E402
    extract_metadata,
    extract_text_from_page,
    extract_toc,
)

MANIFEST = REPO / "benchmark_data" / "corpus_search" / "manifest.json"
WORKER_COUNTS = [1, 2, 4, 8]


# Top-level, picklable worker (spawn-safe on macOS). A child re-imports this
# module, so this must stay at module scope and re-import only PyMuPDF-level
# deps (cheap), exactly like parallel.py's per-page workers.
def _extract_doc(path: str) -> tuple:
    """Extract everything one doc needs, in a worker. No cache writes here."""
    doc = pymupdf.open(path)
    try:
        page_count = len(doc)
        metadata = extract_metadata(doc)
        toc = extract_toc(doc)
        texts: dict[int, str] = {}
        coverage: list[dict[str, int]] = []
        for pn in range(page_count):
            page = doc[pn]
            texts[pn] = extract_text_from_page(page, sort_by_position=True)
            coverage.append(
                {
                    "page": pn + 1,
                    "text_chars": len(page.get_text()),
                    "raster_images": len({img[0] for img in page.get_images()}),
                }
            )
    finally:
        doc.close()
    return path, page_count, metadata, toc, texts, coverage


def warm_concurrent_text(paths: list[str], cache: PDFCache, workers: int) -> None:
    """Concurrent text warm: extract in workers, write in parent."""
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for path, page_count, metadata, toc, texts, _cov in ex.map(
            _extract_doc, paths
        ):
            cache.save_metadata(path, page_count, metadata, toc, text_coverage=_cov)
            cache.save_pages_text(path, texts)


def warm_concurrent_embeddings(
    paths: list[str], cache: PDFCache, workers: int, model: str
) -> None:
    """Concurrent embeddings warm: extract in workers, ONE parent-side
    batched encode, then write. Tests the parent-batched-embeddings design."""
    extracted = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for path, page_count, metadata, toc, texts, cov in ex.map(
            _extract_doc, paths
        ):
            cache.save_metadata(path, page_count, metadata, toc, text_coverage=cov)
            cache.save_pages_text(path, texts)
            extracted.append((path, texts))
    # Gather every non-empty page across all docs, encode in one batch.
    flat_texts: list[str] = []
    index: list[tuple[str, int]] = []
    for path, texts in extracted:
        for pn, t in sorted(texts.items()):
            if t.strip():
                index.append((path, pn))
                flat_texts.append(t)
    if not flat_texts:
        return
    vecs = embedder.encode(flat_texts, model)
    per_doc: dict[str, dict[int, bytes]] = {}
    for (path, pn), v in zip(index, vecs):
        per_doc.setdefault(path, {})[pn] = v.tobytes()
    for path, blobs in per_doc.items():
        cache.save_page_embeddings(path, blobs, model)


def _cold_cache(root: Path, tag: str) -> PDFCache:
    d = root / tag
    d.mkdir(parents=True, exist_ok=True)
    return PDFCache(cache_dir=d, ttl_hours=1)


def _corruption_check(
    cache: PDFCache, ref: PDFCache, paths: list[str], pages: dict[str, int]
) -> tuple[int, int]:
    """Return (docs_corrupt, docs_text_differ). Corrupt = wrong page count or
    an empty page where the reference had text. Differ = any text mismatch
    (expected: extractor nondeterminism, not corruption)."""
    corrupt = differ = 0
    for p in paths:
        want = ref.get_pages_text(p, list(range(pages[p])))
        got = cache.get_pages_text(p, list(range(pages[p])))
        if len(got) != len(want):
            corrupt += 1
            continue
        bad = any(
            (want[pn].strip() and not got.get(pn, "").strip()) for pn in want
        )
        if bad:
            corrupt += 1
        if got != want:
            differ += 1
    return corrupt, differ


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", type=int, default=100, help="cap doc count")
    ap.add_argument("--mode", choices=["text", "embeddings"], default="text")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text())
    paths = [
        str(REPO / d["path"])
        for d in manifest["docs"]
        if (REPO / d["path"]).exists()
    ][: args.docs]
    if not paths:
        print("No corpus PDFs available locally; aborting.")
        return 1

    model = None
    if args.mode == "embeddings":
        model = PDFConfig().embedding_model
        embedder.check_available(model)
        embedder.encode(["warmup"], model)  # one-time model load, untimed

    def embed_fn(texts):
        return [v.tobytes() for v in embedder.encode(texts, model)]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seq_cache = _cold_cache(root, "seq")
        t0 = time.perf_counter()
        if args.mode == "text":
            warm = warm_docs(paths, budget_seconds=10_000, cache=seq_cache)
        else:
            warm = warm_docs(
                paths, budget_seconds=10_000, cache=seq_cache,
                embeddings=True, model_name=model, embed=embed_fn,
            )
        seq_s = time.perf_counter() - t0
        pages = {d["path"]: d["pages"] for d in warm["docs"]}
        total_pages = sum(pages.values())

        rows = [("sequential (warm_docs)", seq_s, 0, 0)]
        for k in WORKER_COUNTS:
            cache = _cold_cache(root, f"c{k}")
            t0 = time.perf_counter()
            if args.mode == "text":
                warm_concurrent_text(paths, cache, k)
            else:
                warm_concurrent_embeddings(paths, cache, k, model)
            secs = time.perf_counter() - t0
            corrupt, differ = _corruption_check(cache, seq_cache, paths, pages)
            rows.append((f"concurrent (workers={k})", secs, corrupt, differ))

    print(f"\nMode: {args.mode} | Corpus: {len(paths)} docs, {total_pages} "
          f"pages | cold cache each run\n")
    print(f"{'config':26s} {'wall(s)':>9s} {'docs/s':>8s} {'vs seq':>8s} "
          f"{'corrupt':>8s} {'txt-diff':>9s}")
    print("-" * 74)
    w1 = None
    for name, secs, corrupt, differ in rows:
        if "workers=1)" in name:
            w1 = secs
        info = "" if name.startswith("seq") else f"{corrupt:>8d} {differ:>9d}"
        print(f"{name:26s} {secs:9.1f} {len(paths)/secs:8.2f} "
              f"{seq_s/secs:7.2f}x {info}")
    best = min(r[1] for r in rows if "workers" in r[0])
    print(f"\nbest speedup vs sequential warm_docs: {seq_s/best:.2f}x")
    if w1:
        print(f"pure concurrency scaling (workers=1 -> best): {w1/best:.2f}x")
    print("corrupt = wrong page count or empty-where-ref-had-text (real bug "
          "signal). txt-diff = docs whose text differs (extractor "
          "nondeterminism, expected, not corruption).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
