#!/usr/bin/env python
"""
scripts/benchmark_warm_concurrency.py

Measures the SHIPPED concurrent `corpus.warm_docs` (process-pool extract,
all SQLite writes in the parent) against its own sequential path, on a
real corpus. Every arm below calls the real `warm_docs`; the pool size is
forced via the shipped controls (`corpus.WARM_DOC_GATE` +
`PDF_MCP_MAX_WORKERS`), not a prototype implementation.

Two modes:
  --mode text        text-only warm (default)
  --mode embeddings  text + embeddings warm via the real warm_docs
                     (requires the [semantic] extra); extraction runs in
                     spawn workers, per-doc encode and all SQLite writes
                     happen in the parent (_finalize_doc)

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
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from pdf_mcp import corpus as corpus_mod  # noqa: E402
from pdf_mcp import embedder  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.config import PDFConfig  # noqa: E402
from pdf_mcp.corpus import warm_docs  # noqa: E402

MANIFEST = REPO / "benchmark_data" / "corpus_search" / "manifest.json"
TEXT_WORKER_COUNTS = [2, 4, 8]
EMBEDDINGS_WORKER_COUNTS = [2, 4]


def run_warm(paths, cache, mode, model, embed_fn, workers):
    """One warm pass via the real warm_docs, pool size forced via the
    shipped controls (gate + PDF_MCP_MAX_WORKERS)."""
    corpus_mod.WARM_DOC_GATE = 1 if workers > 1 else 10**9
    os.environ["PDF_MCP_MAX_WORKERS"] = str(workers)
    try:
        if mode == "text":
            return warm_docs(paths, budget_seconds=10_000, cache=cache)
        return warm_docs(
            paths,
            budget_seconds=10_000,
            cache=cache,
            embeddings=True,
            model_name=model,
            embed=embed_fn,
        )
    finally:
        os.environ.pop("PDF_MCP_MAX_WORKERS", None)


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
        bad = any((want[pn].strip() and not got.get(pn, "").strip()) for pn in want)
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

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    paths = [
        str(REPO / d["path"]) for d in manifest["docs"] if (REPO / d["path"]).exists()
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

    worker_counts = (
        TEXT_WORKER_COUNTS if args.mode == "text" else EMBEDDINGS_WORKER_COUNTS
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seq_cache = _cold_cache(root, "seq")
        t0 = time.perf_counter()
        warm = run_warm(paths, seq_cache, args.mode, model, embed_fn, workers=1)
        seq_s = time.perf_counter() - t0
        pages = {d["path"]: d["pages"] for d in warm["docs"]}
        total_pages = sum(pages.values())

        rows = [("sequential (warm_docs)", seq_s, 0, 0)]
        for k in worker_counts:
            cache = _cold_cache(root, f"c{k}")
            t0 = time.perf_counter()
            run_warm(paths, cache, args.mode, model, embed_fn, workers=k)
            secs = time.perf_counter() - t0
            corrupt, differ = _corruption_check(cache, seq_cache, paths, pages)
            rows.append((f"concurrent (workers={k})", secs, corrupt, differ))

    print(
        f"\nMode: {args.mode} | Corpus: {len(paths)} docs, {total_pages} "
        f"pages | cold cache each run\n"
    )
    print(
        f"{'config':26s} {'wall(s)':>9s} {'docs/s':>8s} {'vs seq':>8s} "
        f"{'corrupt':>8s} {'txt-diff':>9s}"
    )
    print("-" * 74)
    for name, secs, corrupt, differ in rows:
        info = "" if name.startswith("seq") else f"{corrupt:>8d} {differ:>9d}"
        print(
            f"{name:26s} {secs:9.1f} {len(paths)/secs:8.2f} "
            f"{seq_s/secs:7.2f}x {info}"
        )
    best = min(r[1] for r in rows if "workers" in r[0])
    print(f"\nbest speedup vs sequential warm_docs: {seq_s/best:.2f}x")
    print(
        "corrupt = wrong page count or empty-where-ref-had-text (real bug "
        "signal). txt-diff = docs whose text differs (extractor "
        "nondeterminism, expected, not corruption)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
