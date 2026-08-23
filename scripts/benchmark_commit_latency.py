#!/usr/bin/env python3
"""Per-commit latency by SQLite journal_mode x synchronous pairing.

Reproduces the measurement behind the WAL cutover. Rollback-journal mode
costs ~22ms per commit on Windows against ~0.5ms on Linux; the third row
(synchronous=OFF, no fsync at all) is the load-bearing one, because it is
still ~29x slower on Windows and so proves the cost is the per-transaction
journal FILE, not the disk flush. WAL keeps one persistent file and appends.

Run on both platforms and compare. Reports median plus spread over N
repeats rather than a single run, because CI runners are noisy enough that
a single measurement of a small cell means nothing.

    python scripts/benchmark_commit_latency.py --repeats 5
"""

import argparse
import json
import platform
import shutil
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

PAIRINGS = [
    ("delete", "FULL"),
    ("delete", "NORMAL"),
    ("delete", "OFF"),
    ("wal", "FULL"),
    ("wal", "NORMAL"),
]


def time_commits(
    db_path: Path,
    journal: str,
    sync: str,
    n_txns: int,
    per_connection: bool = False,
) -> float:
    """Mean milliseconds per commit for one pairing.

    per_connection mirrors how PDFCache actually behaves: a fresh connection
    per operation, pragmas re-applied, closed by refcount. That matters
    because SQLite deletes the -wal/-shm files when the last connection
    closes on some versions, so the first statement after each connect can
    pay to recreate them. Measuring only the single-connection case hides
    that entirely, which is exactly what happened the first time round.
    """
    if db_path.exists():
        db_path.unlink()
    for sidecar in ("-wal", "-shm", "-journal"):
        p = db_path.with_name(db_path.name + sidecar)
        if p.exists():
            p.unlink()

    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA journal_mode={journal}")
    conn.execute(f"PRAGMA synchronous={sync}")
    conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    if per_connection:
        conn.close()

    start = time.perf_counter()
    for i in range(n_txns):
        if per_connection:
            conn = sqlite3.connect(db_path)
            conn.execute(f"PRAGMA busy_timeout={5000}")
            conn.execute(f"PRAGMA synchronous={sync}")
        conn.execute("INSERT INTO t (v) VALUES (?)", (f"row {i}",))
        conn.commit()
        if per_connection:
            conn.close()
    elapsed = time.perf_counter() - start
    if not per_connection:
        conn.close()
    return (elapsed / n_txns) * 1000.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--txns", type=int, default=40)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="commit-latency-"))
    db = tmp / "bench.db"
    results = {}

    try:
        for journal, sync in PAIRINGS:
            for per_conn in (False, True):
                runs = [
                    time_commits(db, journal, sync, args.txns, per_conn)
                    for _ in range(args.repeats)
                ]
                shape = "conn-per-op" if per_conn else "one-conn"
                results[f"{journal}/{sync} [{shape}]"] = {
                    "median_ms": round(statistics.median(runs), 3),
                    "min_ms": round(min(runs), 3),
                    "max_ms": round(max(runs), 3),
                }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"platform: {platform.system()} {platform.release()}")
    print(f"python {platform.python_version()}  sqlite {sqlite3.sqlite_version}")
    print(f"{args.txns} transactions x {args.repeats} repeats\n")
    print(f"{'journal / synchronous':<40} {'median':>9} {'min':>9} {'max':>9}")
    print("-" * 70)
    for name, r in results.items():
        print(
            f"{name:<40} {r['median_ms']:>8.3f}ms"
            f" {r['min_ms']:>8.3f}ms {r['max_ms']:>8.3f}ms"
        )

    if args.json_out:
        payload = {
            "platform": platform.system(),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "results": results,
        }
        args.json_out.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
