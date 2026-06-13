"""Extraction-coherence eval harness.

Has Claude read pdf-mcp's extracted text and classify reading-order coherence as
coherent / partial / scrambled. Calibrates the judge against fixed-text gold
fixtures, judges a corpus via majority-of-3, and diffs against a committed
baseline so extraction-quality regressions are caught. See
docs_internal/specs/2026-06-13-coherence-eval-harness-design.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

VERDICTS = ("coherent", "partial", "scrambled")
# Ordinal for regression comparison; non-ordinal sentinels excluded from it.
_ORDINAL = {"scrambled": 0, "partial": 1, "coherent": 2}


@dataclass(frozen=True)
class Verdict:
    verdict: str  # one of VERDICTS, or "error" / "unavailable"
    rationale: str = ""
    confidence: str = ""


def parse_verdict(raw: str) -> Verdict:
    """Parse a judge JSON reply into a Verdict; malformed/unknown -> 'error'."""
    try:
        data = json.loads(raw)
        verdict = data["verdict"]
    except (ValueError, TypeError, KeyError):
        return Verdict("error", "unparseable judge response")
    if verdict not in VERDICTS:
        return Verdict("error", f"unknown verdict {verdict!r}")
    return Verdict(
        verdict,
        str(data.get("rationale", "")),
        str(data.get("confidence", "")),
    )
