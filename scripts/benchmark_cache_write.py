#!/usr/bin/env python3
"""Per-operation cost of the REAL PDFCache write path, WAL vs rollback.

Why this exists rather than a synthetic sqlite3 loop: the synthetic version
gave three different answers depending on details PDFCache does not share
(whether a connection is closed explicitly, whether two connections briefly
overlap, whether the -wal file survives). Those details move the result by
40x, so only the actual product path settles whether WAL is a win.

Runs the same code twice, switching arms with PDF_MCP_JOURNAL_MODE, and
reports the SQLite version the verdict belongs to -- the answer differs by
build, which is the whole point.

    python scripts/benchmark_cache_write.py --ops 300 --repeats 5
"""

import argparse
import json
import os
import platform
import shutil
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

_STUB_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def run_arm(mode: str, ops: int) -> float:
    """Mean ms per save_page_text against a fresh cache in `mode`."""
    os.environ["PDF_MCP_JOURNAL_MODE"] = mode

    # Imported here, after the env var is set, so each arm builds its cache
    # under the mode being measured.
    from pdf_mcp.cache import PDFCache

    tmp = Path(tempfile.mkdtemp(prefix=f"cache-write-{mode}-"))
    try:
        doc = tmp / "doc.pdf"
        doc.write_bytes(_STUB_PDF)
        cache = PDFCache(cache_dir=tmp)
        if cache.journal_mode != mode:
            raise SystemExit(
                f"arm {mode!r} did not take effect (got {cache.journal_mode!r})"
            )

        start = time.perf_counter()
        for i in range(ops):
            cache.save_page_text(str(doc), i, "x" * 2000)
        elapsed = time.perf_counter() - start
        return (elapsed / ops) * 1000.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ops", type=int, default=300)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    results = {}
    for mode in ("delete", "wal"):
        runs = [run_arm(mode, args.ops) for _ in range(args.repeats)]
        results[mode] = {
            "median_ms": round(statistics.median(runs), 3),
            "min_ms": round(min(runs), 3),
            "max_ms": round(max(runs), 3),
        }

    verdict = results["delete"]["median_ms"] / results["wal"]["median_ms"]

    print(f"platform: {platform.system()} {platform.release()}")
    print(f"python {platform.python_version()}  sqlite {sqlite3.sqlite_version}")
    print(f"{args.ops} save_page_text ops x {args.repeats} repeats\n")
    print(f"{'journal mode':<16} {'median':>10} {'min':>10} {'max':>10}")
    print("-" * 50)
    for mode, r in results.items():
        print(
            f"{mode:<16} {r['median_ms']:>9.3f}ms"
            f" {r['min_ms']:>9.3f}ms {r['max_ms']:>9.3f}ms"
        )
    print(
        f"\nWAL is {verdict:.2f}x "
        f"{'FASTER' if verdict >= 1 else 'SLOWER'} than the rollback journal"
    )

    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {
                    "platform": platform.system(),
                    "python_version": platform.python_version(),
                    "sqlite_version": sqlite3.sqlite_version,
                    "wal_speedup": round(verdict, 3),
                    "results": results,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
