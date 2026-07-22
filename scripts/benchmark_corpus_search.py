#!/usr/bin/env python
"""
scripts/benchmark_corpus_search.py

Stage-2 spike: benchmark corpus-wide temp FTS (global IDF, arm A) vs
RRF fusion of per-doc rank lists (arm B) for cross-document keyword
ranking, on a real multi-doc corpus with hand-authored graded truth.

Subcommands:
    --build-manifest   write benchmark_data/corpus_search/manifest.json
    --validate         check every ground-truth label against page text
    --run              warm corpus, run both arms, write RESULTS.md

Zero production-code changes: this script builds its own scratch SQLite
FTS tables from cached page text. Tokenization mirrors production
(porter unicode61 for Latin, char-split for CJK).
"""

from __future__ import annotations

import argparse  # noqa: F401
import json  # noqa: F401
import re  # noqa: F401
import sqlite3
import sys
import time  # noqa: F401
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdf_mcp.cache import (  # noqa: E402
    _cjk_split,
    _contains_cjk,
    _escape_fts5_query,
    _escape_fts5_query_cjk,
)

import _corpus_ranking as cr  # noqa: E402

OUT_DIR = REPO / "benchmark_data" / "corpus_search"
EN_PDF_DIR = REPO / "benchmark_data" / ".reading_order_pdfs"
CJK_PDF_DIR = REPO / "docs_internal" / "sample_pdfs" / "vertical-jp"
EN_DOC_CAP = 18
CJK_DOC_CAP = 3
TOP_K = 10


# --------------------------------------------------------------------------
# FTS arms
# --------------------------------------------------------------------------


def build_corpus_index(
    conn: sqlite3.Connection, pages: list[tuple[str, int, str]]
) -> None:
    """Arm A index: one corpus-wide FTS5 pair over all (doc, page, text)."""
    conn.execute(
        "CREATE VIRTUAL TABLE corpus_fts USING fts5("
        "doc UNINDEXED, page UNINDEXED, text,"
        " tokenize='porter unicode61')"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE corpus_fts_cjk USING fts5("
        "doc UNINDEXED, page UNINDEXED, text, tokenize='unicode61')"
    )
    for doc, page, text in pages:
        conn.execute(
            "INSERT INTO corpus_fts (doc, page, text) VALUES (?, ?, ?)",
            (doc, page, text),
        )
        conn.execute(
            "INSERT INTO corpus_fts_cjk (doc, page, text) VALUES (?, ?, ?)",
            (doc, page, _cjk_split(text)),
        )


def search_corpus(
    conn: sqlite3.Connection, query: str, top_k: int
) -> list[tuple[str, int]]:
    """Arm A: BM25 over the corpus-wide table (honest global IDF)."""
    if _contains_cjk(query):
        table, fts_query = "corpus_fts_cjk", _escape_fts5_query_cjk(query)
    else:
        table, fts_query = "corpus_fts", _escape_fts5_query(query)
    rows = conn.execute(
        f"SELECT doc, page FROM {table} WHERE {table} MATCH ?"
        f" ORDER BY rank LIMIT ?",
        (fts_query, top_k),
    ).fetchall()
    return [(str(doc), int(page)) for doc, page in rows]


def _doc_table(idx: int, cjk: bool) -> str:
    return f"doc{idx}_fts_cjk" if cjk else f"doc{idx}_fts"


def build_per_doc_indexes(
    conn: sqlite3.Connection, pages: list[tuple[str, int, str]]
) -> list[str]:
    """Arm B indexes: one FTS5 pair per document (per-doc IDF).

    The returned list's order is the table-naming contract: table i is
    named from doc_ids[i] (see `_doc_table`). Callers must pass this
    exact list, in this exact order, to `search_per_doc_rrf` — it
    re-derives table names positionally from the list it is given.
    """
    doc_ids = sorted({doc for doc, _page, _text in pages})
    for i, doc_id in enumerate(doc_ids):
        conn.execute(
            f"CREATE VIRTUAL TABLE {_doc_table(i, False)} USING fts5("
            "page UNINDEXED, text, tokenize='porter unicode61')"
        )
        conn.execute(
            f"CREATE VIRTUAL TABLE {_doc_table(i, True)} USING fts5("
            "page UNINDEXED, text, tokenize='unicode61')"
        )
        for doc, page, text in pages:
            if doc != doc_id:
                continue
            conn.execute(
                f"INSERT INTO {_doc_table(i, False)} (page, text)" " VALUES (?, ?)",
                (page, text),
            )
            conn.execute(
                f"INSERT INTO {_doc_table(i, True)} (page, text)" " VALUES (?, ?)",
                (page, _cjk_split(text)),
            )
    return doc_ids


def search_per_doc_rrf(
    conn: sqlite3.Connection,
    doc_ids: list[str],
    query: str,
    per_doc_k: int,
    top_k: int,
) -> list[tuple[str, int]]:
    """Arm B: query each doc's own index, RRF-fuse the rank lists.

    doc_ids must be the exact, order-preserved list returned by
    build_per_doc_indexes: table names are re-derived positionally from
    this list (table i is `_doc_table(i, ...)`, named from doc_ids[i]
    at build time), so a reordered or filtered list will silently query
    the wrong document's table.
    """
    cjk = _contains_cjk(query)
    fts_query = _escape_fts5_query_cjk(query) if cjk else _escape_fts5_query(query)
    # Cheap guard: the per-doc-index table for doc_ids[i] must exist at
    # position i. Catches doc_ids lists longer than the built tables
    # and stale/empty connections. Cannot detect same-length reorderings
    # or filtered subsets, which silently mislabel results. The ordering
    # contract (pass exactly the list returned by build_per_doc_indexes,
    # unmodified) is the only defense.
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for i in range(len(doc_ids)):
        expected = _doc_table(i, cjk)
        if expected not in existing:
            raise ValueError(
                f"missing table {expected!r} for doc_ids[{i}] = "
                f"{doc_ids[i]!r}; doc_ids must be the exact, "
                "order-preserved list returned by build_per_doc_indexes"
            )
    rank_lists: list[list[tuple[str, int]]] = []
    for i, doc_id in enumerate(doc_ids):
        table = _doc_table(i, cjk)
        rows = conn.execute(
            f"SELECT page FROM {table} WHERE {table} MATCH ?" f" ORDER BY rank LIMIT ?",
            (fts_query, per_doc_k),
        ).fetchall()
        if rows:
            rank_lists.append([(doc_id, int(p)) for (p,) in rows])
    return cr.rrf_fuse_doc_rankings(rank_lists, top_k=top_k)
