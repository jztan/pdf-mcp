#!/usr/bin/env python3
"""Measure the cost of widening page_text's primary key (issue #27).

SPIKE OUTPUT. This is the risk side of the change: SQLite cannot alter a
primary key, so `(file_path, page_num)` -> `(file_path, page_num, ocr_lang)`
means create-new, copy, drop, rename against every existing user's
cache.db. The question is whether that is seconds or minutes on a warmed
corpus, because a slow blocking migration needs a different design.

Builds a synthetic cache at several scales, runs the migration, and
verifies row counts and content survive. No Tesseract, no network, fully
deterministic.

    python scripts/measure_page_text_migration.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import struct
import sys
import tempfile
import time

# Mirrors the live schema at cache.py:347 closely enough to time the copy.
CREATE_OLD = """
CREATE TABLE page_text (
    file_path TEXT NOT NULL,
    page_num INTEGER NOT NULL,
    file_mtime REAL NOT NULL,
    text TEXT NOT NULL,
    text_length INTEGER NOT NULL,
    source TEXT DEFAULT 'extracted',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    has_hidden_text INTEGER DEFAULT NULL,
    ocr_lang TEXT DEFAULT NULL,
    PRIMARY KEY (file_path, page_num)
);
"""

CREATE_EMB = """
CREATE TABLE page_embeddings (
    file_path TEXT NOT NULL,
    page_num INTEGER NOT NULL,
    file_mtime REAL NOT NULL,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL DEFAULT 'BAAI/bge-small-en-v1.5',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (file_path, page_num)
);
"""

# ocr_lang becomes NOT NULL with a '' sentinel for non-OCR rows: SQLite
# permits NULL in a non-INTEGER primary key column, so leaving it nullable
# would stop extracted rows being unique and let them accumulate
# duplicates on every re-extraction.
CREATE_NEW = """
CREATE TABLE page_text_new (
    file_path TEXT NOT NULL,
    page_num INTEGER NOT NULL,
    file_mtime REAL NOT NULL,
    text TEXT NOT NULL,
    text_length INTEGER NOT NULL,
    source TEXT DEFAULT 'extracted',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    has_hidden_text INTEGER DEFAULT NULL,
    ocr_lang TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (file_path, page_num, ocr_lang)
);
"""

PAGE_TEXT_SAMPLE = (
    "Optical character recognition on a scanned page is never a single "
    "decision. The engine segments the image into lines, the lines into "
    "words, and the words into character candidates, and only then does a "
    "language model rank the readings that the shapes allow. " * 6
)


def build_cache(db_path: str, n_rows: int, ocr_fraction: float) -> None:
    """Populate a synthetic cache: n_rows pages spread over 50-page docs."""
    conn = sqlite3.connect(db_path)
    conn.executescript(CREATE_OLD + CREATE_EMB)
    conn.execute("CREATE INDEX idx_page_text_path ON page_text(file_path)")

    blob = struct.pack("384f", *([0.05] * 384))
    rows, embs = [], []
    for i in range(n_rows):
        doc = f"/Users/someone/docs/scan_{i // 50:04d}.pdf"
        page = i % 50
        is_ocr = (i % 100) < int(ocr_fraction * 100)
        rows.append(
            (
                doc,
                page,
                1755000000.0,
                PAGE_TEXT_SAMPLE,
                len(PAGE_TEXT_SAMPLE),
                "ocr" if is_ocr else "extracted",
                "rus+eng" if is_ocr else None,
            )
        )
        embs.append((doc, page, 1755000000.0, blob))
    conn.executemany(
        "INSERT INTO page_text (file_path, page_num, file_mtime, text,"
        " text_length, source, ocr_lang) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO page_embeddings (file_path, page_num, file_mtime,"
        " embedding) VALUES (?, ?, ?, ?)",
        embs,
    )
    conn.commit()
    conn.close()


def migrate(db_path: str) -> tuple[float, float, int]:
    """Run the migration; return (migrate_s, vacuum_s, size_before_vacuum).

    VACUUM is part of the migration, not an optional extra: drop-and-rename
    leaves the freed pages in the file, so without it the database stays
    ~50% larger forever. It cannot run inside a transaction, so it follows
    the commit.
    """
    conn = sqlite3.connect(db_path)
    t0 = time.perf_counter()
    conn.executescript(CREATE_NEW + """
        INSERT INTO page_text_new
            (file_path, page_num, file_mtime, text, text_length,
             source, created_at, has_hidden_text, ocr_lang)
        SELECT file_path, page_num, file_mtime, text, text_length,
               source, created_at, has_hidden_text,
               COALESCE(ocr_lang, '')
        FROM page_text;
        DROP TABLE page_text;
        ALTER TABLE page_text_new RENAME TO page_text;
        CREATE INDEX idx_page_text_path ON page_text(file_path);
        """)
    conn.commit()
    elapsed = time.perf_counter() - t0

    # Measured here, between commit and VACUUM: this is the inflated size a
    # user would be left with if the migration skipped the VACUUM.
    size_unvacuumed = pathlib.Path(db_path).stat().st_size

    t1 = time.perf_counter()
    conn.execute("VACUUM")
    vacuum_elapsed = time.perf_counter() - t1

    conn.close()
    return elapsed, vacuum_elapsed, size_unvacuumed


def verify(db_path: str, n_rows: int) -> None:
    """A migration that loses or mangles rows is worse than no migration."""
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM page_text").fetchone()[0]
    nulls = conn.execute(
        "SELECT COUNT(*) FROM page_text WHERE ocr_lang IS NULL"
    ).fetchone()[0]
    ocr_langs = conn.execute(
        "SELECT COUNT(*) FROM page_text WHERE source = 'ocr'"
        " AND ocr_lang = 'rus+eng'"
    ).fetchone()[0]
    extracted = conn.execute(
        "SELECT COUNT(*) FROM page_text WHERE source = 'extracted'" " AND ocr_lang = ''"
    ).fetchone()[0]
    sample = conn.execute("SELECT text FROM page_text LIMIT 1").fetchone()[0]
    conn.close()
    problems = []
    if count != n_rows:
        problems.append(f"row count {count} != {n_rows}")
    if nulls:
        problems.append(f"{nulls} NULL ocr_lang rows survived")
    if ocr_langs + extracted != n_rows:
        problems.append(
            f"backfill split {ocr_langs} ocr + {extracted} extracted" f" != {n_rows}"
        )
    if sample != PAGE_TEXT_SAMPLE:
        problems.append("page text was mangled")
    if problems:
        sys.exit("[FAIL] " + "; ".join(problems))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--scales",
        type=int,
        nargs="+",
        default=[1000, 5000, 20000, 50000],
        help="page_text row counts to time",
    )
    ap.add_argument("--ocr-fraction", type=float, default=0.2)
    ap.add_argument(
        "--out",
        default="benchmark_data/page_text_migration_results.json",
    )
    args = ap.parse_args()

    results = []
    for n in args.scales:
        with tempfile.TemporaryDirectory() as d:
            db = str(pathlib.Path(d) / "cache.db")
            build_cache(db, n, args.ocr_fraction)
            size_before = pathlib.Path(db).stat().st_size
            elapsed, vacuum_elapsed, size_unvacuumed = migrate(db)
            verify(db, n)
            size_after = pathlib.Path(db).stat().st_size
            results.append(
                {
                    "rows": n,
                    "seconds": round(elapsed, 3),
                    "vacuum_seconds": round(vacuum_elapsed, 3),
                    "db_mb_before": round(size_before / 1e6, 1),
                    "db_mb_unvacuumed": round(size_unvacuumed / 1e6, 1),
                    "db_mb_after_vacuum": round(size_after / 1e6, 1),
                }
            )
            print(
                f"{n:>7,} rows  migrate {elapsed:>6.3f}s  "
                f"vacuum {vacuum_elapsed:>6.3f}s  "
                f"{size_before / 1e6:>6.1f}MB -> "
                f"{size_unvacuumed / 1e6:.1f}MB -> {size_after / 1e6:.1f}MB"
            )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"ocr_fraction": args.ocr_fraction, "results": results}, indent=2)
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
