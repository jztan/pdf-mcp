#!/usr/bin/env python
"""
scripts/benchmark_corpus_modes.py

Retrieval quality of the PRODUCTION `pdf_corpus_search` tool, all three
modes, with REAL embeddings, against the stage-2 graded ground truth
(benchmark_data/corpus_search/queries.json, 64 queries over 100 docs).

This closes the evidence gap left by the stage-2 spike, which compared
keyword-arm DESIGNS on scratch FTS tables: here the actual tool is called
per query (isolated cache, warmed once), so the numbers measure exactly
what an agent receives, semantic and hybrid included.

Sanity cross-check: the keyword mode reuses the design the spike selected
(per-doc doc-local BM25 + RRF fusion), so its overall NDCG@10 should land
near the spike's arm-B result (~0.547 at the 100-doc re-run). A large
deviation means a wiring bug, not a quality change.

Run:  uv run python scripts/benchmark_corpus_modes.py
Writes benchmark_data/corpus_search/modes_results.{json,md}. Exits 0
(informational; the committed md is the record).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _retrieval_metrics as rm  # noqa: E402

DEFAULT_DATA = REPO / "benchmark_data" / "corpus_search"
TOP_K = 10
MODES = ("keyword", "semantic", "auto")


def class_names(queries: dict) -> list[str]:
    """Query classes present in the dataset, sorted. Data-driven replacement
    for the hardcoded (needle, spread, trap) tuple."""
    return sorted({q["class"] for q in queries["queries"]})


def nonlatin_ids(manifest: dict) -> set[str]:
    """Doc ids whose lang is not English. The English-only embedding model is
    expected to underperform on these, so they are reported as a subset."""
    return {d["id"] for d in manifest["docs"] if d.get("lang", "en") != "en"}


def agg(rows: dict, key=lambda r: True) -> dict[str, float]:
    sel = [r for r in rows.values() if key(r)]
    if not sel:
        return {"ndcg": 0.0, "doc_ndcg": 0.0, "dochit3": 0.0, "n": 0}
    paged = [r for r in sel if r["ndcg"] is not None]
    return {
        # None, not 0.0, when nothing in the selection carries page labels.
        # The route class has no page-level score to report, and printing
        # 0.000 there reads as "scored zero" rather than "not applicable".
        "ndcg": (
            round(sum(r["ndcg"] for r in paged) / len(paged), 4) if paged else None
        ),
        "doc_ndcg": round(sum(r["doc_ndcg"] for r in sel) / len(sel), 4),
        "dochit3": round(sum(r["dochit3"] for r in sel) / len(sel), 4),
        "n": len(sel),
    }


def fmt(value: float | None) -> str:
    """Format an NDCG cell; page-level score is n/a for page-less classes."""
    return "n/a" if value is None else f"{value:.3f}"


def grade_query(query: dict, ranked: list[tuple[str, int]], top_k: int) -> dict:
    """Grade one query's ranked (doc_id, page) results.

    Labels carrying a `page` grade page-level NDCG. Labels without one
    (the route class: "which document answers this?") grade only at doc
    level, and the query's page-level NDCG is None so it is excluded from
    the page-level mean rather than counted as a zero.
    """
    page_labels = {
        (lb["doc"], lb["page"]): float(lb["gain"])
        for lb in query["labels"]
        if "page" in lb
    }
    doc_gains: dict[str, float] = {}
    for lb in query["labels"]:
        doc_gains[lb["doc"]] = max(doc_gains.get(lb["doc"], 0.0), float(lb["gain"]))
    gold_docs = {d for d, g in doc_gains.items() if g >= 2}

    ndcg = None
    if page_labels:
        gains = [page_labels.get(item, 0.0) for item in ranked]
        ndcg = rm.ndcg_at_k(gains, list(page_labels.values()), top_k)

    # Doc-level NDCG: dedupe ranked docs in rank order, gain = the doc's
    # best labeled gain. Separates "wrong doc" from "right doc, unlabeled
    # page" -- page-level NDCG floors on label sparsity when a gold doc
    # matches the query on many pages.
    seen: set[str] = set()
    doc_ranked_gains = []
    for doc_id, _page in ranked:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        doc_ranked_gains.append(doc_gains.get(doc_id, 0.0))

    return {
        "ndcg": ndcg,
        "doc_ndcg": rm.ndcg_at_k(doc_ranked_gains, list(doc_gains.values()), top_k),
        "dochit3": int(bool({d for d, _p in ranked[:3]} & gold_docs)),
    }


def single_doc_queries(queries: dict) -> list[tuple[str, str]]:
    """(query_id, doc_id) pairs eligible for the single-doc arm: queries with
    page-level labels concentrated in exactly one gold doc. Multi-doc spread
    queries have no single 'the' document to search, and route queries carry
    no page labels to grade against."""
    out: list[tuple[str, str]] = []
    for q in queries["queries"]:
        paged = [lb for lb in q["labels"] if "page" in lb]
        if not paged:
            continue
        docs = {lb["doc"] for lb in paged}
        if len(docs) == 1:
            out.append((q["id"], docs.pop()))
    return out


_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Whitespace-collapse + casefold for evidence matching."""
    return _WS.sub(" ", text).strip().casefold()


def validate_queries(manifest: dict, queries: dict, page_text_lookup) -> list[str]:
    """Check every label: the doc exists in the manifest, and (for labels
    carrying a page) the evidence substring is present in that page's
    extracted text. page_text_lookup(doc_id, page_1indexed) -> str.
    Returns a list of human-readable error strings."""
    known = {d["id"] for d in manifest["docs"]}
    errors: list[str] = []
    for q in queries["queries"]:
        for label in q["labels"]:
            if label["doc"] not in known:
                errors.append(f"{q['id']}: unknown doc {label['doc']}")
                continue
            if "page" not in label:
                continue
            text = page_text_lookup(label["doc"], label["page"])
            if normalize(label["evidence"]) not in normalize(text):
                errors.append(
                    f"{q['id']}: evidence not found on"
                    f" {label['doc']} p{label['page']}:"
                    f" {label['evidence']!r}"
                )
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA,
        help="dataset directory holding manifest.json and queries.json",
    )
    ap.add_argument(
        "--validate",
        action="store_true",
        help="check ground-truth labels against extracted page text, then exit",
    )
    ap.add_argument(
        "--single-doc-arm",
        action="store_true",
        help="also measure per-document pdf_search on single-gold-doc queries",
    )
    args = ap.parse_args(argv)
    data = args.data_dir if args.data_dir.is_absolute() else REPO / args.data_dir

    import pdf_mcp.server as server_module
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search, pdf_corpus_warm

    manifest = json.loads((data / "manifest.json").read_text())
    queries = json.loads((data / "queries.json").read_text())
    classes = class_names(queries)
    subset_ids = nonlatin_ids(manifest)
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}

    if args.validate:
        import pymupdf

        from pdf_mcp.extractor import extract_text_from_page

        docs_by_id = {d["id"]: REPO / d["path"] for d in manifest["docs"]}
        text_cache: dict[str, list[str]] = {}

        def lookup(doc_id: str, page: int) -> str:
            if doc_id not in text_cache:
                doc = pymupdf.open(str(docs_by_id[doc_id]))
                text_cache[doc_id] = [
                    extract_text_from_page(doc[i], sort_by_position=True)
                    for i in range(len(doc))
                ]
                doc.close()
            pages = text_cache[doc_id]
            return pages[page - 1] if 1 <= page <= len(pages) else ""

        errors = validate_queries(manifest, queries, lookup)
        for err in errors:
            print(f"INVALID {err}")
        print(f"\n{len(queries['queries'])} queries checked, {len(errors)} errors")
        return 1 if errors else 0

    paths = [p for p in id_by_path if Path(p).exists()]
    if len(paths) != len(manifest["docs"]):
        print(
            f"WARNING: {len(manifest['docs']) - len(paths)} manifest docs "
            "missing locally; proceeding with the rest"
        )
    if not paths:
        print("ERROR: no corpus docs available")
        return 2

    single: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        prev_cache = server_module.cache
        server_module.cache = PDFCache(cache_dir=Path(tmp), ttl_hours=24)
        try:
            t0 = time.perf_counter()
            warm = pdf_corpus_warm(paths, budget_seconds=300, embeddings=True)
            while warm.get("unprocessed"):
                warm = pdf_corpus_warm(paths, budget_seconds=300, embeddings=True)
            warm_s = time.perf_counter() - t0
            print(
                f"warmed {len(paths)} docs (text+embeddings) in {warm_s:.0f}s"
                f" ({len(warm.get('skipped', []))} skipped)"
            )

            per_mode: dict[str, dict] = {}
            for mode in MODES:
                rows: dict[str, dict] = {}
                times: list[float] = []
                reported_mode: set[str] = set()
                for q in queries["queries"]:
                    tq = time.perf_counter()
                    res = pdf_corpus_search(paths, q["query"], mode=mode, top_k=TOP_K)
                    times.append(time.perf_counter() - tq)
                    if "error" in res:
                        print(f"ERROR {mode} {q['id']}: {res['error']}")
                        return 2
                    reported_mode.add(res["search_mode"])
                    if res["coverage"]["searched"] != len(paths):
                        print(
                            f"ERROR {mode} {q['id']}: partial coverage "
                            f"{res['coverage']}"
                        )
                        return 2
                    ranked = [
                        (id_by_path[m["path"]], m["page"]) for m in res["matches"]
                    ]
                    graded = grade_query(q, ranked, TOP_K)
                    gold_docs = {
                        lb["doc"] for lb in q["labels"] if float(lb["gain"]) >= 2
                    }
                    rows[q["id"]] = {
                        "class": q["class"],
                        "cjk": any(d in subset_ids for d in gold_docs),
                        "ndcg": graded["ndcg"],
                        "doc_ndcg": graded["doc_ndcg"],
                        "dochit3": graded["dochit3"],
                        "seconds": round(times[-1], 3),
                    }
                per_mode[mode] = {
                    "search_mode_reported": sorted(reported_mode),
                    "per_query": rows,
                    "mean_query_seconds": round(sum(times) / len(times), 3),
                }
                print(
                    f"{mode}: done ({per_mode[mode]['mean_query_seconds']}s"
                    f"/query, reported={sorted(reported_mode)})"
                )

            if args.single_doc_arm:
                from pdf_mcp.server import pdf_search

                path_by_id = {v: k for k, v in id_by_path.items()}
                query_by_id = {q["id"]: q for q in queries["queries"]}
                eligible = [
                    (qid, doc)
                    for qid, doc in single_doc_queries(queries)
                    if path_by_id.get(doc) in paths
                ]
                for mode in MODES:
                    scores = []
                    for qid, doc_id in eligible:
                        q = query_by_id[qid]
                        res = pdf_search(
                            path_by_id[doc_id],
                            q["query"],
                            mode=mode,
                            max_results=TOP_K,
                        )
                        if "error" in res:
                            print(f"ERROR single-doc {mode} {qid}: {res['error']}")
                            return 2
                        ranked = [(doc_id, m["page"]) for m in res["matches"]]
                        scores.append(grade_query(q, ranked, TOP_K)["ndcg"])
                    single[mode] = {
                        "ndcg": (
                            round(sum(scores) / len(scores), 4) if scores else 0.0
                        ),
                        "n": len(scores),
                    }
                    print(f"single-doc {mode}: NDCG@10 {single[mode]['ndcg']:.3f}")
        finally:
            server_module.cache = prev_cache

    summary: dict[str, dict] = {}
    for mode in MODES:
        rows = per_mode[mode]["per_query"]
        summary[mode] = {
            "overall": agg(rows),
            "by_class": {c: agg(rows, lambda r, c=c: r["class"] == c) for c in classes},
            "cjk_subset": agg(rows, lambda r: r["cjk"]),
            "non_cjk": agg(rows, lambda r: not r["cjk"]),
            "mean_query_seconds": per_mode[mode]["mean_query_seconds"],
            "search_mode_reported": per_mode[mode]["search_mode_reported"],
        }

    out = {
        "corpus_docs": len(paths),
        "top_k": TOP_K,
        "queries": len(queries["queries"]),
        "summary": summary,
        "per_query": {m: per_mode[m]["per_query"] for m in MODES},
    }
    (data / "modes_results.json").write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n"
    )

    dataset_note = manifest.get("results_note", "graded ground truth, stage-2")
    sanity_note = manifest.get(
        "sanity_note",
        "Sanity cross-check: keyword overall should land near the stage-2"
        " arm-B result (~0.547). Interpretation is appended by hand after"
        " the run.",
    )
    class_header = " | ".join(classes)
    lines = [
        "# pdf_corpus_search mode benchmark (production tool, real embeddings)",
        "",
        f"Corpus: {len(paths)} docs. Queries: {len(queries['queries'])}"
        f" ({dataset_note}). top_k={TOP_K}. The tool itself is"
        " called per query on a warmed isolated cache, so numbers measure the"
        " agent-facing contract end to end.",
        "",
        f"| mode | overall NDCG@10 | {class_header} | doc-hit@3 | s/query |",
        "|---|---|" + "---|" * len(classes) + "---|---|",
    ]
    for mode in MODES:
        s = summary[mode]
        cells = "".join(f" {fmt(s['by_class'][c]['ndcg'])} |" for c in classes)
        lines.append(
            f"| {mode} ({'/'.join(s['search_mode_reported'])}) |"
            f" {fmt(s['overall']['ndcg'])} |"
            f"{cells}"
            f" {s['overall']['dochit3']:.3f} |"
            f" {s['mean_query_seconds']:.2f} |"
        )
    lines += [
        "",
        "## Doc-level NDCG@10 (ranked docs deduped, gain = doc's best label)",
        "",
        'Separates "wrong doc" from "right doc, unlabeled page": sparse page'
        " labels grade only a few (doc, page) pairs while a gold doc matches"
        " the query on many pages, so page-level NDCG floors on label"
        " sparsity. Doc-level is the honest ceiling-side read wherever gold"
        " docs match on many more pages than are labeled.",
        "",
        f"| mode | overall | {class_header} |",
        "|---|---|" + "---|" * len(classes),
    ]
    for mode in MODES:
        s = summary[mode]
        cells = "".join(f" {s['by_class'][c]['doc_ndcg']:.3f} |" for c in classes)
        lines.append(f"| {mode} | {s['overall']['doc_ndcg']:.3f} |{cells}")
    if subset_ids:
        lines += [
            "",
            "## CJK subset (5 needle queries on Japanese docs; embedding model is"
            " English bge-small, so the semantic arm is expected to be weak"
            " there)",
            "",
            "| mode | CJK NDCG@10 (n=5) | non-CJK NDCG@10 |",
            "|---|---|---|",
        ]
        for mode in MODES:
            s = summary[mode]
            lines.append(
                f"| {mode} | {s['cjk_subset']['ndcg']:.3f} |"
                f" {s['non_cjk']['ndcg']:.3f} |"
            )
    lines += ["", sanity_note, ""] if sanity_note else ["", ""]
    # Preserve the hand-appended interpretation section across reruns.
    md_path = data / "modes_results.md"
    interp = ""
    if md_path.exists():
        prev = md_path.read_text()
        idx = prev.find("## Interpretation")
        if idx != -1:
            interp = prev[idx:]
    md_path.write_text("\n".join(lines) + interp)
    print("\nwrote modes_results.{json,md}")
    for mode in MODES:
        s = summary[mode]
        per_class = " ".join(f"{c}={fmt(s['by_class'][c]['ndcg'])}" for c in classes)
        print(
            f"  {mode:8s} overall={fmt(s['overall']['ndcg'])} "
            f"{per_class} "
            f"dochit3={s['overall']['dochit3']:.3f} "
            f"doc_ndcg={s['overall']['doc_ndcg']:.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
