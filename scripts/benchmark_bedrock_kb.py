"""Anchor benchmark: pdf-mcp corpus search vs Bedrock Knowledge Bases.

Scores every arm by evidence-span containment at an equal token budget,
per query class, with bootstrap CIs. Bedrock is an anchor, not a subject:
any result is acceptable. See
docs/superpowers/specs/2026-08-29-bedrock-kb-comparison-design.md.

Arms: P (pdf_corpus_search, hybrid), B0 (Bedrock default), B1 (Bedrock
fixed-1000 + Cohere Rerank 3.5). B2 and N are optional and not built here.
"""

from __future__ import annotations

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
