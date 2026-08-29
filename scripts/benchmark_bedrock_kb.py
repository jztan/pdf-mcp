"""Anchor benchmark: pdf-mcp corpus search vs Bedrock Knowledge Bases.

Scores every arm by evidence-span containment at an equal token budget,
per query class, with bootstrap CIs. Bedrock is an anchor, not a subject:
any result is acceptable. See
docs/superpowers/specs/2026-08-29-bedrock-kb-comparison-design.md.

Arms: P (pdf_corpus_search, hybrid), B0 (Bedrock default), B1 (Bedrock
fixed-1000 + Cohere Rerank 3.5). B2 and N are optional and not built here.
"""

from __future__ import annotations

import random
import re
import sys
import time
from pathlib import Path

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
