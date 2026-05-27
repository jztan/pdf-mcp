#!/usr/bin/env python
"""
scripts/benchmark_excerpt_quality.py

Directional signal: excerpt_style="paragraph" vs "snippet" quality.

Measures excerpt containment rate — whether the returned excerpt
contains a known answer substring for each (query, page) pair.
Tests across multiple PDF types: academic prose (Transformer, GPT-3),
survey papers (GNN review, LLM survey), and structured bullet-list
documents (AWS exam guide).

Treat results as a go/no-go signal, not a publishable benchmark.
Containment is a weak proxy: it catches wrong-block failures but
can't distinguish "right block, noisy context" from "right block,
clean context."

    python scripts/benchmark_excerpt_quality.py

Gate: paragraph >= snippet containment, zero regressions.

Always exits 0 (informational report, no CI gate).
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pdf_mcp.server import pdf_search  # noqa: E402

# Each PDF has a path (local or URL) and a list of queries.
# answer: substring that MUST appear in a good excerpt.
# category: "prose" or "structured"
PDFS = {
    "transformer": {
        "path": "docs_internal/1706.03762v7.pdf",
        "title": "Attention Is All You Need",
        "queries": [
            {
                "query": "dropout rate",
                "page": 8,
                "answer": "apply dropout",
                "category": "prose",
            },
            {
                "query": "why self-attention is better than recurrent layers",
                "page": 7,
                "answer": "self-attention",
                "category": "prose",
            },
            {
                "query": "scale dot products by 1 over sqrt dk",
                "page": 4,
                "answer": "dot product",
                "category": "prose",
            },
            {
                "query": "positional encoding sinusoidal",
                "page": 6,
                "answer": "positional encoding",
                "category": "prose",
            },
            {
                "query": "multi-head attention parallel heads",
                "page": 5,
                "answer": "parallel attention",
                "category": "prose",
            },
            {
                "query": "label smoothing during training",
                "page": 8,
                "answer": "label smoothing",
                "category": "prose",
            },
            {
                "query": "encoder decoder stacks",
                "page": 3,
                "answer": "encoder",
                "category": "prose",
            },
            {
                "query": "BLEU score English to German",
                "page": 8,
                "answer": "BLEU",
                "category": "structured",
            },
            {
                "query": "Scaled Dot-Product Attention",
                "page": 4,
                "answer": "attention",
                "category": "structured",
            },
            {
                "query": "training data sentence pairs",
                "page": 8,
                "answer": "training",
                "category": "structured",
            },
        ],
    },
    "gpt3": {
        "path": "https://arxiv.org/pdf/2005.14165",
        "title": "GPT-3: Language Models are Few-Shot Learners",
        "queries": [
            {
                "query": "few-shot performance on LAMBADA",
                "page": 12,
                "answer": "LAMBADA",
                "category": "structured",
            },
            {
                "query": "bias fairness stereotyped content",
                "page": 36,
                "answer": "bias",
                "category": "prose",
            },
            {
                "query": "limitations of the pretraining objective",
                "page": 34,
                "answer": "limitation",
                "category": "prose",
            },
            {
                "query": "model size parameters 175 billion",
                "page": 8,
                "answer": "175",
                "category": "structured",
            },
            {
                "query": "data contamination benchmark overlap",
                "page": 32,
                "answer": "contamination",
                "category": "prose",
            },
        ],
    },
    "gnn_review": {
        "path": "https://arxiv.org/pdf/1812.08434",
        "title": "Graph Neural Networks: A Review",
        "queries": [
            {
                "query": "spectral methods graph convolution",
                "page": 5,
                "answer": "spectral",
                "category": "prose",
            },
            {
                "query": "pooling modules graph coarsening",
                "page": 10,
                "answer": "pooling",
                "category": "prose",
            },
            {
                "query": "static dynamic graphs time information",
                "page": 3,
                "answer": "dynamic graph",
                "category": "prose",
            },
            {
                "query": "message passing neural network",
                "page": 7,
                "answer": "message",
                "category": "prose",
            },
            {
                "query": "graph attention network",
                "page": 7,
                "answer": "attention",
                "category": "prose",
            },
        ],
    },
    "llm_survey": {
        "path": "https://arxiv.org/pdf/2303.18223",
        "title": "A Survey of Large Language Models",
        "queries": [
            {
                "query": "scaling laws emergent abilities",
                "page": 5,
                "answer": "scaling",
                "category": "prose",
            },
            {
                "query": "mixed precision training FP16",
                "page": 30,
                "answer": "FP16",
                "category": "prose",
            },
            {
                "query": "reinforcement learning from human feedback",
                "page": 38,
                "answer": "human feedback",
                "category": "prose",
            },
            {
                "query": "in-context learning demonstration examples",
                "page": 45,
                "answer": "in-context learning",
                "category": "prose",
            },
            {
                "query": "instruction tuning fine-tuning",
                "page": 35,
                "answer": "instruction",
                "category": "prose",
            },
        ],
    },
    "aws_exam": {
        "path": "docs_internal/AWS AIP-C01 107.pdf",
        "title": "AWS AI Practitioner Exam Guide",
        "queries": [
            {
                "query": "prompt engineering best practices",
                "page": 50,
                "answer": "prompt",
                "category": "structured",
            },
            {
                "query": "responsible AI fairness toxicity",
                "page": 99,
                "answer": "responsible",
                "category": "structured",
            },
            {
                "query": "Amazon Bedrock Knowledge Bases",
                "page": 11,
                "answer": "Bedrock",
                "category": "structured",
            },
            {
                "query": "model evaluation metrics accuracy",
                "page": 28,
                "answer": "evaluation",
                "category": "structured",
            },
            {
                "query": "generative AI use cases business",
                "page": 114,
                "answer": "generative",
                "category": "structured",
            },
        ],
    },
}


def _resolve_pdf_path(path: str) -> str:
    """Resolve local paths relative to project root; pass URLs through."""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    resolved = Path(__file__).parent.parent / path
    if not resolved.exists():
        print(f"WARNING: local PDF not found: {resolved}")
    return str(resolved)


def run_benchmark() -> dict:
    """Run the excerpt quality benchmark across all PDFs."""
    results = {"snippet": [], "paragraph": []}
    all_queries = []

    for pdf_key, pdf_info in PDFS.items():
        pdf_path = _resolve_pdf_path(pdf_info["path"])
        print(f"  Benchmarking: {pdf_info['title']} ...", flush=True)

        for gt in pdf_info["queries"]:
            gt_with_source = {**gt, "pdf": pdf_key}
            all_queries.append(gt_with_source)

            for style in ("snippet", "paragraph"):
                r = pdf_search(
                    pdf_path,
                    gt["query"],
                    excerpt_style=style,
                    max_results=5,
                )
                matches = r.get("matches", [])
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
                        "pdf": pdf_key,
                        "contains_answer": contains,
                        "excerpt_len": excerpt_len,
                        "excerpt_preview": excerpt_preview,
                    }
                )

    return results


def _rate(items: list) -> str:
    """Format containment rate as 'N/M (P%)'."""
    total = len(items)
    if total == 0:
        return "0/0"
    contained = sum(1 for r in items if r["contains_answer"])
    return f"{contained}/{total} ({contained / total:.0%})"


def print_report(results: dict) -> None:
    """Print a formatted comparison report."""
    total_queries = len(results["snippet"])
    pdf_count = len(set(r["pdf"] for r in results["snippet"]))

    print("=" * 80)
    print("EXCERPT QUALITY BENCHMARK")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"PDFs: {pdf_count}  Queries: {total_queries}")
    print("=" * 80)

    # Per-query table
    print(
        f"\n{'PDF':<12} {'Query':<45} {'Pg':>3}"
        f"  {'Cat':<10} {'Snip':>4} {'Para':>4}"
        f"  {'S.len':>5} {'P.len':>5}"
    )
    print("-" * 100)

    snippet_wins = 0
    paragraph_wins = 0
    ties = 0

    for i in range(total_queries):
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
            f"{s['pdf']:<12} {s['query']:<45} {s['page']:>3}"
            f"  {s['category']:<10} {s_mark:>4} {p_mark:>4}"
            f"  {s['excerpt_len']:>5} {p['excerpt_len']:>5}"
        )

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    for style in ("snippet", "paragraph"):
        items = results[style]
        prose = [r for r in items if r["category"] == "prose"]
        structured = [r for r in items if r["category"] == "structured"]
        avg_len = (
            sum(r["excerpt_len"] for r in items) / len(items) if items else 0
        )

        print(f"\n{style.upper()}")
        print(f"  Overall containment:    {_rate(items)}")
        print(f"  Prose containment:      {_rate(prose)}")
        print(f"  Structured containment: {_rate(structured)}")
        print(f"  Avg excerpt length:     {avg_len:.0f} chars")

        lengths = sorted(
            r["excerpt_len"] for r in items if r["excerpt_len"] > 0
        )
        if lengths:
            buckets = {
                "<100": 0,
                "100-299": 0,
                "300-499": 0,
                "500-999": 0,
                "1000+": 0,
            }
            for length in lengths:
                if length < 100:
                    buckets["<100"] += 1
                elif length < 300:
                    buckets["100-299"] += 1
                elif length < 500:
                    buckets["300-499"] += 1
                elif length < 1000:
                    buckets["500-999"] += 1
                else:
                    buckets["1000+"] += 1
            dist = "  ".join(f"{k}:{v}" for k, v in buckets.items() if v > 0)
            print(f"  Length distribution:     {dist}")

    # Per-PDF breakdown
    print("\n" + "-" * 80)
    print("PER-PDF BREAKDOWN")
    print("-" * 80)
    pdf_keys = list(dict.fromkeys(r["pdf"] for r in results["snippet"]))
    for pdf_key in pdf_keys:
        s_items = [r for r in results["snippet"] if r["pdf"] == pdf_key]
        p_items = [r for r in results["paragraph"] if r["pdf"] == pdf_key]
        title = PDFS[pdf_key]["title"]
        print(f"  {title}: snippet {_rate(s_items)}  paragraph {_rate(p_items)}")

    print(
        f"\nHead-to-head: paragraph wins {paragraph_wins},"
        f" snippet wins {snippet_wins}, ties {ties}"
    )

    # Gate
    s_total = len(results["snippet"])
    p_total = len(results["paragraph"])
    s_rate = (
        sum(1 for r in results["snippet"] if r["contains_answer"]) / s_total
    )
    p_rate = (
        sum(1 for r in results["paragraph"] if r["contains_answer"]) / p_total
    )
    passed = p_rate >= s_rate and snippet_wins == 0
    print(
        f"\nGate (paragraph >= snippet, zero regressions):"
        f" {'PASS' if passed else 'FAIL'}"
    )
    print(f"  snippet={s_rate:.0%}  paragraph={p_rate:.0%}  regressions={snippet_wins}")

    # Regressions
    failures = []
    for i in range(total_queries):
        s = results["snippet"][i]
        p = results["paragraph"][i]
        if s["contains_answer"] and not p["contains_answer"]:
            failures.append(
                f"  REGRESSION: [{s['pdf']}] '{s['query']}'"
                f" p{s['page']} — snippet has answer, paragraph doesn't"
                f"\n    paragraph excerpt: {p['excerpt_preview']}"
            )
    if failures:
        print(f"\nREGRESSIONS ({len(failures)}):")
        for f in failures:
            print(f)


if __name__ == "__main__":
    print("Running excerpt quality benchmark...\n")
    results = run_benchmark()
    print()
    print_report(results)
