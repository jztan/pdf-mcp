#!/usr/bin/env python
"""Per-OS WAL vs rollback on the REAL cache write path.

The cache ships in WAL mode on every platform. WAL's payoff is the per-commit
durability cost it removes, dominated by fsync expense plus the rollback
journal's per-transaction file create/delete, and that cost is a property of
the OS/filesystem. Measured on the real `save_page_text` path, WAL is faster
than the rollback journal on macOS (~1.2x) and Linux (~2.5x); Windows, where
creating and deleting a journal file per commit is most expensive, is its
strongest case but cannot be measured on a developer Mac.

This harness exists to be run by CI on real Linux and Windows runners so the
WAL decision is backed by numbers rather than inferred. It is deliberately
faithful to the product: it times the actual `PDFCache.save_page_text` (a
page_text insert + FTS index + commit) with the connection lifecycle the
cache really uses. An earlier isolated mock that closed a connection per page
forced a WAL checkpoint on every write and reported the opposite verdict, so
only the real path is trustworthy here.

It is informational and never fails the build: the honest floor should come
from these numbers, not a guessed threshold.

    uv run python scripts/benchmark_cache_write.py --ops 400 --repeats 5
"""

from __future__ import annotations

import argparse
import gc
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

from pdf_mcp.cache import PDFCache

_STUB_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
_PAGE_TEXT = "the quick brown fox jumps over the lazy dog " * 48  # ~2KB, multi-token


def _force_rollback(cache: PDFCache) -> None:
    """Switch a WAL cache to the rollback journal for the A/B comparison.

    The cache ships WAL, so measuring the rollback arm means switching an
    existing WAL database. Release the constructor's connection first
    (switching out of WAL needs exclusive access), then flip the persistent
    journal mode and record it so `_connect` keeps synchronous=FULL, matching
    a real rollback-mode cache.
    """
    gc.collect()
    raw = sqlite3.connect(cache.db_path, isolation_level=None)
    try:
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        got = str(raw.execute("PRAGMA journal_mode=delete").fetchone()[0]).lower()
    finally:
        raw.close()
    if got != "delete":
        raise SystemExit(f"could not force rollback mode (got {got!r})")
    cache.journal_mode = "delete"


def run_arm(mode: str, ops: int) -> float:
    """Mean ms per real save_page_text with the journal mode set to `mode`."""
    tmp = Path(tempfile.mkdtemp(prefix=f"cachewrite-{mode}-"))
    try:
        doc = tmp / "doc.pdf"
        doc.write_bytes(_STUB_PDF)
        cache = PDFCache(cache_dir=tmp, ttl_hours=1)  # ships WAL
        if mode == "delete":
            _force_rollback(cache)
        if cache.journal_mode != mode:
            raise SystemExit(f"arm {mode!r} did not take (got {cache.journal_mode!r})")
        for i in range(20):  # warm the path, unmeasured
            cache.save_page_text(str(doc), i, _PAGE_TEXT)
        gc.collect()
        start = time.perf_counter()
        for i in range(ops):
            cache.save_page_text(str(doc), i, _PAGE_TEXT)
        return (time.perf_counter() - start) / ops * 1000.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ops", type=int, default=400)
    ap.add_argument("--repeats", type=int, default=5)
    args = ap.parse_args(argv)

    res = {}
    for mode in ("delete", "wal"):
        runs = [run_arm(mode, args.ops) for _ in range(args.repeats)]
        res[mode] = statistics.median(runs)

    ratio = res["delete"] / res["wal"]
    print(
        f"platform: {platform.system()} ({sys.platform})  "
        f"python {platform.python_version()}  sqlite {sqlite3.sqlite_version}"
    )
    print(f"{args.ops} save_page_text ops x {args.repeats} repeats")
    print(f"  rollback (delete): {res['delete']:.3f} ms/op")
    print(f"  wal              : {res['wal']:.3f} ms/op")
    verdict = "WAL faster" if ratio >= 1 else "WAL slower"
    print(f"WAL is {ratio:.2f}x the rollback throughput  ({verdict})")
    return 0  # informational; never fails the build


if __name__ == "__main__":
    sys.exit(main())
