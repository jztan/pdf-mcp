"""Anchor benchmark: pdf-mcp corpus search vs Bedrock Knowledge Bases.

Scores every arm by evidence-span containment at an equal token budget,
per query class, with bootstrap CIs. Bedrock is an anchor, not a subject:
any result is acceptable. See
docs/superpowers/specs/2026-08-29-bedrock-kb-comparison-design.md.

Arms: P (pdf_corpus_search, hybrid), B0 (Bedrock default), B1 (Bedrock
fixed-1000 + Cohere Rerank 3.5). B2 and N are optional and not built here.
"""

from __future__ import annotations

import re
import sys
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
