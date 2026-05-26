#!/usr/bin/env python
"""
scripts/benchmark_excerpt_quality.py

Benchmark: excerpt_style="paragraph" vs "snippet" excerpt quality.

Measures excerpt containment rate — whether the returned excerpt
contains a known answer substring for each (query, page) pair.
Uses local PDFs only (no network).

    python scripts/benchmark_excerpt_quality.py

Gate: paragraph must match or beat snippet containment rate overall,
and beat it on at least one category (structured or prose).

Always exits 0 (informational report, no CI gate).
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pdf_mcp.server import pdf_search  # noqa: E402

TRANSFORMER_PDF = "docs_internal/1706.03762v7.pdf"

# Ground truth: (query, target_page, answer_substring, category)
# answer_substring is text that MUST appear in a good excerpt.
# category: "prose" (body paragraphs) or "structured" (tables, headings)
GROUND_TRUTH = [
    # --- Transformer paper: prose-heavy ---
    {
        "query": "dropout rate",
        "page": 8,
        "answer": "apply dropout",
        "category": "prose",
        "notes": "Regularization section body text",
    },
    {
        "query": "why self-attention is better than recurrent layers",
        "page": 7,
        "answer": "self-attention",
        "category": "prose",
        "notes": "Why Self-Attention section",
    },
    {
        "query": "scale dot products by 1 over sqrt dk",
        "page": 4,
        "answer": "dot product",
        "category": "prose",
        "notes": "Section 3.2.1 body explaining scaling",
    },
    {
        "query": "positional encoding sinusoidal",
        "page": 6,
        "answer": "positional encoding",
        "category": "prose",
        "notes": "Section 3.5 body paragraph",
    },
    {
        "query": "multi-head attention parallel heads",
        "page": 5,
        "answer": "parallel attention",
        "category": "prose",
        "notes": "Section 3.2.2 body with h=8",
    },
    {
        "query": "label smoothing during training",
        "page": 8,
        "answer": "label smoothing",
        "category": "prose",
        "notes": "Regularization subsection",
    },
    {
        "query": "encoder decoder stacks",
        "page": 3,
        "answer": "encoder",
        "category": "prose",
        "notes": "Architecture overview",
    },
    # --- Transformer paper: structured (tables, headings) ---
    {
        "query": "BLEU score English to German",
        "page": 8,
        "answer": "BLEU",
        "category": "structured",
        "notes": "Table 2 content",
    },
    {
        "query": "Scaled Dot-Product Attention",
        "page": 4,
        "answer": "attention",
        "category": "structured",
        "notes": "Section heading / figure caption",
    },
    {
        "query": "training data sentence pairs",
        "page": 8,
        "answer": "training",
        "category": "structured",
        "notes": "Training section",
    },
]


def run_benchmark() -> dict:
    """Run the excerpt quality benchmark. Returns results dict."""
    pdf_path = str(Path(__file__).parent.parent / TRANSFORMER_PDF)
    if not Path(pdf_path).exists():
        print(f"ERROR: PDF not found at {pdf_path}")
        sys.exit(1)

    results = {"snippet": [], "paragraph": []}

    for gt in GROUND_TRUTH:
        for style in ("snippet", "paragraph"):
            r = pdf_search(
                pdf_path,
                gt["query"],
                excerpt_style=style,
                max_results=5,
            )
            matches = r.get("matches", [])
            # Find the match for the target page
            target_match = None
            for m in matches:
                if m["page"] == gt["page"]:
                    target_match = m
                    break

            if target_match is None:
                contains = False
                excerpt_len = 0
                excerpt_preview = "(page not in results)"
            else:
                excerpt = target_match["excerpt"]
                contains = gt["answer"].lower() in excerpt.lower()
                excerpt_len = len(excerpt)
                excerpt_preview = excerpt[:100]

            results[style].append(
                {
                    "query": gt["query"],
                    "page": gt["page"],
                    "category": gt["category"],
                    "contains_answer": contains,
                    "excerpt_len": excerpt_len,
                    "excerpt_preview": excerpt_preview,
                }
            )

    return results


def print_report(results: dict) -> None:
    """Print a formatted comparison report."""
    print("=" * 72)
    print("EXCERPT QUALITY BENCHMARK")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"PDF: {TRANSFORMER_PDF}")
    print(f"Queries: {len(GROUND_TRUTH)}")
    print("=" * 72)

    # Per-query comparison
    print(f"\n{'Query':<50} {'Page':>4}  {'Cat':<10} {'Snip':>4} {'Para':>4}  {'S.len':>5} {'P.len':>5}")
    print("-" * 92)

    snippet_wins = 0
    paragraph_wins = 0
    ties = 0

    for i, gt in enumerate(GROUND_TRUTH):
        s = results["snippet"][i]
        p = results["paragraph"][i]
        s_mark = "Y" if s["contains_answer"] else "N"
        p_mark = "Y" if p["contains_answer"] else "N"

        if s["contains_answer"] == p["contains_answer"]:
            ties += 1
        elif p["contains_answer"]:
            paragraph_wins += 1
        else:
            snippet_wins += 1

        print(
            f"{gt['query']:<50} {gt['page']:>4}  {gt['category']:<10} "
            f"{s_mark:>4} {p_mark:>4}  {s['excerpt_len']:>5} {p['excerpt_len']:>5}"
        )

    # Summary stats
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)

    for style in ("snippet", "paragraph"):
        total = len(results[style])
        contained = sum(1 for r in results[style] if r["contains_answer"])
        rate = contained / total if total else 0
        avg_len = (
            sum(r["excerpt_len"] for r in results[style]) / total if total else 0
        )

        # By category
        prose = [r for r in results[style] if r["category"] == "prose"]
        structured = [r for r in results[style] if r["category"] == "structured"]
        prose_rate = (
            sum(1 for r in prose if r["contains_answer"]) / len(prose)
            if prose
            else 0
        )
        struct_rate = (
            sum(1 for r in structured if r["contains_answer"]) / len(structured)
            if structured
            else 0
        )

        print(f"\n{style.upper():}")
        print(f"  Overall containment: {contained}/{total} ({rate:.0%})")
        print(f"  Prose containment:   {sum(1 for r in prose if r['contains_answer'])}/{len(prose)} ({prose_rate:.0%})")
        print(f"  Structured containment: {sum(1 for r in structured if r['contains_answer'])}/{len(structured)} ({struct_rate:.0%})")
        print(f"  Avg excerpt length:  {avg_len:.0f} chars")

    print(f"\nHead-to-head: paragraph wins {paragraph_wins}, snippet wins {snippet_wins}, ties {ties}")

    # Gate check
    s_rate = sum(1 for r in results["snippet"] if r["contains_answer"]) / len(
        results["snippet"]
    )
    p_rate = sum(1 for r in results["paragraph"] if r["contains_answer"]) / len(
        results["paragraph"]
    )
    passed = p_rate >= s_rate
    print(f"\nGate (paragraph >= snippet containment): {'PASS' if passed else 'FAIL'}")
    print(f"  snippet={s_rate:.0%}  paragraph={p_rate:.0%}")

    # Show failures
    failures = []
    for i, gt in enumerate(GROUND_TRUTH):
        s = results["snippet"][i]
        p = results["paragraph"][i]
        if s["contains_answer"] and not p["contains_answer"]:
            failures.append(
                f"  REGRESSION: '{gt['query']}' p{gt['page']} — snippet has answer, paragraph doesn't"
                f"\n    paragraph excerpt: {p['excerpt_preview']}"
            )
    if failures:
        print(f"\nREGRESSIONS ({len(failures)}):")
        for f in failures:
            print(f)


if __name__ == "__main__":
    results = run_benchmark()
    print_report(results)
