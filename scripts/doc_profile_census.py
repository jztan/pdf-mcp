"""Census of document-profile head pages in a warm cache.

Counts how many docs open on an empty or thin page 1, which page the
"first page with text" rule would pick instead, and optionally compares
two caches byte for byte (page text and page embeddings) or times the
steady-state profile backfill. Reads cached text only, never a PDF.

    uv run python scripts/doc_profile_census.py --cache-dir DIR \\
        --manifest benchmark_data/corpus_search/manifest.json

The head-page rule is defined here, not imported, so the script also runs
against a develop cache: the lowest page whose text, stripped of
TEXT_BLANK_CHARS, is non-empty; -1 when no page has any. Page indexes in
the output are 0-based (the cache's own numbering).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

TEXT_BLANK_CHARS = " \t\n\r\f\v"
THIN_CHARS = 50
BACKFILL_RUNS = 5


def head_page(texts: dict[int, str]) -> int:
    """Lowest page index with non-blank text, -1 when there is none."""
    for page in sorted(texts):
        if texts[page].strip(TEXT_BLANK_CHARS):
            return page
    return -1


def _stripped_len(text: str | None) -> int:
    return len((text or "").strip(TEXT_BLANK_CHARS))


def census(cache: Any, paths: list[str]) -> dict[str, Any]:
    docs = empty_page1 = page1_thin = head_thin = 0
    heads: Counter[int] = Counter()
    missing: list[str] = []
    for path in paths:
        meta = cache.get_metadata(path)
        texts = (
            cache.get_pages_text(path, list(range(meta["page_count"]))) if meta else {}
        )
        if not texts:
            missing.append(path)
            continue
        docs += 1
        p1 = _stripped_len(texts.get(0))
        empty_page1 += p1 == 0
        page1_thin += p1 < THIN_CHARS
        head = head_page(texts)
        heads[head] += 1
        head_thin += (_stripped_len(texts[head]) if head >= 0 else 0) < THIN_CHARS
    return {
        "docs": docs,
        "not_cached": missing,
        "empty_page1": empty_page1,
        "page1_under_50": page1_thin,
        "head_page_under_50": head_thin,
        "head_page_counts": {str(k): heads[k] for k in sorted(heads)},
    }


def compare_caches(a: Any, b: Any, paths: list[str]) -> dict[str, Any]:
    """Page text and page embeddings must match byte for byte per doc."""
    from pdf_mcp.embedder import DEFAULT_MODEL

    differ: list[str] = []
    compared = 0
    for path in paths:
        ma, mb = a.get_metadata(path), b.get_metadata(path)
        if not ma or not mb:
            continue
        compared += 1
        pages = list(range(ma["page_count"]))
        same = ma["page_count"] == mb["page_count"]
        same = same and a.get_pages_text(path, pages) == b.get_pages_text(path, pages)
        same = same and a.get_page_embeddings(
            path, pages, DEFAULT_MODEL
        ) == b.get_page_embeddings(path, pages, DEFAULT_MODEL)
        if not same:
            differ.append(path)
    return {"compared": compared, "differ": differ}


def time_backfill(cache: Any, paths: list[str]) -> float:
    """Median seconds of five steady-state backfill passes.

    One untimed pass with the real embedder first, so every profile exists;
    the timed passes use an embed that raises, proving they never encode.
    """
    from pdf_mcp import corpus, embedder

    model = embedder.DEFAULT_MODEL

    def real_embed(texts: list[str]) -> list[bytes]:
        return [v.tobytes() for v in embedder.encode(texts, model)]

    def no_embed(texts: list[str]) -> list[bytes]:
        raise AssertionError("steady-state backfill must not encode")

    corpus.backfill_doc_profiles(paths, cache, model, real_embed)
    runs = []
    for _ in range(BACKFILL_RUNS):
        t0 = time.perf_counter()
        corpus.backfill_doc_profiles(paths, cache, model, no_embed)
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs)


def _open(cache_dir: Path) -> Any:
    import benchmark_corpus_search as bcs

    return bcs.open_validation_cache(cache_dir)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--compare-cache", type=Path, default=None)
    ap.add_argument("--time-backfill", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    paths = [str((REPO / d["path"]).resolve()) for d in manifest["docs"]]
    cache = _open(args.cache_dir)
    out = census(cache, paths)
    out["byte_identical"] = None
    if args.compare_cache is not None:
        out["byte_identical"] = compare_caches(cache, _open(args.compare_cache), paths)
    out["backfill_seconds"] = (
        time_backfill(cache, paths) if args.time_backfill else None
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
