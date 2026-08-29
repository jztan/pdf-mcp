"""Anchor benchmark: pdf-mcp corpus search vs Bedrock Knowledge Bases.

Scores every arm by evidence-span containment at an equal token budget,
per query class, with bootstrap CIs. Bedrock is an anchor, not a subject:
any result is acceptable.

Arms: P (pdf_corpus_search, hybrid), B0 (Bedrock default), B1 (Bedrock
fixed-1000 + Cohere Rerank 3.5). B2 and N are optional and not built here.
"""

from __future__ import annotations

import argparse
import datetime as dt
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
) -> dict[str, dict]:
    """pdf-mcp corpus search, hybrid mode, run in-session.

    Never lift these numbers from modes_results.md: runs from different
    cache warms are not comparable number for number.
    """
    from benchmark_corpus_modes import build_ranked, grade_query
    from pdf_mcp.server import pdf_corpus_search

    rows: dict[str, dict] = {}
    for q in queries:
        t0 = time.perf_counter()
        res = pdf_corpus_search(paths, q["query"], mode="auto", top_k=top_k)
        secs = time.perf_counter() - t0
        if "error" in res:
            raise RuntimeError(f"arm P {q['id']}: {res['error']}")
        if res["coverage"]["searched"] != len(paths):
            raise RuntimeError(f"arm P {q['id']}: partial coverage {res['coverage']}")
        units = matches_to_units(res["matches"], id_by_path)
        kept, k = cap_to_budget(units, budget_tokens)
        graded = grade_query(q, build_ranked(res["matches"], id_by_path), 10)
        rows[q["id"]] = {
            "class": q["class"],
            "kept": [(d, p) for d, p, _t in kept],
            "realized_k": k,
            "containment": grade_containment(q, kept),
            "doc_ndcg": graded["doc_ndcg"],
            "dochit3": graded["dochit3"],
            "seconds": round(secs, 3),
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
) -> dict:
    status_by_arm = {
        arm: {qid: r["containment"]["status"] for qid, r in rows.items()}
        for arm, rows in rows_by_arm.items()
    }
    flagged = set(no_arm_found(status_by_arm))
    per_class: dict[str, dict] = {}
    diffs: dict[str, dict] = {}
    for cls in classes:
        per_class[cls] = {}
        for arm, rows in rows_by_arm.items():
            sel = [
                r for qid, r in rows.items() if r["class"] == cls and qid not in flagged
            ]
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
            }
        diffs[cls] = {}
        ref = rows_by_arm.get(ref_arm, {})
        ids = sorted(
            q for q, r in ref.items() if r["class"] == cls and q not in flagged
        )
        for arm in anchor_arms:
            if arm not in rows_by_arm:
                continue
            a = [ref[q]["containment"]["span_recall"] for q in ids]
            b = [rows_by_arm[arm][q]["containment"]["span_recall"] for q in ids]
            diffs[cls][arm] = bootstrap_diff_ci(a, b)
    return {"per_class": per_class, "diffs": diffs, "flagged": sorted(flagged)}


def _cell(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def render_markdown(summary: dict, config: dict) -> str:
    out = [
        "# Bedrock KB anchor benchmark",
        "",
        f"Generated {dt.date.today().isoformat()}. Token budget "
        f"{config.get('budget_tokens')} per query per arm. Bedrock is an anchor, "
        "not a subject; any result is acceptable. Never average across classes.",
        "",
    ]
    for cls, arms in summary["per_class"].items():
        out += [f"## {cls}", ""]
        out.append(
            "| arm | n | span recall | fidelity gap | doc-NDCG@10 | "
            "doc-hit@3 | realized k |"
        )
        out.append("|---|---|---|---|---|---|---|")
        for arm, m in arms.items():
            out.append(
                f"| {arm} | {m['n']} | {_cell(m['span_recall'])} | "
                f"{_cell(m['fidelity_gap'])} | {_cell(m['doc_ndcg'])} | "
                f"{_cell(m['dochit3'])} | {_cell(m['mean_k'])} |"
            )
        out.append("")
        for arm, ci in summary["diffs"].get(cls, {}).items():
            zero = "includes zero" if ci["includes_zero"] else "excludes zero"
            out.append(
                f"- P minus {arm}, span recall: {ci['mean_diff']:+.3f} "
                f"[{ci['lo']:+.3f}, {ci['hi']:+.3f}] ({zero}, n={ci['n']})"
            )
        out.append("")
    out += [
        "## Flagged for manual page-image review",
        "",
        "Evidence span found by no arm; excluded from every mean above.",
        "",
    ]
    out += [f"- {q}" for q in summary["flagged"]] or ["- none"]
    out.append("")
    return "\n".join(out)


def write_results(
    summary: dict, rows_by_arm: dict, config: dict, out_dir: Path
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": config,
        "summary": summary,
        "per_query": rows_by_arm,
    }
    (out_dir / "results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "RESULTS.md").write_text(
        render_markdown(summary, config), encoding="utf-8"
    )


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
        else ["P"] + [a for a in config["arms"] if a != "P"]
    )
    rows_by_arm: dict[str, dict] = {}

    if "P" in arms:
        rows_by_arm["P"] = run_arm_p(list(id_by_path), queries, id_by_path, args.budget)
        print(f"P: done ({len(rows_by_arm['P'])} queries)")

    bedrock_arms = [a for a in arms if a.startswith("B")]
    if bedrock_arms:
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
            rows_by_arm[arm] = run_arm_bedrock(
                runtime,
                out["KnowledgeBaseId"],
                queries,
                id_by_stem,
                args.budget,
                rerank_model=config["arms"][arm]["rerank"],
            )
            print(f"{arm}: done")

    # Result tables use the short label (B0, B1); the immutable arm id and its
    # stamp go into provenance so a reader can tell which index produced a row.
    label_of = {a: config["arms"].get(a, {}).get("label", a) for a in rows_by_arm}
    rows_by_label = {label_of[a]: r for a, r in rows_by_arm.items()}
    config["arm_ids"] = {label_of[a]: a for a in rows_by_arm}
    config["index_stamps"] = (
        {
            label_of[a]: {
                "stack": stack_name(a),
                "arm_config_sha256": deployed[a]["tags"].get(TAG_KEY),
                **state[a].get("stamp", {}),
            }
            for a in bedrock_arms
            if a in rows_by_arm
        }
        if bedrock_arms
        else {}
    )
    rows_by_arm = rows_by_label
    anchors = tuple(label_of[a] for a in bedrock_arms)
    summary = summarize(rows_by_arm, classes, anchor_arms=anchors, ref_arm="P")
    write_results(summary, rows_by_arm, config, args.out_dir)
    print(render_markdown(summary, config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
