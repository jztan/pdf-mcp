"""Extraction-coherence eval harness.

Has Claude read pdf-mcp's extracted text and classify reading-order coherence as
coherent / partial / scrambled. Calibrates the judge against fixed-text gold
fixtures, judges a corpus via majority-of-3, and diffs against a committed
baseline so extraction-quality regressions are caught. See
docs_internal/specs/2026-06-13-coherence-eval-harness-design.md.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Sequence

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


def majority_verdict(votes: Sequence[Verdict]) -> Verdict:
    """Return the strict-majority verdict, else 'error'.

    A label needs > half the votes to win. No majority (e.g. 3-way split, or
    errors preventing a majority) -> 'error', surfaced for investigation. The
    returned rationale is taken from the first vote carrying the winning label.
    """
    counts = Counter(v.verdict for v in votes)
    label, n = counts.most_common(1)[0]
    if label == "error" or n * 2 <= len(votes):
        return Verdict("error", f"no majority: {dict(counts)}")
    rationale = next(v.rationale for v in votes if v.verdict == label)
    return Verdict(label, rationale)


def judge_majority(
    text: str, direction: str, judge: Callable[[str, str], Verdict], n: int = 3
) -> Verdict:
    """Call ``judge`` n times and return the majority verdict (n=3 default)."""
    return majority_verdict([judge(text, direction) for _ in range(n)])


def compare(baseline: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    """Per-page diff status: regressed | improved | same | error | unavailable.

    'error' and 'unavailable' are non-ordinal: 'error' (judge failed) is always
    surfaced as a failure; 'unavailable' (source could not be fetched) is
    reported and excluded from the regression decision.
    """
    out: dict[str, str] = {}
    for page_id, cur in current.items():
        base = baseline.get(page_id)
        if cur == "error":
            out[page_id] = "error"
        elif cur == "unavailable":
            out[page_id] = "unavailable"
        elif base is None or base not in _ORDINAL:
            out[page_id] = "new"
        elif _ORDINAL[cur] < _ORDINAL[base]:
            out[page_id] = "regressed"
        elif _ORDINAL[cur] > _ORDINAL[base]:
            out[page_id] = "improved"
        else:
            out[page_id] = "same"
    return out


def has_regression(diff: dict[str, str]) -> bool:
    """True if any page regressed or errored (the guard's failing condition)."""
    return any(status in ("regressed", "error") for status in diff.values())


def active_extras_config() -> dict[str, bool]:
    """Snapshot which optional extras affect extraction output.

    Uses the same predicates server_info uses, so the stamp matches real
    extraction behaviour. Imported lazily so the module loads without pdf_mcp.
    """
    from pdf_mcp import extractor

    cfg = {
        "column_aware": extractor.column_detection_available(),
        "vertical_aware": extractor.vertical_detection_available(),
    }
    try:
        from pdf_mcp import config, embedder

        embedder.check_available(config.PDFConfig().embedding_model)
        cfg["semantic"] = True
    except Exception:
        cfg["semantic"] = False
    return cfg


def config_matches(a: dict[str, bool], b: dict[str, bool]) -> bool:
    """True when two extras-config snapshots are identical."""
    return a == b
