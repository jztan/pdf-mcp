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
import time
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
EN_DOC_CAP = 97
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
    exact list, in this exact order, to `search_per_doc_rrf`: it
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
    return json.loads((OUT_DIR / "manifest.json").read_text(encoding="utf-8"))


def load_queries() -> dict:
    return json.loads((OUT_DIR / "queries.json").read_text(encoding="utf-8"))


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


def run_benchmark(force: bool = False) -> int:
    """Warm the corpus, run both arms over all queries, apply the
    pre-committed decision rule, write results.json + RESULTS.md."""
    import tempfile

    import _retrieval_metrics as rm
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.corpus import warm_docs

    manifest, queries = load_manifest(), load_queries()
    paths = [
        str(REPO / d["path"]) for d in manifest["docs"] if (REPO / d["path"]).exists()
    ]
    missing = [d["path"] for d in manifest["docs"] if not (REPO / d["path"]).exists()]
    for m in missing:
        print(f"SKIP (missing locally): {m}")
    if not paths:
        print("No corpus PDFs available locally; aborting.")
        return 1

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cache = PDFCache(cache_dir=Path(tmp), ttl_hours=1)
        t0 = time.perf_counter()
        warm = warm_docs(paths, budget_seconds=3600, cache=cache)
        warm_s = time.perf_counter() - t0
        print(
            f"warmed {warm['warmed_this_call']} docs in {warm_s:.1f}s"
            f" ({len(warm['skipped'])} skipped)"
        )

        id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
        pages: list[tuple[str, int, str]] = []
        for row in warm["docs"]:
            doc_id = id_by_path[row["path"]]
            texts = cache.get_pages_text(row["path"], list(range(row["pages"])))
            for pn, text in sorted(texts.items()):
                pages.append((doc_id, pn + 1, text))  # 1-indexed pages

        # Arm B indexes are built once (production would keep per-doc
        # persistent indexes); arm A rebuilds per query BY DESIGN, since
        # the production candidate is a per-query temp index.
        conn_b = sqlite3.connect(":memory:")
        tb0 = time.perf_counter()
        doc_ids = build_per_doc_indexes(conn_b, pages)
        arm_b_build_s = time.perf_counter() - tb0

        per_query: dict[str, dict] = {}
        arm_a_times: list[float] = []
        for q in queries["queries"]:
            labels = {(lb["doc"], lb["page"]): float(lb["gain"]) for lb in q["labels"]}
            ideal = list(labels.values())
            gold_docs = {lb["doc"] for lb in q["labels"] if lb["gain"] >= 2}

            ta = time.perf_counter()
            conn_a = sqlite3.connect(":memory:")
            build_corpus_index(conn_a, pages)
            ranked_a = search_corpus(conn_a, q["query"], TOP_K)
            conn_a.close()
            a_time = time.perf_counter() - ta
            arm_a_times.append(a_time)

            tqb = time.perf_counter()
            ranked_b = search_per_doc_rrf(
                conn_b, doc_ids, q["query"], per_doc_k=TOP_K, top_k=TOP_K
            )
            b_time = time.perf_counter() - tqb

            per_query[q["id"]] = {
                "class": q["class"],
                "ndcg_a": rm.ndcg_at_k(
                    cr.grade_ranking(ranked_a, labels), ideal, TOP_K
                ),
                "ndcg_b": rm.ndcg_at_k(
                    cr.grade_ranking(ranked_b, labels), ideal, TOP_K
                ),
                "dochit3_a": int(bool({d for d, _p in ranked_a[:3]} & gold_docs)),
                "dochit3_b": int(bool({d for d, _p in ranked_b[:3]} & gold_docs)),
                "a_seconds": round(a_time, 4),
                "b_seconds": round(b_time, 4),
            }

    def class_mean(metric: str, arm: str) -> dict[str, float]:
        out: dict[str, list[float]] = {}
        for row in per_query.values():
            out.setdefault(row["class"], []).append(row[f"{metric}_{arm}"])
        return {c: sum(v) / len(v) for c, v in out.items()}

    class_ndcg_a = class_mean("ndcg", "a")
    class_ndcg_b = class_mean("ndcg", "b")
    arm_a_mean_s = sum(arm_a_times) / len(arm_a_times)
    decision = cr.evaluate_decision(class_ndcg_a, class_ndcg_b, arm_a_mean_s)

    all_rows = list(per_query.values())
    results = {
        "corpus_docs": len(doc_ids),
        "corpus_pages": len(pages),
        "missing_docs": missing,
        "per_query": per_query,
        "overall_ndcg_temp_fts": sum(r["ndcg_a"] for r in all_rows) / len(all_rows),
        "overall_ndcg_rrf_fusion": sum(r["ndcg_b"] for r in all_rows) / len(all_rows),
        "class_ndcg_temp_fts": class_ndcg_a,
        "class_ndcg_rrf_fusion": class_ndcg_b,
        "dochit3_temp_fts": class_mean("dochit3", "a"),
        "dochit3_rrf_fusion": class_mean("dochit3", "b"),
        "arm_a_mean_query_seconds": round(arm_a_mean_s, 4),
        "arm_b_index_build_seconds": round(arm_b_build_s, 3),
        "decision": decision,
    }
    (OUT_DIR / "results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _write_results_md(results, force=force)
    print(f"decision: {decision['winner']}")
    for r in decision["reasons"]:
        print(f"  - {r}")
    return 0


def _write_results_md(results: dict, force: bool = False) -> None:
    d = results["decision"]
    lines = [
        "# Cross-Doc Keyword Ranking Spike: Results",
        "",
        f"Corpus: {results['corpus_docs']} docs,"
        f" {results['corpus_pages']} pages"
        f" ({len(results['missing_docs'])} manifest docs missing locally).",
        "",
        "## Decision",
        "",
        f"**Winner: {d['winner']}**",
        "",
    ]
    lines += [f"- {r}" for r in d["reasons"]]
    lines += [
        "",
        "## Per-class NDCG@10",
        "",
        "| class | temp-fts (A) | rrf-fusion (B) |",
        "|---|---|---|",
    ]
    for cls in sorted(results["class_ndcg_temp_fts"]):
        a = results["class_ndcg_temp_fts"][cls]
        b = results["class_ndcg_rrf_fusion"].get(cls, 0.0)
        lines.append(f"| {cls} | {a:.3f} | {b:.3f} |")
    lines.append(
        f"| overall | {results['overall_ndcg_temp_fts']:.3f}"
        f" | {results['overall_ndcg_rrf_fusion']:.3f} |"
    )
    lines += [
        "",
        "## Cost",
        "",
        f"- Arm A mean per-query (incl. per-query index build):"
        f" {results['arm_a_mean_query_seconds']}s",
        f"- Arm B one-time index build: {results['arm_b_index_build_seconds']}s"
        " (amortized in production as persistent per-doc indexes)",
        "",
    ]
    write_results_md("\n".join(lines), force=force)


# Everything below the spike's own generated sections is hand-written:
# the interpretation, and later arms appended by other benchmarks (the
# described-query arm lives in this same file). A blind overwrite silently
# destroys all of it, so refuse unless the caller says otherwise.
_GENERATED_MARKER = "# Cross-Doc Keyword Ranking Spike: Results"


def write_results_md(body: str, force: bool = False) -> None:
    """Write RESULTS.md, refusing to clobber hand-written sections."""
    out = OUT_DIR / "RESULTS.md"
    if out.exists() and not force:
        existing = out.read_text(encoding="utf-8")
        generated_len = len(body)
        if not existing.startswith(_GENERATED_MARKER) or len(existing) > generated_len:
            print(
                f"REFUSING to overwrite {out}: it carries "
                f"{len(existing) - generated_len} bytes this script did not "
                "generate (hand-written interpretation and/or later benchmark "
                "arms).\nRe-run with --force to overwrite anyway, or diff the "
                "generated body against the file by hand."
            )
            return
    out.write_text(body, encoding="utf-8")
    print(f"wrote {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-manifest", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite RESULTS.md even if it carries hand-written sections",
    )
    args = ap.parse_args()

    if args.build_manifest:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        manifest = build_manifest()
        (OUT_DIR / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
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
        return run_benchmark(force=args.force)  # Task 4

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
