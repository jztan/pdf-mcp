"""Benchmark query-class names, and the lookup that reads old saved files.

Stdlib only, no I/O. The corpus query set grades the four classes in
``CLASS_MEANINGS``, which every results table prints as its legend.

Until 2026-10-05 these were called described / needle / spread / trap.
Query ids keep the old prefix (``trap-01``) on purpose: they are opaque
join keys into caches, transcripts and earlier result files. Saved files
written before the rename still carry the old class labels, so every
harness passes what it loads through ``normalize_*``. Scripts that write a
query set back (the merge and authoring scripts) save it under the current
names, so the live query files move to the new labels once the rename is
merged; older branches cannot read them after that.
"""

from __future__ import annotations

from typing import Any

PARAPHRASE = "paraphrase"
EXACT_MATCH = "exact_match"
MULTI_DOC = "multi_doc"
LEXICAL_DISTRACTOR = "lexical_distractor"

CLASS_NAMES = (PARAPHRASE, EXACT_MATCH, MULTI_DOC, LEXICAL_DISTRACTOR)

CLASS_MEANINGS = {
    PARAPHRASE: "the question uses different words from the PDF",
    EXACT_MATCH: "a short literal query whose rare terms appear in one place",
    MULTI_DOC: "several documents each hold a valid answer; finding any one counts",
    LEXICAL_DISTRACTOR: (
        "the query terms are boilerplate in most documents and meaningful in"
        " one; the system must find that one (the answer is in the corpus)"
    ),
}

OLD_TO_NEW = {
    "described": PARAPHRASE,
    "needle": EXACT_MATCH,
    "spread": MULTI_DOC,
    "trap": LEXICAL_DISTRACTOR,
}

# Query ids and the on-disk artifacts named after a class (candidate files,
# authoring caches and their keys) keep the old prefix, so new queries
# number on from the old ones and cached billed calls still hit.
LEGACY_PREFIX = {new: old for old, new in OLD_TO_NEW.items()}


def normalize_class(name: str) -> str:
    """Map an old class label to its current name; other labels pass through."""
    return OLD_TO_NEW.get(name, name)


def normalize_classes(names: str | list[str]) -> list[str]:
    """Normalize a comma-separated string or list of class names (CLI args)."""
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    return [normalize_class(n) for n in names]


def normalize_rows(rows: Any) -> Any:
    """Rewrite every ``"class"`` value in place, then return ``rows``.

    Accepts a query-set document (``{"queries": [...]}``), a list of row
    dicts, or a dict of row dicts keyed by query id, nested arbitrarily.
    """
    if isinstance(rows, dict):
        cls = rows.get("class")
        if isinstance(cls, str):
            rows["class"] = normalize_class(cls)
        for value in rows.values():
            if isinstance(value, (dict, list)):
                normalize_rows(value)
    elif isinstance(rows, list):
        for item in rows:
            if isinstance(item, (dict, list)):
                normalize_rows(item)
    return rows


def normalize_keys(by_class: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a ``{class_name: value}`` mapping under current names."""
    return {normalize_class(k): v for k, v in by_class.items()}


def legend_lines(classes: list[str] | tuple[str, ...] = CLASS_NAMES) -> list[str]:
    """Markdown bullets explaining each class present in a results table."""
    shown = [c for c in classes if c in CLASS_MEANINGS]
    if not shown:
        return []
    return [f"- **{c}**: {CLASS_MEANINGS[c]}" for c in shown] + [""]
