"""Anchor benchmark: pdf-mcp corpus search vs Bedrock Knowledge Bases.

Scores every arm by evidence-span containment at an equal token budget,
per query class, with bootstrap CIs. Bedrock is an anchor, not a subject:
any result is acceptable.

Arms: P (pdf_corpus_search, hybrid), B0 (Bedrock default), B1 (Bedrock
fixed-1000 + Cohere Rerank 3.5). B2 and N are optional and not built here.

The default run is offline: it reuses the stored B0/B1 rows from
benchmark_data/bedrock_kb/results.json (Bedrock retrieval measured
byte-identical across runs) and re-runs only the local arms, never importing
boto3. It refuses with exit 2 if the stored arm-config hash, manifest hash,
query-id set or scoring budget differ from the current run, or if no stored
rows exist yet.

    python scripts/benchmark_bedrock_kb.py --out-dir /tmp/rerun

The script warms the corpus into the active cache first (idempotent: seconds
when already warm, minutes once when cold) and refuses to score a partial
corpus.

Pass --live to re-query Bedrock (about $0.20, needs AWS credentials); only
needed after a Bedrock-side change. --reuse-bedrock-from PATH reuses rows
from a different results.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
from typing import Any
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

DEFAULT_DATA = REPO / "benchmark_data" / "corpus_search"
OUT_DIR = REPO / "benchmark_data" / "bedrock_kb"


def provenance_path(path: Path) -> str:
    """Path recorded in results.json for a reused-rows source.

    Repo-relative when the file lies inside the repo, so the committed
    artifact never carries a machine-local checkout path; absolute otherwise.
    """
    try:
        return str(Path(path).resolve().relative_to(REPO.resolve()))
    except ValueError:
        return str(path)


BUDGET_TOKENS = 2000
TOKEN_CHARS = 4  # repo convention: ~4 chars per token
BEDROCK_FILE_LIMIT = 50 * 1024 * 1024  # Bedrock KB per-document quota


def check_corpus_quota(
    manifest: dict, repo: Path, limit_bytes: int = BEDROCK_FILE_LIMIT
) -> list[str]:
    """Return one message per manifest file that Bedrock would refuse.

    Bedrock silently skips an over-quota file at ingest. That document would
    then exist in arm P but not in B0/B1 and the gap would read as a
    retrieval failure, so this is asserted before any AWS call.
    """
    errors: list[str] = []
    for d in manifest["docs"]:
        path = repo / d["path"]
        if not path.exists():
            errors.append(f"{d['id']}: missing at {d['path']}")
            continue
        size = path.stat().st_size
        if size > limit_bytes:
            errors.append(
                f"{d['id']}: {size} bytes exceeds Bedrock limit {limit_bytes}"
            )
    return errors


Unit = tuple[str, int | None, str]  # (doc_id, 1-indexed page or None, text)


def estimate_tokens(text: str) -> int:
    return len(text) // TOKEN_CHARS


def cap_to_budget(units: list[Unit], budget_tokens: int) -> tuple[list[Unit], int]:
    """Truncate a ranked unit list to a token budget; return (kept, realized_k).

    Units are consumed in rank order. The first unit is always kept, so a
    single long section cannot zero a query it answers. After that a unit is
    kept only if the running total stays within budget, and the walk stops
    at the first unit that does not fit: skipping ahead to a smaller unit
    would let an arm cherry-pick by size.
    """
    kept: list[Unit] = []
    used = 0
    for unit in units:
        cost = estimate_tokens(unit[2])
        if kept and used + cost > budget_tokens:
            break
        kept.append(unit)
        used += cost
    return kept, len(kept)


_WS = re.compile(r"\s+")
_RANK = {"exact": 2, "normalized": 1, "missing": 0}


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def contain(context: str, evidence: str) -> str:
    """exact: verbatim substring. normalized: substring after whitespace and
    case folding (retrieved but mangled). missing: neither."""
    if evidence in context:
        return "exact"
    if normalize(evidence) in normalize(context):
        return "normalized"
    return "missing"


def grade_containment(query: dict, kept: list[Unit]) -> dict:
    """Best containment status across the query's page-bearing labels.

    Containment is checked per unit, never across a concatenation: a span
    split across two chunks was not retrieved intact and must not score.
    """
    best = "missing"
    for lb in query["labels"]:
        if "page" not in lb or "evidence" not in lb:
            continue
        for _doc, _page, text in kept:
            status = contain(text, lb["evidence"])
            if _RANK[status] > _RANK[best]:
                best = status
            if best == "exact":
                break
        if best == "exact":
            break
    return {
        "span_recall": 1.0 if best != "missing" else 0.0,
        "fidelity_gap": 1.0 if best == "normalized" else 0.0,
        "status": best,
    }


def bootstrap_diff_ci(
    a: list[float],
    b: list[float],
    n_boot: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> dict:
    """Paired bootstrap CI for mean(a) - mean(b) over the same queries.

    Paired resampling matters: each query is answered by both arms, so
    resampling query indices (not the two lists independently) keeps the
    per-query dependence that makes the comparison fair.
    """
    if len(a) != len(b):
        raise ValueError(f"unpaired lengths {len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return {"mean_diff": 0.0, "lo": 0.0, "hi": 0.0, "includes_zero": True, "n": 0}
    rng = random.Random(seed)
    diffs = [x - y for x, y in zip(a, b)]
    mean_diff = sum(diffs) / n
    boots = []
    for _ in range(n_boot):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        boots.append(sum(sample) / n)
    boots.sort()
    lo = boots[int((alpha / 2) * n_boot)]
    hi = boots[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return {
        "mean_diff": round(mean_diff, 4),
        "lo": round(lo, 4),
        "hi": round(hi, 4),
        "includes_zero": lo <= 0.0 <= hi,
        "n": n,
    }


def no_arm_found(status_by_arm: dict[str, dict[str, str]]) -> list[str]:
    """Query ids whose evidence span no arm retrieved.

    The graded spans were validated against pdf-mcp's own extraction, so a
    span nobody finds may be a label defect rather than a retrieval miss.
    These go to manual page-image review and are reported, not scored.
    """
    if not status_by_arm:
        return []
    ids = set.intersection(*(set(s) for s in status_by_arm.values()))
    return sorted(
        q for q in ids if all(s[q] == "missing" for s in status_by_arm.values())
    )


def matches_to_units(matches: list[dict], id_by_path: dict[str, str]) -> list[Unit]:
    return [
        (id_by_path.get(m["path"], m["path"]), m["page"], m.get("excerpt", ""))
        for m in matches
    ]


def run_arm_p(
    paths: list[str],
    queries: list[dict],
    id_by_path: dict[str, str],
    budget_tokens: int,
    top_k: int = 25,
    excerpt_style: str = "paragraph",
    window_tokens: int | None = None,
    product_auto: bool = False,
) -> dict[str, dict]:
    """pdf-mcp corpus search, hybrid mode, run in-session.

    `excerpt_style="auto"` applies the per-query routing rule HERE in the
    harness (the original P-auto measurement). `product_auto=True` passes
    excerpt_style="auto" to the tool so the server routes; main() asserts
    the two produce identical kept units so the shipped rule and the
    measured rule cannot drift apart.

    Never lift these numbers from modes_results.md: runs from different
    cache warms are not comparable number for number. excerpt_style is
    passed explicitly (rather than relying on the tool default) because
    config.json records "excerpt_style": "paragraph" as fact about this
    run; a future change to the tool's default must not silently falsify
    that record.
    """
    from benchmark_corpus_modes import build_ranked, grade_query
    from pdf_mcp.server import _corpus_keyword_rankings, pdf_corpus_search

    rows: dict[str, dict] = {}
    for q in queries:
        t0 = time.perf_counter()
        style = excerpt_style
        chosen_by = None
        if excerpt_style == "auto":
            # Per-query excerpt shape from the signals the hybrid path
            # already computes: the number of documents with an AND
            # keyword match (no OR fallback, same as hybrid mode). 0 means a
            # paraphrase (paragraph is its best style); 1 means a single-
            # document keyword question (a contiguous window covers it);
            # 2+ means the answer is spread (many small units cover more
            # documents at the same budget).
            _lists, kw_docs, _payload = _corpus_keyword_rankings(
                paths, q["query"], top_k, 200, allow_or_fallback=False
            )
            n_docs = len(kw_docs)
            style = (
                "paragraph" if n_docs == 0 else ("window" if n_docs == 1 else "snippet")
            )
            chosen_by = {"keyword_docs": n_docs, "style": style}
        extra = {} if window_tokens is None else {"window_tokens": window_tokens}
        if product_auto:
            style = "auto"
        res = pdf_corpus_search(
            paths,
            q["query"],
            mode="auto",
            top_k=top_k,
            excerpt_style=style,
            **extra,
        )
        secs = time.perf_counter() - t0
        if "error" in res:
            raise RuntimeError(f"arm P {q['id']}: {res['error']}")
        if res["coverage"]["searched"] != len(paths):
            raise RuntimeError(f"arm P {q['id']}: partial coverage {res['coverage']}")
        if product_auto:
            r = res["excerpt_routing"]
            chosen_by = {"keyword_docs": r["keyword_doc_count"], "style": r["unit"]}
        units = matches_to_units(res["matches"], id_by_path)
        kept, k = cap_to_budget(units, budget_tokens)
        graded = grade_query(q, build_ranked(res["matches"], id_by_path), 10)
        rows[q["id"]] = {
            "class": q["class"],
            "kept": [(d, p) for d, p, _t in kept],
            "kept_text": [_t for _d, _p, _t in kept],
            "realized_k": k,
            "containment": grade_containment(q, kept),
            "doc_ndcg": graded["doc_ndcg"],
            "dochit3": graded["dochit3"],
            "seconds": round(secs, 3),
            **({"auto": chosen_by} if chosen_by else {}),
        }
    return rows


_PAGE_KEY = "x-amz-bedrock-kb-document-page-number"


def bedrock_results_to_units(
    results: list[dict], id_by_stem: dict[str, str]
) -> list[Unit]:
    units: list[Unit] = []
    for r in results:
        uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
        stem = Path(urlparse(uri).path).stem
        page_raw = r.get("metadata", {}).get(_PAGE_KEY)
        page = int(page_raw) if page_raw is not None else None
        text = r.get("content", {}).get("text", "")
        units.append((id_by_stem.get(stem, stem), page, text))
    return units


def run_arm_bedrock(
    runtime,
    kb_id: str,
    queries: list[dict],
    id_by_stem: dict[str, str],
    budget_tokens: int,
    rerank_model: str | None,
    n: int = 25,
) -> dict[str, dict]:
    """Bedrock KB retrieve (optional Cohere rerank), run in-session.

    Row shape is identical to run_arm_p's so summarize() can consume both
    arms interchangeably.
    """
    from _bedrock_kb import rerank, retrieve
    from benchmark_corpus_modes import grade_query

    rows: dict[str, dict] = {}
    for q in queries:
        t0 = time.perf_counter()
        results = retrieve(runtime, kb_id, q["query"], n=n)
        units = bedrock_results_to_units(results, id_by_stem)
        if rerank_model:
            order = rerank(
                runtime, q["query"], [u[2] for u in units], model=rerank_model
            )
            units = [units[i] for i in order]
        secs = time.perf_counter() - t0
        kept, k = cap_to_budget(units, budget_tokens)
        # doc-level grading needs (doc_id, page); page may be None for a
        # chunk with no page metadata, and grade_query only uses page for
        # page-level labels, so -1 is a safe non-matching placeholder. Pass
        # the full retrieved (and, where applicable, reranked) list, not a
        # pre-sliced window: grade_query dedups by document before trimming
        # to top_k, mirroring run_arm_p exactly. Slicing to units[:10] here
        # would dedup across fewer raw chunks than arm P's up-to-25, letting
        # duplicate chunks from one document crowd out documents a wider
        # window would have surfaced, a systematic penalty to doc_ndcg.
        ranked = [(d, p if p is not None else -1) for d, p, _t in units]
        graded = grade_query(q, ranked, 10)
        rows[q["id"]] = {
            "class": q["class"],
            "kept": [(d, p) for d, p, _t in kept],
            "kept_text": [_t for _d, _p, _t in kept],
            "realized_k": k,
            "containment": grade_containment(q, kept),
            "doc_ndcg": graded["doc_ndcg"],
            "dochit3": graded["dochit3"],
            "seconds": round(secs, 3),
        }
    return rows


def summarize(
    rows_by_arm: dict[str, dict[str, dict]],
    classes: list[str],
    anchor_arms: tuple[str, ...] = ("B0", "B1"),
    ref_arm: str = "P",
    exclude_flagged: bool = True,
    bedrock_arms: tuple[str, ...] = ("B0", "B1"),
) -> dict:
    """Per-class means and paired diffs.

    `diffs` compares ref_arm against every other arm (local and Bedrock).
    `diffs_vs_anchors` additionally compares every non-ref LOCAL arm
    against every Bedrock anchor, so a non-default configuration (e.g.
    the evidence_budget arm) has its own anchor CIs on record instead of
    being readable only relative to ref_arm.

    A query is flagged when no arm's containment status is anything but
    `missing` (see no_arm_found): the graded span was validated against
    pdf-mcp's own extraction, so a span nobody finds may be a label defect
    rather than a retrieval miss, and the spec excludes flagged queries
    "until reviewed". exclude_flagged selects which contract applies here:
    True drops them from every mean (the pre-review sensitivity view);
    False keeps them as legitimate 0-0 observations (the post-review
    primary view, once every flagged id has been checked against the page
    image and confirmed a genuine miss). Both variants report `n` (queries
    actually averaged) alongside `n_total` (every query in the class,
    regardless of exclude_flagged), so a reader can see how many were
    dropped.
    """
    status_by_arm = {
        arm: {qid: r["containment"]["status"] for qid, r in rows.items()}
        for arm, rows in rows_by_arm.items()
    }
    flagged = set(no_arm_found(status_by_arm))
    per_class: dict[str, dict] = {}
    diffs: dict[str, dict] = {}
    for cls in classes:
        per_class[cls] = {}
        n_total = None
        for arm, rows in rows_by_arm.items():
            in_class = [r for qid, r in rows.items() if r["class"] == cls]
            if n_total is None:
                n_total = len(in_class)
            sel = (
                [
                    r
                    for qid, r in rows.items()
                    if r["class"] == cls and qid not in flagged
                ]
                if exclude_flagged
                else in_class
            )
            n = len(sel)
            mean = lambda key: (  # noqa: E731
                round(sum(key(r) for r in sel) / n, 4) if n else None
            )
            per_class[cls][arm] = {
                "span_recall": mean(lambda r: r["containment"]["span_recall"]),
                "fidelity_gap": mean(lambda r: r["containment"]["fidelity_gap"]),
                "doc_ndcg": mean(lambda r: r["doc_ndcg"]),
                "dochit3": mean(lambda r: r["dochit3"]),
                "mean_k": mean(lambda r: r["realized_k"]),
                "n": n,
                "n_total": n_total,
            }
        diffs[cls] = {}
        ref = rows_by_arm.get(ref_arm, {})
        ids = sorted(
            q
            for q, r in ref.items()
            if r["class"] == cls and (not exclude_flagged or q not in flagged)
        )
        for arm in anchor_arms:
            if arm not in rows_by_arm:
                continue
            a = [ref[q]["containment"]["span_recall"] for q in ids]
            b = [rows_by_arm[arm][q]["containment"]["span_recall"] for q in ids]
            diffs[cls][arm] = bootstrap_diff_ci(a, b)
    diffs_vs_anchors: dict[str, dict] = {}
    for cls in classes:
        diffs_vs_anchors[cls] = {}
        ids = sorted(
            q
            for q, r in rows_by_arm.get(ref_arm, {}).items()
            if r["class"] == cls and (not exclude_flagged or q not in flagged)
        )
        for arm, rows in rows_by_arm.items():
            if arm == ref_arm or arm in bedrock_arms:
                continue
            a = [rows[q]["containment"]["span_recall"] for q in ids]
            for anchor in bedrock_arms:
                if anchor not in rows_by_arm:
                    continue
                b = [rows_by_arm[anchor][q]["containment"]["span_recall"] for q in ids]
                diffs_vs_anchors[cls][f"{arm} minus {anchor}"] = bootstrap_diff_ci(a, b)
    return {
        "per_class": per_class,
        "diffs": diffs,
        "diffs_vs_anchors": diffs_vs_anchors,
        "flagged": sorted(flagged),
    }


def _cell(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def _table(arms: dict) -> list[str]:
    lines = [
        "| arm | n | n total | span recall | fidelity gap | doc-NDCG@10 | "
        "doc-hit@3 | realized k |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm, m in arms.items():
        lines.append(
            f"| {arm} | {m['n']} | {m['n_total']} | {_cell(m['span_recall'])} | "
            f"{_cell(m['fidelity_gap'])} | {_cell(m['doc_ndcg'])} | "
            f"{_cell(m['dochit3'])} | {_cell(m['mean_k'])} |"
        )
    return lines


def _diff_lines(diffs: dict, prefix: str = "P minus ") -> list[str]:
    lines = []
    for arm, ci in diffs.items():
        zero = "includes zero" if ci["includes_zero"] else "excludes zero"
        lines.append(
            f"- {prefix}{arm}, span recall: {ci['mean_diff']:+.3f} "
            f"[{ci['lo']:+.3f}, {ci['hi']:+.3f}] ({zero}, n={ci['n']})"
        )
    return lines


def render_markdown(
    summary: dict, config: dict, sensitivity: dict | None = None
) -> str:
    """Render the per-class tables. summary is the primary (include-flagged)
    view; sensitivity, if given, is the exclude-flagged view and is
    rendered as a labelled second table under each class.

    This file is regenerated on every run: interpretation, the flagged-id
    review, provenance, and cost notes are hand-written and live in
    ANALYSIS.md instead, so a re-run never silently destroys them.
    """
    n_flagged = len(summary["flagged"])
    out = [
        "<!-- GENERATED by scripts/benchmark_bedrock_kb.py -- do not hand-edit, "
        "this file is overwritten on every run. See ANALYSIS.md for the "
        "interpretation, the flagged-query review, provenance, and observed "
        "AWS cost. -->",
        "",
        "# Bedrock KB anchor benchmark",
        "",
        f"Generated {dt.date.today().isoformat()}. Token budget "
        f"{config.get('budget_tokens')} per query per arm. Bedrock is an anchor, "
        "not a subject; any result is acceptable. Never average across classes. "
        "See [ANALYSIS.md](ANALYSIS.md) for the interpretation.",
        "",
    ]
    if n_flagged:
        out += [
            f"{n_flagged} of the queries below had their evidence span found by "
            "no arm. Each was reviewed against the page image and confirmed a "
            "genuine miss, not a label defect (see ANALYSIS.md), so the tables "
            "below include them as legitimate 0-0 observations. A "
            "flagged-excluded sensitivity table follows each primary table.",
            "",
        ]
    for cls, arms in summary["per_class"].items():
        out += [f"## {cls}", ""]
        out += _table(arms)
        out.append("")
        out += _diff_lines(summary["diffs"].get(cls, {}))
        out += _diff_lines(summary.get("diffs_vs_anchors", {}).get(cls, {}), prefix="")
        out.append("")
        if sensitivity is not None:
            out += [f"### {cls}, sensitivity (flagged queries excluded)", ""]
            out += _table(sensitivity["per_class"].get(cls, {}))
            out.append("")
            out += _diff_lines(sensitivity["diffs"].get(cls, {}))
            out += _diff_lines(
                sensitivity.get("diffs_vs_anchors", {}).get(cls, {}), prefix=""
            )
            out.append("")
    out += [
        "## Flagged for manual page-image review",
        "",
        "Evidence span found by no arm. Included in the primary tables above "
        "(reviewed and confirmed a genuine miss, see ANALYSIS.md); excluded "
        "from the sensitivity tables.",
        "",
    ]
    out += [f"- {q}" for q in summary["flagged"]] or ["- none"]
    out.append("")
    return "\n".join(out)


def write_results(
    summary: dict,
    rows_by_arm: dict,
    config: dict,
    out_dir: Path,
    sensitivity: dict | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": config,
        "summary": summary,
        "summary_excl_flagged": sensitivity,
        "per_query": rows_by_arm,
    }
    (out_dir / "results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "RESULTS.md").write_text(
        render_markdown(summary, config, sensitivity=sensitivity), encoding="utf-8"
    )


def _sha256_json(obj: Any) -> str:
    """Byte-identical to scripts/_bedrock_kb.sha256_json. Duplicated here so the
    offline --reuse-bedrock-from path never imports _bedrock_kb (which pulls in
    botocore at module top). tests assert the two stay in sync."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def warm_corpus(paths: list[str]) -> dict:
    """Warm the corpus into the ACTIVE cache (default or PDF_MCP_CACHE_DIR) so
    arm P scores the same cache the tool serves from. Idempotent: already-warm
    docs cost a per-page SQLite check (seconds); a cold cache takes minutes,
    once. Loops until nothing is unprocessed. The caller must check
    warm_complete: scoring a partially warmed corpus is the silent-partial-warm
    trap, and pdf_corpus_search would report partial coverage anyway."""
    from pdf_mcp.server import pdf_corpus_warm

    warm = pdf_corpus_warm(paths, budget_seconds=900, embeddings=True)
    while warm.get("unprocessed"):
        warm = pdf_corpus_warm(paths, budget_seconds=900, embeddings=True)
    return warm


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--arms",
        default=None,
        help="comma list of arm ids; default: P plus every Bedrock arm in config.json",
    )
    ap.add_argument("--budget", type=int, default=BUDGET_TOKENS)
    ap.add_argument("--limit", type=int, default=None, help="pilot: first N queries")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument(
        "--reuse-bedrock-from",
        type=Path,
        default=None,
        help=(
            "reuse stored Bedrock rows from this results.json (default: the "
            "canonical benchmark_data/bedrock_kb/results.json). Offline."
        ),
    )
    ap.add_argument(
        "--live-classes",
        default="",
        help=(
            "with --live: re-query Bedrock only for these comma-separated query "
            "classes and take every other id from the stored rows (label "
            "revisions on one class cost cents, not the whole run)"
        ),
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help=(
            "re-query Bedrock instead of reusing stored rows (about $0.20, needs "
            "AWS credentials). Only needed after a Bedrock-side change."
        ),
    )
    args = ap.parse_args(argv)

    from benchmark_corpus_modes import class_names

    manifest = json.loads((args.data_dir / "manifest.json").read_text(encoding="utf-8"))
    queries_doc = json.loads(
        (args.data_dir / "queries.json").read_text(encoding="utf-8")
    )
    queries = (
        queries_doc["queries"][: args.limit] if args.limit else queries_doc["queries"]
    )
    classes = class_names(queries_doc)
    config = json.loads((OUT_DIR / "config.json").read_text(encoding="utf-8"))
    config["budget_tokens"] = args.budget
    config["n_queries"] = len(queries)

    errors = check_corpus_quota(manifest, REPO)
    if errors:
        for e in errors:
            print("QUOTA", e)
        return 2

    id_by_path = {str((REPO / d["path"]).resolve()): d["id"] for d in manifest["docs"]}
    id_by_stem = {Path(d["path"]).stem: d["id"] for d in manifest["docs"]}
    arms = (
        [a.strip() for a in args.arms.split(",") if a.strip()]
        if args.arms
        else list(config["arms"])
    )
    rows_by_arm: dict[str, dict] = {}

    local_arms = [a for a in arms if config["arms"].get(a, {}).get("tool")]
    if local_arms:
        # The script owns the warm, so a plain invocation works from a cold or
        # half-warm cache. Refuse to score a partial corpus.
        t0 = time.perf_counter()
        warm = warm_corpus(list(id_by_path))
        if not warm.get("warm_complete") or warm.get("unprocessed"):
            print(
                "ERROR: corpus warm incomplete: "
                f"unprocessed={warm.get('unprocessed')} skipped={warm.get('skipped')}. "
                "Refusing to score a partial corpus."
            )
            return 2
        print(
            f"warmed {len(id_by_path)} docs (text+embeddings) in "
            f"{time.perf_counter() - t0:.0f}s ({len(warm.get('skipped', []))} skipped)"
        )
    for arm in local_arms:
        rows_by_arm[arm] = run_arm_p(
            list(id_by_path),
            queries,
            id_by_path,
            args.budget,
            excerpt_style=config["arms"][arm].get("excerpt_style", "paragraph"),
            window_tokens=config["arms"][arm].get("window_tokens"),
            product_auto=bool(config["arms"][arm].get("product_auto")),
        )
        print(f"{arm}: done ({len(rows_by_arm[arm])} queries)")
    # Drift guard: every product arm (evidence_budget) must reproduce the
    # harness-side routing arm (excerpt_style "auto") unit for unit.
    harness_auto = [
        a for a in local_arms if config["arms"][a].get("excerpt_style") == "auto"
    ]
    product_arms = [a for a in local_arms if config["arms"][a].get("product_auto")]
    for ref in harness_auto:
        for arm in product_arms:
            diverged = [
                qid
                for qid in rows_by_arm[ref]
                if rows_by_arm[ref][qid]["kept_text"]
                != rows_by_arm[arm][qid]["kept_text"]
                or rows_by_arm[ref][qid]["auto"] != rows_by_arm[arm][qid]["auto"]
            ]
            if diverged:
                print(
                    f"ERROR: {arm} (product excerpt_style=auto) diverged from {ref} "
                    f"(harness rule) on {len(diverged)} queries: {diverged[:5]}"
                )
                return 2
            print(f"{arm}: identical to {ref} on all {len(rows_by_arm[ref])} queries")

    bedrock_arms = [a for a in arms if not config["arms"].get(a, {}).get("tool")]
    index_stamps_by_arm: dict[str, dict] = {}
    # Offline reuse is the default: Bedrock retrieval measured byte-identical
    # across runs, so re-querying it is pure cost and needs credentials.
    # --live is the explicit opt-in for the paid path.
    reuse_path: Path | None = None
    if args.live and args.reuse_bedrock_from:
        print("ERROR: --live and --reuse-bedrock-from are mutually exclusive.")
        return 2
    if bedrock_arms and not args.live:
        reuse_path = args.reuse_bedrock_from or (OUT_DIR / "results.json")
        if not reuse_path.exists():
            print(
                f"ERROR: no stored Bedrock rows at {reuse_path}. Pass --live to "
                "query Bedrock (about $0.20, needs AWS credentials), or "
                "--reuse-bedrock-from PATH to point at a prior results.json."
            )
            return 2
    live_classes = {c.strip() for c in args.live_classes.split(",") if c.strip()}
    if live_classes and not args.live:
        print("ERROR: --live-classes needs --live")
        return 2
    query_by_id = {q["id"]: q for q in queries}
    if bedrock_arms and (reuse_path or live_classes):
        # Bedrock retrieval measured byte-identical across runs, so its rows
        # only ever needed re-querying for the paired-CI mechanics. Load them
        # instead. Four offline refusals stand in for the live drift guard.
        if reuse_path is None:
            reuse_path = OUT_DIR / "results.json"
        prior = json.loads(reuse_path.read_text(encoding="utf-8"))
        prior_cfg = prior.get("config", {})
        manifest_sha = hashlib.sha256(
            (args.data_dir / "manifest.json").read_bytes()
        ).hexdigest()
        current_qids = {q["id"] for q in queries}
        if prior_cfg.get("budget_tokens") != args.budget:
            print(
                f"ERROR: stored Bedrock rows were scored at budget "
                f"{prior_cfg.get('budget_tokens')}, this run uses {args.budget}. "
                "kept and containment are budget-dependent; match the budget or "
                "re-run live."
            )
            return 2
        for arm in bedrock_arms:
            label = config["arms"][arm].get("label", arm)
            rows = prior.get("per_query", {}).get(label)
            stamp = prior_cfg.get("index_stamps", {}).get(label, {})
            if rows is None:
                print(f"ERROR: {reuse_path} has no rows for {arm}")
                return 2
            drift = []
            if stamp.get("arm_config_sha256") != _sha256_json(config["arms"][arm]):
                drift.append("arm_config")
            if stamp.get("manifest_sha256") != manifest_sha:
                drift.append("manifest")
            if drift:
                print(
                    f"ERROR: stored rows for {arm} came from a different "
                    f"{' and '.join(drift)}; re-run live."
                )
                return 2
            if set(rows) != current_qids:
                print(
                    f"ERROR: stored rows for {arm} cover {len(rows)} query ids, "
                    f"this run has {len(current_qids)}; paired CIs would misalign. "
                    "Re-run live."
                )
                return 2
            # Rows that carry the kept unit texts are re-graded against the
            # CURRENT labels, so a label revision never needs Bedrock again.
            # Older rows (no texts) keep their stored containment.
            regraded = 0
            for qid, row in rows.items():
                if "kept_text" in row and qid in query_by_id:
                    units = [
                        (d, p, t) for (d, p), t in zip(row["kept"], row["kept_text"])
                    ]
                    row["containment"] = grade_containment(query_by_id[qid], units)
                    regraded += 1
            rows_by_arm[arm] = rows
            index_stamps_by_arm[arm] = {
                **stamp,
                "reused_from": provenance_path(reuse_path),
            }
            print(
                f"{arm}: reused {len(rows)} stored rows (offline), "
                f"{regraded} re-graded against current labels"
            )
        config["bedrock_rows_reused_from"] = provenance_path(reuse_path)
        config["bedrock_live_check"] = False
    if bedrock_arms and args.live:
        import boto3

        from _bedrock_kb import (
            TAG_KEY,
            ingest_stamp_matches,
            load_state,
            sha256_json,
            stack_name,
            stack_outputs,
        )

        # .stack.json always lives in the canonical OUT_DIR, never in a
        # pilot/sub out-dir (--out-dir only changes where results.json and
        # RESULTS.md land for this run).
        state = load_state(OUT_DIR / ".stack.json")
        manifest_path = args.data_dir / "manifest.json"
        cfn = boto3.Session(region_name=config["region"]).client("cloudformation")
        deployed: dict[str, dict] = {}
        runtime = boto3.Session(region_name=config["region"]).client(
            "bedrock-agent-runtime"
        )
        for arm in bedrock_arms:
            out = stack_outputs(cfn, stack_name(arm))
            st = state.get(arm)
            if not out or not st or not st.get("ingested"):
                print(
                    f"ERROR: arm {arm} not deployed+ingested; run bedrock_kb_stack.py"
                )
                return 2
            drift = []
            if out.get("tags", {}).get(TAG_KEY) != sha256_json(config["arms"][arm]):
                drift.append("arm_config")
            drift += ingest_stamp_matches(st.get("stamp", {}), manifest_path)
            if drift:
                print(
                    f"ERROR: arm {arm} index was built from a different "
                    f"{' and '.join(drift)}. "
                    "Indexes are immutable: add a new arm id with a bumped -vN "
                    "suffix, deploy it, and run that instead."
                )
                return 2
            deployed[arm] = out
            index_stamps_by_arm[arm] = {
                "stack": stack_name(arm),
                "arm_config_sha256": out.get("tags", {}).get(TAG_KEY),
                **st.get("stamp", {}),
            }
            live_queries = (
                [q for q in queries if q["class"] in live_classes]
                if live_classes
                else queries
            )
            live_rows = run_arm_bedrock(
                runtime,
                out["KnowledgeBaseId"],
                live_queries,
                id_by_stem,
                args.budget,
                rerank_model=config["arms"][arm]["rerank"],
            )
            # under --live-classes the other ids come from the stored rows
            # loaded above; a plain --live replaces everything.
            rows_by_arm[arm] = {**rows_by_arm.get(arm, {}), **live_rows}
            print(f"{arm}: done ({len(live_rows)} live, {len(rows_by_arm[arm])} total)")
        config["bedrock_live_check"] = True
        if live_classes:
            config["bedrock_live_classes"] = sorted(live_classes)

    # Result tables use the short label (B0, B1); the immutable arm id and its
    # stamp go into provenance so a reader can tell which index produced a row.
    label_of = {a: config["arms"].get(a, {}).get("label", a) for a in rows_by_arm}
    rows_by_label = {label_of[a]: r for a, r in rows_by_arm.items()}
    config["arm_ids"] = {label_of[a]: a for a in rows_by_arm}
    config["index_stamps"] = {
        label_of[a]: index_stamps_by_arm[a] for a in bedrock_arms if a in rows_by_arm
    }
    rows_by_arm = rows_by_label
    ref_label = label_of.get("P", "P")
    # Every arm except the reference is compared against it, including other
    # local arms such as P-snippet. Comparing only the Bedrock arms would
    # silently drop the excerpt-style comparison from the CI tables.
    anchors = tuple(lbl for a, lbl in label_of.items() if a != "P")
    bedrock_labels = tuple(label_of[a] for a in label_of if a in bedrock_arms)
    summary = summarize(
        rows_by_arm,
        classes,
        anchor_arms=anchors,
        ref_arm=ref_label,
        exclude_flagged=False,
        bedrock_arms=bedrock_labels,
    )
    sensitivity = summarize(
        rows_by_arm,
        classes,
        anchor_arms=anchors,
        ref_arm=ref_label,
        exclude_flagged=True,
        bedrock_arms=bedrock_labels,
    )
    write_results(summary, rows_by_arm, config, args.out_dir, sensitivity=sensitivity)
    print(render_markdown(summary, config, sensitivity=sensitivity))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
