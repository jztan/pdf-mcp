"""Hidden-text regression ratchet over real PDFs.

Scans every page of a fixed real-PDF corpus with the production detector
(pdfium backend pages, content_trust._scan_page_geometry) and compares the
flagged pages, per document, against benchmark_data/hidden_text_baseline.json.

Why: the synthetic attack corpus stayed green from 3.0.0 to 3.4.0 while the
detector flagged 1,092 pages of 27 real filings, almost all visible text
(2026-10-01). A page that starts OR stops being flagged must be looked at.

Corpus:
  committed  pages/corpus/*.pdf (public domain, also pinned in CI by
             tests/test_hidden_text_regression.py)
  local      the 24 10-K filings, Starbucks FY2025, the German BGB
             (local-only; restore from the bench backup if missing)

Exit codes: 0 = matches baseline; 1 = any page newly flagged or cleared;
2 = setup error (missing PDF without --committed-only, sha256 mismatch,
no baseline entry).

  uv run python scripts/benchmark_hidden_text.py
  uv run python scripts/benchmark_hidden_text.py --committed-only
  uv run python scripts/benchmark_hidden_text.py --update-baseline [--accept-new]

--update-baseline refuses when a page becomes newly flagged unless
--accept-new is given: render each new page and confirm the text really
is hidden first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = str(Path(__file__).resolve().parents[1])
BASELINE_PATH = Path(ROOT) / "benchmark_data" / "hidden_text_baseline.json"

COMMITTED = sorted(
    p.relative_to(ROOT).as_posix() for p in (Path(ROOT) / "pages/corpus").glob("*.pdf")
)
LOCAL = sorted(
    p.relative_to(ROOT).as_posix()
    for p in (Path(ROOT) / "benchmark_data/.financial_pdfs").glob("*.pdf")
) + [
    "docs_internal/sample_pdfs/financial/starbucks_2025ar.pdf",
    "docs_internal/sample_pdfs/german/BGB.pdf",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_pdf(path: Path) -> dict[str, Any]:
    """Flagged pages (1-indexed) and reason counts for one PDF."""
    from pdf_mcp.backend.page import open_document
    from pdf_mcp.content_trust import _scan_page_geometry

    doc = open_document(str(path))
    flagged: list[int] = []
    reasons: Counter[str] = Counter()
    try:
        pages = doc.page_count
        for i in range(pages):
            spans = _scan_page_geometry(doc[i], i)
            if spans:
                flagged.append(i + 1)
                for s in spans:
                    reasons.update(s["reasons"])
    finally:
        doc.close()
    return {
        "sha256": _sha256(path),
        "pages": pages,
        "flagged": flagged,
        "reasons": dict(sorted(reasons.items())),
    }


def load_baseline() -> dict[str, Any]:
    with open(BASELINE_PATH, encoding="utf-8") as f:
        return json.load(f)


def compare(
    baseline: dict[str, Any], results: dict[str, dict[str, Any]]
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """(newly flagged pages, cleared pages) per document."""
    regressions: dict[str, list[int]] = {}
    improvements: dict[str, list[int]] = {}
    for rel, got in results.items():
        before = set(baseline["docs"][rel]["flagged"])
        after = set(got["flagged"])
        if after - before:
            regressions[rel] = sorted(after - before)
        if before - after:
            improvements[rel] = sorted(before - after)
    return regressions, improvements


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--committed-only", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--accept-new", action="store_true")
    args = ap.parse_args(argv)

    corpus = COMMITTED if args.committed_only else COMMITTED + LOCAL
    missing = [rel for rel in corpus if not (Path(ROOT) / rel).is_file()]
    if missing:
        print(f"setup error: missing PDFs (use --committed-only): {missing}")
        return 2

    results: dict[str, dict[str, Any]] = {}
    for rel in corpus:
        results[rel] = scan_pdf(Path(ROOT) / rel)
        r = results[rel]
        print(f"{rel}: {len(r['flagged'])}/{r['pages']} flagged {r['reasons']}")

    baseline = (
        load_baseline() if BASELINE_PATH.is_file() else {"version": 1, "docs": {}}
    )
    unknown = [rel for rel in results if rel not in baseline["docs"]]

    if args.update_baseline:
        known = {rel: res for rel, res in results.items() if rel not in unknown}
        regressions, _ = compare(baseline, known)
        if regressions and not args.accept_new:
            print(f"refusing: newly flagged pages {regressions};")
            print("render each, confirm the text is hidden, then --accept-new")
            return 1
        baseline["docs"].update(results)
        baseline["docs"] = dict(sorted(baseline["docs"].items()))
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(baseline, f, indent=1)
            f.write("\n")
        print(f"baseline written: {BASELINE_PATH.relative_to(ROOT)}")
        return 0

    if unknown:
        print(f"setup error: no baseline entry for {unknown}")
        return 2
    changed = [
        rel
        for rel, res in results.items()
        if res["sha256"] != baseline["docs"][rel]["sha256"]
    ]
    if changed:
        print(f"setup error: sha256 mismatch (PDF changed): {changed}")
        return 2

    regressions, improvements = compare(baseline, results)
    total = sum(len(r["flagged"]) for r in results.values())
    print(f"\nflagged pages: {total} across {len(results)} PDFs")
    if regressions:
        print(f"FAIL newly flagged: {regressions}")
    if improvements:
        print(f"FAIL cleared (verify, then --update-baseline): {improvements}")
    if regressions or improvements:
        return 1
    print("PASS: matches baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
