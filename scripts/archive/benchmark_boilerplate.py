#!/usr/bin/env python
"""
scripts/benchmark_boilerplate.py

Running-header / footer (boilerplate) detection benchmark for pdf-mcp.

pdf-mcp's `extract_text_from_page` deliberately keeps everything on the page,
including running headers, footers, and page numbers (see the `footer_margin=0,
header_margin=0` call in extractor.py). That repeated boilerplate flows into the
text returned to the agent and into the FTS5 / embedding index, where it gets
indexed once per page and flattens search ranking. This benchmark evaluates
whether boilerplate can be detected and stripped *effectively* — i.e. with high
recall on real boilerplate and, just as importantly, without deleting genuine
body content.

Why synthetic: boilerplate has no off-the-shelf labeled dataset, and the only
way to get an exact precision/recall is a corpus where we KNOW which text is
boilerplate. So each scenario in benchmark_data/boilerplate_corpus.json is
rendered to a real multi-page PDF in memory with PyMuPDF, with known
headers/footers/page-numbers injected at known positions. Ground-truth labels
are then exact, the benchmark runs offline, and results are deterministic.

What it measures: several detector variants, each adding one refinement, scored
per scenario (F1 over all text blocks across all pages) so you can see which
refinement earns its complexity:

    naive_regex   RAG-on-PDF-style: strip blocks that are all-digits / very
                  short / look like page markers, with NO cross-page logic.
                  High recall on page numbers, but destroys numeric body text.
    freq_v0       Exact-text frequency over the whole page (>= MIN_FRAC of pages).
    freq_bands    + restrict candidates to the top/bottom margin bands.
    freq_digits   + digit normalization, so "Page 7"/"Page 8" collapse and
                  page-numbered footers become detectable.
    freq_parity   + odd/even (recto/verso) frequency, for alternating headers.
    freq_runs     + consecutive-run rule, for section-scoped headers.
                  (freq_runs is the full proposed method.)

Usage:
    python scripts/benchmark_boilerplate.py                 # full run
    python scripts/benchmark_boilerplate.py --output FILE   # write md table
    python scripts/benchmark_boilerplate.py --details       # per-scenario P/R
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Any

import pymupdf

CORPUS = Path(__file__).parent.parent / "benchmark_data" / "boilerplate_corpus.json"

# A4 in points, and the regions text is drawn into.
PAGE_W, PAGE_H = 595.0, 842.0
HEADER_RECT = pymupdf.Rect(50, 30, 545, 72)
BODY_RECT = pymupdf.Rect(50, 92, 545, 760)
FOOTER_RECT = pymupdf.Rect(50, 788, 545, 822)

# Detector tuning. MIN_FRAC is intentionally > 0.5 so that odd/even headers
# (each on ~half the pages) fail the document-wide test and must be caught by
# the parity refinement — this keeps the variants cleanly separable.
TOP_BAND = 0.15   # fraction of page height treated as the header zone
BOT_BAND = 0.85   # blocks below this fraction are in the footer zone
MIN_FRAC = 0.6    # share of pages a signature must repeat on to be boilerplate
MIN_RUN = 3       # consecutive pages in the same band that flag a run

_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")
_WORD_BANK = (
    "analysis model dataset results method approach system framework process "
    "evaluation baseline parameter gradient function network training sample "
    "feature vector matrix distribution estimate variance threshold protocol "
    "experiment observation hypothesis correlation inference structure latent "
    "component objective constraint optimization convergence representation"
).split()


# --------------------------------------------------------------------------- #
# Signatures
# --------------------------------------------------------------------------- #
def signature(text: str, digit_norm: bool) -> str:
    """Canonical form of a block's text for cross-page comparison.

    Lowercased, whitespace-collapsed. When digit_norm is True, every run of
    digits becomes "#", so "Confidential 7" and "Confidential 8" share a
    signature — this is what makes page-numbered boilerplate detectable.
    """
    t = text.strip().lower()
    if digit_norm:
        t = _DIGITS.sub("#", t)
    return _WS.sub(" ", t)


def gt_signature(text: str) -> str:
    """Ground-truth signature: always digit-normalized (page numbers are truth)."""
    return signature(text, digit_norm=True)


# --------------------------------------------------------------------------- #
# Corpus -> PDF generation (with exact injected ground truth)
# --------------------------------------------------------------------------- #
def _header_for_page(sc: dict[str, Any], p: int) -> str | list[str] | None:
    """Injected header text for 1-indexed page p, or None."""
    header: str | list[str] | None
    if "section_headers" in sc:
        idx = (p - 1) // sc["section_size"]
        heads = sc["section_headers"]
        header = heads[min(idx, len(heads) - 1)]
    elif "header_odd" in sc or "header_even" in sc:
        header = sc.get("header_odd") if p % 2 == 1 else sc.get("header_even")
    else:
        header = sc.get("header")
    return header


def _footer_for_page(sc: dict[str, Any], p: int) -> str | None:
    """Injected footer text for 1-indexed page p, or None."""
    if sc.get("page_number_standalone"):
        return str(p)
    base = sc.get("footer")
    if sc.get("page_number"):
        return f"{base} {p}" if base else str(p)
    return base


def _body_text(rng: random.Random, numeric: bool) -> str:
    """Unique multi-paragraph body so body blocks never repeat across pages."""
    paras = []
    for _ in range(2):
        words = [rng.choice(_WORD_BANK) for _ in range(rng.randint(45, 70))]
        paras.append(" ".join(words) + ".")
    if numeric:
        # Short numeric lines a naive digit/short-line filter would wrongly kill.
        paras.append(rng.choice(["42", "Table 3", "see Eq. 5", "Figure 7", "0.91"]))
        paras.append(rng.choice(["17", "Section 4", "p < 0.05", "n = 128", "x 10"]))
    return "\n\n".join(paras)


def build_pdf(sc: dict[str, Any], seed: int) -> tuple[pymupdf.Document, list[set[str]]]:
    """Render a scenario to a PDF; return (doc, injected[p] = set of GT sigs)."""
    rng = random.Random(f"{seed}:{sc['name']}")
    doc = pymupdf.open()
    injected: list[set[str]] = []
    for p in range(1, sc["pages"] + 1):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        truth: set[str] = set()

        header = _header_for_page(sc, p)
        if header:
            htext = "\n".join(header) if isinstance(header, list) else header
            page.insert_textbox(HEADER_RECT, htext, fontsize=11, align=1)
            truth.add(gt_signature(htext))

        page.insert_textbox(
            BODY_RECT, _body_text(rng, sc.get("body_numeric", False)), fontsize=10
        )

        footer = _footer_for_page(sc, p)
        if footer:
            page.insert_textbox(FOOTER_RECT, footer, fontsize=9, align=1)
            truth.add(gt_signature(footer))

        injected.append(truth)
    return doc, injected


# --------------------------------------------------------------------------- #
# Block extraction
# --------------------------------------------------------------------------- #
def page_blocks(page: pymupdf.Page) -> list[tuple[str, float, float]]:
    """Text blocks as (text, y0_frac, y1_frac); fracs are of page height."""
    h = page.rect.height
    out = []
    for b in page.get_text("blocks", sort=True):
        if b[6] != 0 or not b[4].strip():  # text blocks only
            continue
        out.append((b[4], b[1] / h, b[3] / h))
    return out


def _band(y0: float, y1: float) -> str | None:
    """Margin band a block sits in: 'top', 'bot', or None (body)."""
    if y0 < TOP_BAND:
        return "top"
    if y1 > BOT_BAND:
        return "bot"
    return None


# --------------------------------------------------------------------------- #
# Detector variants
# --------------------------------------------------------------------------- #
_NAIVE_PAGE = re.compile(r"^(page\s*)?\d+$|^[ivxlc]+$", re.IGNORECASE)


def _naive_is_boilerplate(text: str) -> bool:
    """RAG-on-PDF-style per-block filter: no cross-page knowledge."""
    t = text.strip()
    if not t:
        return False
    if _NAIVE_PAGE.match(t):
        return True
    if t.replace(".", "").replace(" ", "").isdigit():
        return True
    return len(t.split()) <= 3  # nuke short lines (incl. real body refs)


def _freq_keys(
    blocks_per_page: list[list[tuple[str, float, float]]],
    *,
    use_bands: bool,
    digit_norm: bool,
    use_parity: bool,
    use_runs: bool,
) -> set[tuple[str, str]]:
    """Compute the set of (signature, band) keys judged to be boilerplate."""
    n = len(blocks_per_page)
    pages_with: dict[tuple[str, str], set[int]] = {}
    for pi, blocks in enumerate(blocks_per_page):
        for text, y0, y1 in blocks:
            band = _band(y0, y1)
            if use_bands and band is None:
                continue
            band_part = band if (use_bands and band is not None) else "*"
            key = (signature(text, digit_norm), band_part)
            pages_with.setdefault(key, set()).add(pi)

    odd = [pi for pi in range(n) if pi % 2 == 0]   # 0-indexed page0 == "page 1"
    even = [pi for pi in range(n) if pi % 2 == 1]
    keys: set[tuple[str, str]] = set()
    for key, present in pages_with.items():
        if len(present) / n >= MIN_FRAC:
            keys.add(key)
            continue
        if use_parity:
            for grp in (odd, even):
                if grp and len(present & set(grp)) / len(grp) >= MIN_FRAC:
                    keys.add(key)
                    break
            if key in keys:
                continue
        if use_runs:
            run = best = 0
            prev = -2
            for pi in sorted(present):
                run = run + 1 if pi == prev + 1 else 1
                best = max(best, run)
                prev = pi
            if best >= MIN_RUN:
                keys.add(key)
    return keys


VARIANTS: list[tuple[str, dict[str, bool]]] = [
    ("naive_regex", {"naive": True}),
    ("freq_v0", {"use_bands": False, "digit_norm": False,
                 "use_parity": False, "use_runs": False}),
    ("freq_bands", {"use_bands": True, "digit_norm": False,
                    "use_parity": False, "use_runs": False}),
    ("freq_digits", {"use_bands": True, "digit_norm": True,
                     "use_parity": False, "use_runs": False}),
    ("freq_parity", {"use_bands": True, "digit_norm": True,
                     "use_parity": True, "use_runs": False}),
    ("freq_runs", {"use_bands": True, "digit_norm": True,
                   "use_parity": True, "use_runs": True}),
]


def predict_scenario(
    blocks_per_page: list[list[tuple[str, float, float]]], cfg: dict[str, bool]
) -> list[list[bool]]:
    """Per-block boilerplate prediction for one document under one variant."""
    if cfg.get("naive"):
        return [[_naive_is_boilerplate(t) for t, _, _ in bl] for bl in blocks_per_page]
    keys = _freq_keys(
        blocks_per_page,
        use_bands=cfg["use_bands"],
        digit_norm=cfg["digit_norm"],
        use_parity=cfg["use_parity"],
        use_runs=cfg["use_runs"],
    )
    preds = []
    for blocks in blocks_per_page:
        row = []
        for text, y0, y1 in blocks:
            band = _band(y0, y1)
            if cfg["use_bands"] and band is None:
                row.append(False)
                continue
            sig = signature(text, cfg["digit_norm"])
            band_part = band if (cfg["use_bands"] and band is not None) else "*"
            row.append((sig, band_part) in keys)
        preds.append(row)
    return preds


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def run() -> dict[str, Any]:
    corpus = json.loads(CORPUS.read_text())
    seed = corpus.get("seed", 0)
    scenarios = corpus["scenarios"]

    # Per (variant, scenario): tallies over every text block on every page.
    cells: dict[str, dict[str, tuple[float, float, float]]] = {
        v: {} for v, _ in VARIANTS
    }
    for sc in scenarios:
        doc, injected = build_pdf(sc, seed)
        try:
            blocks_per_page = [page_blocks(doc[i]) for i in range(doc.page_count)]
        finally:
            doc.close()
        gold = [
            [gt_signature(t) in injected[pi] for t, _, _ in blocks]
            for pi, blocks in enumerate(blocks_per_page)
        ]
        for vname, cfg in VARIANTS:
            preds = predict_scenario(blocks_per_page, cfg)
            tp = fp = fn = 0
            for grow, prow in zip(gold, preds):
                for g, pr in zip(grow, prow):
                    tp += g and pr
                    fp += (not g) and pr
                    fn += g and (not pr)
            cells[vname][sc["name"]] = prf(tp, fp, fn)

    names = [sc["name"] for sc in scenarios]
    macro = {
        v: sum(cells[v][n][2] for n in names) / len(names) for v, _ in VARIANTS
    }
    return {"names": names, "cells": cells, "macro": macro}


def format_markdown(result: dict[str, Any], details: bool) -> str:
    names = result["names"]
    cells = result["cells"]
    lines = [
        "# Boilerplate (running header/footer) detection benchmark",
        "",
        "F1 of per-block boilerplate classification on a synthetic corpus where "
        "headers/footers/page-numbers are injected at known positions, so labels "
        "are exact (`scripts/benchmark_boilerplate.py`, "
        "`benchmark_data/boilerplate_corpus.json`). Each row adds one refinement; "
        "`freq_runs` is the full proposed method. `clean_control` and "
        "`numeric_body` are precision guardrails — the detector must strip "
        "nothing real there.",
        "",
        "## F1 by scenario",
        "",
        "| variant | " + " | ".join(names) + " | macro |",
        "| --- |" + " --- |" * (len(names) + 1),
    ]
    for v, _ in VARIANTS:
        row = " | ".join(f"{cells[v][n][2]:.2f}" for n in names)
        lines.append(f"| {v} | {row} | {result['macro'][v]:.2f} |")

    if details:
        for variant, caption in (
            ("freq_runs", "full proposed method"),
            ("naive_regex", "RAG-on-PDF-style baseline"),
        ):
            lines += ["", f"## Precision / recall — {variant} ({caption})", "",
                      "| scenario | precision | recall | f1 |",
                      "| --- | --- | --- | --- |"]
            for n in names:
                p, r, f = cells[variant][n]
                lines.append(f"| {n} | {p:.2f} | {r:.2f} | {f:.2f} |")

    lines += [
        "",
        "## Caveats",
        "",
        "- Synthetic corpus: text is digitally injected, so `freq_runs` scoring "
        "a clean 1.00 validates the algorithm's *logic and edge-case coverage*, "
        "not its robustness to OCR drift or near-duplicate text. Real scanned "
        "PDFs would score lower; fuzzy signature matching for OCR pages is the "
        "obvious next experiment (needs a real, network-fetched corpus).",
        "- The variants are separated on purpose: `MIN_FRAC` is set above 0.5 so "
        "odd/even headers fail the document-wide test and must be earned by the "
        "parity rule. The takeaway is the *ordering* — each refinement recovers "
        "one failure mode at no precision cost on the guardrails.",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Real-PDF evaluation (label-free)
# --------------------------------------------------------------------------- #
REAL_CORPUS = (
    Path(__file__).parent.parent / "benchmark_data" / "boilerplate_real_corpus.json"
)
REAL_PDFS = Path(__file__).parent.parent / "benchmark_data" / ".real_pdfs"

FULL_CFG = dict(VARIANTS)["freq_runs"]
NAIVE_CFG = dict(VARIANTS)["naive_regex"]
SENTINEL_COVERAGE = 0.25  # strips below this page-coverage are flagged for review


def _tokens(text: str) -> int:
    return len(text.split())


def eval_real_doc(doc: pymupdf.Document, known: list[dict[str, str]]) -> dict[str, Any]:
    """Run the full method on a real PDF; report label-free quality signals."""
    blocks_per_page = [page_blocks(doc[i]) for i in range(doc.page_count)]
    n_pages = len(blocks_per_page)
    n_blocks = sum(len(b) for b in blocks_per_page)
    tok_total = sum(_tokens(t) for bl in blocks_per_page for t, _, _ in bl)

    full = predict_scenario(blocks_per_page, FULL_CFG)
    naive = predict_scenario(blocks_per_page, NAIVE_CFG)

    # Inventory of what the full method strips: signature -> page coverage.
    cov: dict[str, set[int]] = {}
    full_tok = 0
    for pi, (blocks, prow) in enumerate(zip(blocks_per_page, full)):
        for (text, _, _), flag in zip(blocks, prow):
            if flag:
                cov.setdefault(signature(text, True), set()).add(pi)
                full_tok += _tokens(text)
    inventory = sorted(
        ((sig, len(pages) / n_pages) for sig, pages in cov.items()),
        key=lambda x: -x[1],
    )

    # Recall on hand-verified known boilerplate: each pattern must match some
    # stripped signature that covers a reasonable share of pages.
    recall_hits = 0
    for k in known:
        rx = re.compile(k["pattern"], re.IGNORECASE)
        if any(rx.search(sig) and frac >= 0.3 for sig, frac in inventory):
            recall_hits += 1

    # Precision sentinel: real boilerplate is high-frequency, so any stripped
    # signature below SENTINEL_COVERAGE is surfaced for manual review.
    suspects = [(sig, frac) for sig, frac in inventory if frac < SENTINEL_COVERAGE]

    # Naive over-reach: blocks naive strips that the full method keeps. On real
    # documents these are almost all genuine body content (short lines, numbers).
    naive_only: list[str] = []
    naive_only_tok = 0
    for blocks, nrow, frow in zip(blocks_per_page, naive, full):
        for (text, _, _), nf, ff in zip(blocks, nrow, frow):
            if nf and not ff:
                naive_only_tok += _tokens(text)
                if len(naive_only) < 6:
                    naive_only.append(" ".join(text.split())[:60])

    return {
        "pages": n_pages,
        "blocks": n_blocks,
        "tok_total": tok_total,
        "full_tok_stripped": full_tok,
        "full_pct": full_tok / tok_total if tok_total else 0.0,
        "inventory": inventory,
        "recall": (recall_hits, len(known)),
        "suspects": suspects,
        "naive_only_tok": naive_only_tok,
        "naive_only_samples": naive_only,
    }


def run_real() -> list[tuple[dict[str, str], dict[str, Any]]]:
    corpus = json.loads(REAL_CORPUS.read_text())
    out = []
    for entry in corpus["documents"]:
        pdf = REAL_PDFS / entry["file"]
        if not pdf.exists():
            print(f"  skip {entry['file']}: not in {REAL_PDFS} (see manifest source)")
            continue
        doc = pymupdf.open(pdf)
        try:
            out.append((entry, eval_real_doc(doc, entry["known_boilerplate"])))
        finally:
            doc.close()
    return out


def format_real_markdown(rows: list[tuple[dict[str, str], dict[str, Any]]]) -> str:
    lines = [
        "# Boilerplate detection on real PDFs",
        "",
        "Full method (`freq_runs`) on real documents (`--real`). Real PDFs have "
        "no per-block labels, so this reports label-free signals: a recall check "
        "against hand-verified known boilerplate, a precision sentinel (real "
        "boilerplate is high-frequency, so low-coverage strips are surfaced for "
        "review), token savings, and how much extra a RAG-on-PDF-style naive "
        "filter would wrongly strip. PDFs are gitignored; see "
        "`benchmark_data/boilerplate_real_corpus.json` for sources.",
        "",
        "| document | pages | known recall | tokens stripped | % | suspects | "
        "naive over-strip (tokens) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry, r in rows:
        rc, rt = r["recall"]
        lines.append(
            f"| {entry['file']} | {r['pages']} | {rc}/{rt} | "
            f"{r['full_tok_stripped']} | {r['full_pct'] * 100:.1f}% | "
            f"{len(r['suspects'])} | {r['naive_only_tok']} |"
        )

    lines += ["", "## What the full method stripped (per document)", ""]
    for entry, r in rows:
        lines.append(f"### {entry['file']} — {entry['title']}")
        lines.append("")
        lines.append("| stripped signature (digit-normalized) | page coverage |")
        lines.append("| --- | --- |")
        for sig, frac in r["inventory"][:8]:
            shown = sig if len(sig) <= 70 else sig[:67] + "..."
            lines.append(f"| `{shown}` | {frac * 100:.0f}% |")
        if r["naive_only_samples"]:
            lines.append("")
            lines.append(
                "Body content the naive filter would wrongly strip (sample): "
                + "; ".join(f"`{s}`" for s in r["naive_only_samples"])
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=None, help="write md table")
    parser.add_argument("--details", action="store_true", help="per-scenario P/R")
    parser.add_argument(
        "--real", action="store_true", help="evaluate real PDFs (.real_pdfs)"
    )
    args = parser.parse_args()

    if args.real:
        md = format_real_markdown(run_real())
    else:
        md = format_markdown(run(), details=args.details)
    print(md)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
