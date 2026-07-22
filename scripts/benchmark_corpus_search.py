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

import argparse
import json
import re
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


# --------------------------------------------------------------------------
# Manifest and ground-truth validation
# --------------------------------------------------------------------------


def build_manifest() -> dict:
    """Enumerate local corpus PDFs into a committed manifest.

    EN docs come from the untracked reading-order corpus (sorted, capped
    at EN_DOC_CAP); CJK docs from the local vertical-jp samples (capped
    at CJK_DOC_CAP). Missing directories reduce the corpus, loudly.
    """
    docs: list[dict[str, str]] = []
    if EN_PDF_DIR.is_dir():
        for p in sorted(EN_PDF_DIR.glob("*.pdf"))[:EN_DOC_CAP]:
            docs.append(
                {
                    "id": p.stem,
                    "path": str(p.relative_to(REPO)),
                    "lang": "en",
                }
            )
    else:
        print(f"WARNING: {EN_PDF_DIR} absent; no EN docs in manifest")
    if CJK_PDF_DIR.is_dir():
        for p in sorted(CJK_PDF_DIR.glob("*.pdf"))[:CJK_DOC_CAP]:
            docs.append(
                {
                    "id": p.stem.split("_vertical")[0],
                    "path": str(p.relative_to(REPO)),
                    "lang": "cjk",
                }
            )
    else:
        print(f"WARNING: {CJK_PDF_DIR} absent; no CJK docs in manifest")
    return {
        "description": (
            "Corpus for the cross-doc keyword ranking spike. PDFs are"
            " local-only (not committed); missing files are skipped"
            " loudly at run time."
        ),
        "docs": docs,
    }


def load_manifest() -> dict:
    return json.loads((OUT_DIR / "manifest.json").read_text())


def load_queries() -> dict:
    return json.loads((OUT_DIR / "queries.json").read_text())


_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Whitespace-collapse + casefold for evidence matching."""
    return _WS.sub(" ", text).strip().casefold()


def validate_queries(manifest, queries, page_text_lookup) -> list[str]:
    """Check every label: doc exists in manifest, evidence substring is
    present (normalized) in that doc/page's text. Returns error strings.

    page_text_lookup(doc_id, page_1indexed) -> str
    """
    known = {d["id"] for d in manifest["docs"]}
    errors: list[str] = []
    for q in queries["queries"]:
        for label in q["labels"]:
            if label["doc"] not in known:
                errors.append(f"{q['id']}: unknown doc {label['doc']}")
                continue
            text = page_text_lookup(label["doc"], label["page"])
            if normalize(label["evidence"]) not in normalize(text):
                errors.append(
                    f"{q['id']}: evidence not found on"
                    f" {label['doc']} p{label['page']}:"
                    f" {label['evidence']!r}"
                )
    return errors


def _page_text_lookup_factory(manifest: dict):
    """Open each doc lazily once; return extracted text per 1-indexed page."""
    import pymupdf

    from pdf_mcp.extractor import extract_text_from_page

    docs_by_id = {d["id"]: REPO / d["path"] for d in manifest["docs"]}
    cache: dict[str, list[str]] = {}

    def lookup(doc_id: str, page: int) -> str:
        if doc_id not in cache:
            doc = pymupdf.open(str(docs_by_id[doc_id]))
            cache[doc_id] = [
                extract_text_from_page(doc[i], sort_by_position=True)
                for i in range(len(doc))
            ]
            doc.close()
        pages = cache[doc_id]
        return pages[page - 1] if 1 <= page <= len(pages) else ""

    return lookup


def run_benchmark() -> int:
    raise NotImplementedError("Task 4")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-manifest", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()

    if args.build_manifest:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        manifest = build_manifest()
        (OUT_DIR / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        )
        print(f"wrote manifest with {len(manifest['docs'])} docs")
        return 0

    if args.validate:
        manifest, queries = load_manifest(), load_queries()
        errors = validate_queries(
            manifest, queries, _page_text_lookup_factory(manifest)
        )
        for e in errors:
            print(f"INVALID: {e}")
        print(f"{len(queries['queries'])} queries," f" {len(errors)} label errors")
        return 1 if errors else 0

    if args.run:
        return run_benchmark()  # Task 4

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
