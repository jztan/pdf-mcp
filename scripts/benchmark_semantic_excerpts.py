#!/usr/bin/env python
"""
scripts/benchmark_semantic_excerpts.py

Ratchet for excerpt quality on PURE-SEMANTIC hits.

Both committed excerpt gates are blind to this path: the excerpt gate runs
`pdf_search` in auto mode and no graded query lands on a semantic-only page,
and the Bedrock anchor runs corpus hybrid mode. On the 2026-09-05 snippet
anchoring change both read byte-identical before and after while the real
path moved single-doc containment 0.159 -> 0.537. This script measures that
path directly and fails when a class drops against the committed baseline.

Arms (both `mode="semantic"`, excerpt_style snippet and paragraph):
  corpus : pdf_corpus_search over the 184-query graded corpus
           (benchmark_data/corpus_search), span recall of the gold evidence
           inside the first 2,000 tokens of returned excerpts (same scoring
           as scripts/benchmark_bedrock_kb.py).
  single : pdf_search over the excerpt-gate corpus
           (benchmark_data/excerpt_quality_queries.json), answer containment
           in the excerpt of the graded page (same scoring as
           scripts/benchmark_excerpt_quality.py, same sha256 / page checks).

Gate: for every (arm, style, class) the paired bootstrap CI of
current-minus-baseline must not lie entirely below zero. A single flipped
query cannot trip it; a class-wide drop does. Retrieval invariants
(corpus doc-NDCG@10, single-doc graded-page hits) are reported, and a shift
there is flagged as a confound, because the excerpt comparison assumes the
same pages were retrieved.

Usage:
    uv run python scripts/benchmark_semantic_excerpts.py                # gated run
    uv run python scripts/benchmark_semantic_excerpts.py --calibrate    # report only
    uv run python scripts/benchmark_semantic_excerpts.py --update-baseline
    uv run python scripts/benchmark_semantic_excerpts.py --arms single --limit 10

Every run persists benchmark_data/semantic_excerpts_results.{json,md}.
`--update-baseline` also rewrites benchmark_data/semantic_excerpts_baseline.json;
do that deliberately, after a change whose improvement the report shows with a
CI excluding zero, never to make a red gate green.

Exit codes: 0 = PASS / calibrate, 1 = FAIL, 2 = setup error (missing
baseline, query-id mismatch, incomplete warm, corpus integrity).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

from benchmark_bedrock_kb import (  # noqa: E402
    BUDGET_TOKENS,
    bootstrap_diff_ci,
    cap_to_budget,
    grade_containment,
    matches_to_units,
    warm_corpus,
)
from benchmark_corpus_modes import build_ranked, grade_query  # noqa: E402
from benchmark_excerpt_quality import (  # noqa: E402
    _assert_answer_on_page,
    _assert_sha256,
    _resolve_pdf_path,
    load_queries,
)

ARMS = ("corpus", "single")
STYLES = ("snippet", "paragraph")
#: The per-query quality metric each arm is gated on.
METRIC = {"corpus": "span_recall", "single": "contains"}
CORPUS_TOP_K = 25
SINGLE_MAX_RESULTS = 5

DEFAULT_CORPUS_DATA = REPO / "benchmark_data" / "corpus_search"
DEFAULT_SINGLE_QUERIES = REPO / "benchmark_data" / "excerpt_quality_queries.json"
DEFAULT_BASELINE = REPO / "benchmark_data" / "semantic_excerpts_baseline.json"
DEFAULT_OUT_JSON = REPO / "benchmark_data" / "semantic_excerpts_results.json"
DEFAULT_OUT_MD = REPO / "benchmark_data" / "semantic_excerpts_results.md"


def _digest(text: str) -> str:
    """Short digest of an excerpt, so runs can count changed excerpts without
    committing the excerpt text itself into the baseline."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Arms
# --------------------------------------------------------------------------


def corpus_arm(data_dir: Path, limit: int | None = None) -> dict:
    """Pure-semantic corpus search, snippet and paragraph, span recall."""
    from pdf_mcp.server import pdf_corpus_search

    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    queries = json.loads((data_dir / "queries.json").read_text(encoding="utf-8"))[
        "queries"
    ]
    if limit:
        queries = queries[:limit]
    id_by_path = {str((REPO / d["path"]).resolve()): d["id"] for d in manifest["docs"]}
    paths = list(id_by_path)

    t0 = time.perf_counter()
    warm = warm_corpus(paths)
    if not warm.get("warm_complete") or warm.get("unprocessed"):
        raise RuntimeError(
            "corpus warm incomplete: "
            f"unprocessed={warm.get('unprocessed')} skipped={warm.get('skipped')}. "
            "Refusing to score a partial corpus."
        )
    print(f"  warmed {len(paths)} docs in {time.perf_counter() - t0:.0f}s", flush=True)

    out: dict = {"rows": {}, "seconds_per_query": {}}
    for style in STYLES:
        rows: dict[str, dict] = {}
        t0 = time.perf_counter()
        for q in queries:
            res = pdf_corpus_search(
                paths,
                q["query"],
                mode="semantic",
                top_k=CORPUS_TOP_K,
                excerpt_style=style,
            )
            if "error" in res:
                raise RuntimeError(f"{q['id']}: {res['error']}")
            if res["coverage"]["searched"] != len(paths):
                raise RuntimeError(f"{q['id']}: partial coverage {res['coverage']}")
            units = matches_to_units(res["matches"], id_by_path)
            kept, realized_k = cap_to_budget(units, BUDGET_TOKENS)
            graded = grade_query(q, build_ranked(res["matches"], id_by_path), 10)
            rows[q["id"]] = {
                "class": q["class"],
                "span_recall": grade_containment(q, kept)["span_recall"],
                "doc_ndcg": graded["doc_ndcg"],
                "realized_k": realized_k,
                "excerpt_digest": _digest("\n".join(t for _d, _p, t in kept)),
            }
        elapsed = time.perf_counter() - t0
        out["rows"][style] = rows
        out["seconds_per_query"][style] = round(elapsed / max(1, len(rows)), 3)
        print(f"  corpus/{style}: {len(rows)} q in {elapsed:.0f}s", flush=True)
    return out


def single_arm(queries_path: Path, limit: int | None = None) -> dict:
    """Pure-semantic single-doc search, snippet and paragraph, containment on
    the graded page. Runs the excerpt gate's corpus-integrity checks first
    (sha256, page in range, answer on page) so a drifted PDF exits 2 instead
    of reading as a quality drop."""
    import pymupdf

    from pdf_mcp.server import _resolve_path, pdf_search

    all_pdfs = load_queries(str(queries_path))
    plan: list[tuple[str, str, dict]] = []
    for pdf_key, pdf_data in all_pdfs.items():
        pdf_path = _resolve_pdf_path(pdf_data)
        local_path, err = _resolve_path(pdf_path)
        if local_path is None:
            raise FileNotFoundError(
                f"Corpus PDF '{pdf_key}' could not be resolved: {pdf_path} ({err})"
            )
        if pdf_data.get("sha256"):
            _assert_sha256(local_path, pdf_key, pdf_data["sha256"])
        doc = pymupdf.open(local_path)
        try:
            for q in pdf_data["queries"]:
                _assert_answer_on_page(doc, pdf_key, q)
                plan.append((pdf_key, pdf_path, q))
        finally:
            doc.close()
    if limit:
        plan = plan[:limit]

    out: dict = {"rows": {}, "seconds_per_query": {}}
    for style in STYLES:
        rows: dict[str, dict] = {}
        t0 = time.perf_counter()
        for _pdf_key, pdf_path, q in plan:
            r = pdf_search(
                pdf_path,
                q["query"],
                mode="semantic",
                excerpt_style=style,
                max_results=SINGLE_MAX_RESULTS,
            )
            if "error" in r:
                raise RuntimeError(f"{q['id']}: {r['error']}")
            target = next(
                (m for m in r.get("matches", []) if m["page"] == q["page"]), None
            )
            excerpt = target["excerpt"] if target else ""
            rows[q["id"]] = {
                "class": q["category"],
                "page_hit": int(target is not None),
                "contains": int(
                    bool(target) and q["answer"].lower() in excerpt.lower()
                ),
                "excerpt_digest": _digest(excerpt),
            }
        elapsed = time.perf_counter() - t0
        out["rows"][style] = rows
        out["seconds_per_query"][style] = round(elapsed / max(1, len(rows)), 3)
        print(f"  single/{style}: {len(rows)} q in {elapsed:.0f}s", flush=True)
    return out


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # pragma: no cover - git absent
        return "unknown"


def run_all(
    arms: tuple[str, ...],
    corpus_data: Path,
    single_queries: Path,
    limit: int | None,
) -> dict:
    run: dict = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "config": {
            "mode": "semantic",
            "styles": list(STYLES),
            "corpus_top_k": CORPUS_TOP_K,
            "corpus_budget_tokens": BUDGET_TOKENS,
            "single_max_results": SINGLE_MAX_RESULTS,
            "limit": limit,
        },
        "arms": {},
    }
    if "corpus" in arms:
        print("corpus arm (pdf_corpus_search, mode=semantic)", flush=True)
        run["arms"]["corpus"] = corpus_arm(corpus_data, limit)
    if "single" in arms:
        print("single arm (pdf_search, mode=semantic)", flush=True)
        run["arms"]["single"] = single_arm(single_queries, limit)
    return run


# --------------------------------------------------------------------------
# Scoring, gate, report (pure logic; tested)
# --------------------------------------------------------------------------


def _classes(rows: dict[str, dict]) -> list[tuple[str, list[str]]]:
    """[('all', ids), (class, ids), ...] with ids sorted for determinism."""
    by_class: dict[str, list[str]] = defaultdict(list)
    for qid in sorted(rows):
        by_class[rows[qid]["class"]].append(qid)
    return [("all", sorted(rows))] + sorted(by_class.items())


def summarize(run: dict) -> dict:
    """Per (arm, style, class) mean of the arm's metric, plus retrieval
    invariants and per-query latency."""
    out: dict = {}
    for arm, data in run["arms"].items():
        metric = METRIC[arm]
        out[arm] = {"styles": {}, "seconds_per_query": data.get("seconds_per_query")}
        for style, rows in data["rows"].items():
            cells = {}
            for cls, ids in _classes(rows):
                vals = [rows[q][metric] for q in ids]
                cells[cls] = {"n": len(ids), "mean": round(sum(vals) / len(vals), 4)}
            entry: dict = {"metric": metric, "classes": cells}
            if arm == "corpus":
                ids = sorted(rows)
                entry["doc_ndcg"] = round(
                    sum(rows[q]["doc_ndcg"] for q in ids) / len(ids), 4
                )
            if arm == "single":
                entry["page_hits"] = sum(r["page_hit"] for r in rows.values())
            out[arm]["styles"][style] = entry
    return out


def evaluate_ratchet(baseline: dict, current: dict) -> dict:
    """Compare `current` against `baseline` arm by arm.

    Clause 1 (gated): no (arm, style, class) whose current-minus-baseline
        CI lies entirely below zero.
    Clause 2 (setup, not quality): the query ids per (arm, style) match;
        a mismatch means the corpora differ and nothing is comparable.
    Retrieval invariants are reported: doc-NDCG (corpus) and graded-page
        hits (single). A shift there does not fail the gate but is flagged,
        since it confounds the excerpt comparison.
    Improvements whose CI excludes zero are listed so the baseline can be
        raised deliberately with --update-baseline.
    """
    verdict: dict = {
        "pass": True,
        "regressions": [],
        "improvements": [],
        "confounds": [],
        "id_mismatch": [],
        "cells": [],
    }
    for arm in ARMS:
        if arm not in current["arms"]:
            continue
        if arm not in baseline["arms"]:
            verdict["id_mismatch"].append(f"{arm}: absent from baseline")
            continue
        metric = METRIC[arm]
        for style in STYLES:
            b_rows = baseline["arms"][arm]["rows"].get(style, {})
            c_rows = current["arms"][arm]["rows"].get(style, {})
            if sorted(b_rows) != sorted(c_rows):
                verdict["id_mismatch"].append(
                    f"{arm}/{style}: baseline {len(b_rows)} ids, current {len(c_rows)}"
                )
                continue
            changed = sum(
                1
                for q in c_rows
                if c_rows[q]["excerpt_digest"] != b_rows[q]["excerpt_digest"]
            )
            for cls, ids in _classes(c_rows):
                b = [float(b_rows[q][metric]) for q in ids]
                c = [float(c_rows[q][metric]) for q in ids]
                ci = bootstrap_diff_ci(c, b)
                cell = {
                    "arm": arm,
                    "style": style,
                    "class": cls,
                    "n": len(ids),
                    "baseline": round(sum(b) / len(b), 4),
                    "current": round(sum(c) / len(c), 4),
                    "changed_excerpts": changed if cls == "all" else None,
                    **ci,
                }
                verdict["cells"].append(cell)
                key = f"{arm}/{style}/{cls}"
                if ci["mean_diff"] < 0 and not ci["includes_zero"]:
                    verdict["regressions"].append(key)
                elif ci["mean_diff"] > 0 and not ci["includes_zero"]:
                    verdict["improvements"].append(key)
            ids = sorted(c_rows)
            if arm == "corpus":
                b_inv = [float(b_rows[q]["doc_ndcg"]) for q in ids]
                c_inv = [float(c_rows[q]["doc_ndcg"]) for q in ids]
                label = "doc_ndcg"
            else:
                b_inv = [float(b_rows[q]["page_hit"]) for q in ids]
                c_inv = [float(c_rows[q]["page_hit"]) for q in ids]
                label = "page_hit"
            inv = bootstrap_diff_ci(c_inv, b_inv)
            if not inv["includes_zero"]:
                verdict["confounds"].append(
                    f"{arm}/{style}: {label} moved {inv['mean_diff']:+.3f}"
                    f" [{inv['lo']:+.3f}, {inv['hi']:+.3f}]; retrieval changed,"
                    " so the excerpt deltas above are confounded"
                )
    verdict["pass"] = not verdict["regressions"] and not verdict["id_mismatch"]
    return verdict


def _fmt_ci(cell: dict) -> str:
    tag = "includes zero" if cell["includes_zero"] else "excludes zero"
    return f"{cell['mean_diff']:+.3f} [{cell['lo']:+.3f}, {cell['hi']:+.3f}] ({tag})"


def render_markdown(run: dict, summary: dict, verdict: dict | None) -> str:
    lines = [
        "# Pure-semantic excerpt quality",
        "",
        f"Generated {run['generated']} at `{run['git_head']}` by"
        " `scripts/benchmark_semantic_excerpts.py`. Both arms run"
        ' `mode="semantic"`; corpus scores span recall of the gold evidence'
        f" within {run['config']['corpus_budget_tokens']:,} tokens"
        f" (top_k={run['config']['corpus_top_k']}), single-doc scores answer"
        " containment in the graded page's excerpt"
        f" (max_results={run['config']['single_max_results']}).",
        "",
    ]
    if run["config"].get("limit"):
        lines += [f"**Pilot run: first {run['config']['limit']} queries only.**", ""]
    for arm, s in summary.items():
        lines.append(f"## {arm} (metric: {METRIC[arm]})")
        lines.append("")
        classes = list(next(iter(s["styles"].values()))["classes"])
        lines.append("| style | " + " | ".join(classes) + " | retrieval | s/query |")
        lines.append("|---|" + "---|" * (len(classes) + 2))
        for style, entry in s["styles"].items():
            cells = [
                f"{entry['classes'][c]['mean']:.3f} (n={entry['classes'][c]['n']})"
                for c in classes
            ]
            inv = (
                f"doc-NDCG@10 {entry['doc_ndcg']:.3f}"
                if arm == "corpus"
                else f"graded page hit {entry['page_hits']}"
            )
            spq = (s.get("seconds_per_query") or {}).get(style)
            spq_s = f"{spq:.2f}" if spq is not None else "n/a"
            lines.append(f"| {style} | " + " | ".join(cells) + f" | {inv} | {spq_s} |")
        lines.append("")
    if verdict is not None:
        lines.append("## Gate: " + ("PASS" if verdict["pass"] else "FAIL"))
        lines.append("")
        lines.append(
            "Current minus baseline, paired bootstrap 95% CI. A class fails"
            " only when its CI lies entirely below zero."
        )
        lines.append("")
        lines.append(
            "| arm | style | class | n | baseline | current | current minus baseline |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for c in verdict["cells"]:
            lines.append(
                f"| {c['arm']} | {c['style']} | {c['class']} | {c['n']} |"
                f" {c['baseline']:.3f} | {c['current']:.3f} | {_fmt_ci(c)} |"
            )
        lines.append("")
        changed = [
            f"{c['arm']}/{c['style']}: {c['changed_excerpts']}/{c['n']}"
            for c in verdict["cells"]
            if c["class"] == "all"
        ]
        if changed:
            lines.append("Queries with a different excerpt: " + ", ".join(changed))
            lines.append("")
        for label, items in (
            ("Regressions", verdict["regressions"]),
            ("Improvements (raise the baseline deliberately)", verdict["improvements"]),
            ("Confounds", verdict["confounds"]),
            ("Query-id mismatch", verdict["id_mismatch"]),
        ):
            if items:
                lines.append(f"**{label}:**")
                lines += [f"- {i}" for i in items]
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def print_gate_verdict(verdict: dict) -> None:
    print()
    print("=" * 60)
    print(f"GATE VERDICT: {'PASS' if verdict['pass'] else 'FAIL'}")
    print("=" * 60)
    for c in verdict["cells"]:
        mark = (
            "✗"
            if f"{c['arm']}/{c['style']}/{c['class']}" in verdict["regressions"]
            else "✓"
        )
        print(
            f"  {mark} {c['arm']}/{c['style']}/{c['class']:<11} n={c['n']:<4}"
            f" {c['baseline']:.3f} -> {c['current']:.3f}  {_fmt_ci(c)}"
        )
    for label, items in (
        ("regressions", verdict["regressions"]),
        ("improvements", verdict["improvements"]),
        ("confounds", verdict["confounds"]),
        ("id mismatch", verdict["id_mismatch"]),
    ):
        if items:
            print(f"  {label}: " + "; ".join(items))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pure-semantic excerpt quality ratchet (snippet and paragraph)"
    )
    p.add_argument("--calibrate", action="store_true", help="Report only, no gate.")
    p.add_argument(
        "--update-baseline",
        action="store_true",
        help="Write this run as the new baseline (after reviewing the report).",
    )
    p.add_argument(
        "--arms", default=",".join(ARMS), help="Comma list of arms (default: both)."
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Pilot: first N queries per arm (implies --calibrate, no persist).",
    )
    p.add_argument("--corpus-data", type=Path, default=DEFAULT_CORPUS_DATA)
    p.add_argument("--single-queries", type=Path, default=DEFAULT_SINGLE_QUERIES)
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.add_argument("--output-json", type=Path, default=DEFAULT_OUT_JSON)
    p.add_argument("--output-md", type=Path, default=DEFAULT_OUT_MD)
    return p


def main(
    argv: list[str] | None = None,
    runner: Callable[..., dict] = run_all,
) -> int:
    """Returns exit code: 0 PASS / calibrate, 1 FAIL, 2 setup error."""
    args = _build_parser().parse_args(argv)
    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    bad = [a for a in arms if a not in ARMS]
    if bad or not arms:
        print(f"ERROR: unknown arms {bad or arms}; choose from {ARMS}", file=sys.stderr)
        return 2

    baseline = None
    if not args.calibrate and not args.limit:
        if args.baseline.exists():
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        elif not args.update_baseline:
            print(
                f"ERROR: no baseline at {args.baseline}. Run with"
                " --update-baseline once to create it, or --calibrate.",
                file=sys.stderr,
            )
            return 2

    try:
        run = runner(arms, args.corpus_data, args.single_queries, args.limit)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    summary = summarize(run)
    verdict = evaluate_ratchet(baseline, run) if baseline is not None else None
    report = render_markdown(run, summary, verdict)
    print()
    print(report)

    if args.limit:
        print("[--limit] pilot run: nothing persisted, no gate.")
        return 0

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(run, indent=1), encoding="utf-8")
    args.output_md.write_text(report, encoding="utf-8")
    print(f"wrote {args.output_json} and {args.output_md}")

    if verdict is not None:
        print_gate_verdict(verdict)
        if verdict["id_mismatch"]:
            print(
                "ERROR: query ids differ from the baseline; the corpora are not"
                " comparable. Re-create the baseline deliberately.",
                file=sys.stderr,
            )
            return 2
    elif args.calibrate:
        print("\n[--calibrate] Skipping gate. No exit-code gating.")

    if args.update_baseline:
        # A ratchet only tightens. Refuse to overwrite the baseline with a run
        # that regresses it; lowering the bar means deleting the file on purpose.
        if verdict is not None and verdict["regressions"]:
            print(
                "ERROR: --update-baseline refused: this run regresses"
                f" {verdict['regressions']}. Delete the baseline file if the"
                " lower bar is intended.",
                file=sys.stderr,
            )
            return 1
        args.baseline.write_text(json.dumps(run, indent=1), encoding="utf-8")
        print(f"wrote baseline {args.baseline}")

    if verdict is None:
        return 0
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
