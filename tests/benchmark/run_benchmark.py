#!/usr/bin/env python3
"""
Benchmark runner for comparing PDF reading with and without pdf-mcp.

Usage:
    # Run a single test
    python run_benchmark.py --test 1.1 --pdf /path/to/annual_report.pdf

    # Run all tests in a category
    python run_benchmark.py --category "Table Extraction" --pdf-dir /path/to/test_pdfs/

    # Run with both modes and compare
    python run_benchmark.py --test 1.1 --pdf /path/to/report.pdf --mode both

    # Export results to CSV for blog post
    python run_benchmark.py --export results.csv
"""

import argparse
import json
import time
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class TestResult:
    test_id: str
    test_name: str
    mode: str  # "native" or "pdf-mcp"
    pdf_path: str
    prompt: str
    response: str = ""
    accuracy_score: int = 0  # 0-5, human-rated
    completeness_pct: int = 0  # 0-100
    token_usage: int = 0
    latency_seconds: float = 0.0
    structured_output: bool = False
    error: str = ""
    notes: str = ""


@dataclass
class BenchmarkSuite:
    results: list[TestResult] = field(default_factory=list)

    def add_result(self, result: TestResult) -> None:
        self.results.append(result)

    def export_csv(self, path: str) -> None:
        """Export results to CSV for easy analysis."""
        import csv

        if not self.results:
            print("No results to export.")
            return

        fieldnames = list(asdict(self.results[0]).keys())
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in self.results:
                writer.writerow(asdict(r))
        print(f"Exported {len(self.results)} results to {path}")

    def export_markdown(self, path: str) -> None:
        """Export results as a markdown comparison table for blog posts."""
        if not self.results:
            print("No results to export.")
            return

        # Group results by test_id
        by_test: dict[str, dict[str, TestResult]] = {}
        for r in self.results:
            if r.test_id not in by_test:
                by_test[r.test_id] = {}
            by_test[r.test_id][r.mode] = r

        lines = [
            "# Benchmark Results: pdf-mcp vs Native PDF Reading\n",
            "| Test | Native Accuracy | pdf-mcp Accuracy | Native Tokens | pdf-mcp Tokens | Native Latency | pdf-mcp Latency | Winner |",
            "|------|----------------|-----------------|---------------|----------------|----------------|-----------------|--------|",
        ]

        for test_id in sorted(by_test.keys()):
            modes = by_test[test_id]
            native = modes.get("native")
            mcp = modes.get("pdf-mcp")

            native_acc = f"{native.accuracy_score}/5" if native else "N/A"
            mcp_acc = f"{mcp.accuracy_score}/5" if mcp else "N/A"
            native_tok = f"{native.token_usage:,}" if native else "N/A"
            mcp_tok = f"{mcp.token_usage:,}" if mcp else "N/A"
            native_lat = f"{native.latency_seconds:.1f}s" if native else "N/A"
            mcp_lat = f"{mcp.latency_seconds:.1f}s" if mcp else "N/A"

            # Determine winner
            winner = "—"
            if native and mcp:
                if native.accuracy_score > mcp.accuracy_score:
                    winner = "Native"
                elif mcp.accuracy_score > native.accuracy_score:
                    winner = "pdf-mcp"
                elif native.token_usage > mcp.token_usage:
                    winner = "pdf-mcp (tokens)"
                elif mcp.token_usage > native.token_usage:
                    winner = "Native (tokens)"
                else:
                    winner = "Tie"

            test_name = (native or mcp).test_name
            lines.append(
                f"| {test_id} {test_name} | {native_acc} | {mcp_acc} | "
                f"{native_tok} | {mcp_tok} | {native_lat} | {mcp_lat} | {winner} |"
            )

        lines.append("")
        with open(path, "w") as f:
            f.write("\n".join(lines))
        print(f"Exported markdown to {path}")

    def summary(self) -> str:
        """Print a quick summary of results."""
        native_wins = 0
        mcp_wins = 0
        ties = 0

        by_test: dict[str, dict[str, TestResult]] = {}
        for r in self.results:
            if r.test_id not in by_test:
                by_test[r.test_id] = {}
            by_test[r.test_id][r.mode] = r

        for test_id, modes in by_test.items():
            native = modes.get("native")
            mcp = modes.get("pdf-mcp")
            if native and mcp:
                if native.accuracy_score > mcp.accuracy_score:
                    native_wins += 1
                elif mcp.accuracy_score > native.accuracy_score:
                    mcp_wins += 1
                else:
                    ties += 1

        total = native_wins + mcp_wins + ties
        return (
            f"\n=== Benchmark Summary ===\n"
            f"Total tests compared: {total}\n"
            f"Native wins:  {native_wins}\n"
            f"pdf-mcp wins: {mcp_wins}\n"
            f"Ties:         {ties}\n"
        )


def load_test_prompts() -> dict:
    """Load test definitions from test_prompts.json."""
    prompts_path = Path(__file__).parent / "test_prompts.json"
    with open(prompts_path) as f:
        return json.load(f)


def record_manual_result(test: dict, mode: str, pdf_path: str) -> TestResult:
    """Interactive prompt for recording manual test results."""
    print(f"\n{'='*60}")
    print(f"Test {test['id']}: {test['name']}")
    print(f"Mode: {mode}")
    print(f"PDF: {pdf_path}")
    print(f"Category: {test['category']}")

    prompt = test.get("prompt", "")
    if not prompt and "prompt_sequence" in test:
        prompt = " | ".join(test["prompt_sequence"])

    print(f"\nPrompt to use:\n  {prompt}")
    print(f"\nInstructions:")
    print(f"  1. Open your AI agent ({'with' if mode == 'pdf-mcp' else 'without'} pdf-mcp)")
    print(f"  2. {'Upload' if mode == 'native' else 'Reference'} the PDF: {pdf_path}")
    print(f"  3. Send the prompt above")
    print(f"  4. Record the results below")
    print(f"{'='*60}\n")

    result = TestResult(
        test_id=test["id"],
        test_name=test["name"],
        mode=mode,
        pdf_path=pdf_path,
        prompt=prompt,
    )

    result.response = input("Paste the agent's response (or 'skip'): ").strip()
    if result.response.lower() == "skip":
        result.notes = "Skipped"
        return result

    try:
        result.accuracy_score = int(input("Accuracy score (0-5): "))
    except ValueError:
        result.accuracy_score = 0

    try:
        result.completeness_pct = int(input("Completeness % (0-100): "))
    except ValueError:
        result.completeness_pct = 0

    try:
        result.token_usage = int(input("Token usage (from API stats): "))
    except ValueError:
        result.token_usage = 0

    try:
        result.latency_seconds = float(input("Latency in seconds: "))
    except ValueError:
        result.latency_seconds = 0.0

    result.notes = input("Notes (optional): ").strip()

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark PDF reading: pdf-mcp vs native"
    )
    parser.add_argument("--test", help="Run a specific test by ID (e.g., 1.1)")
    parser.add_argument("--category", help="Run all tests in a category")
    parser.add_argument("--pdf", help="Path to PDF file for the test")
    parser.add_argument("--pdf-dir", help="Directory containing test PDFs")
    parser.add_argument(
        "--mode",
        choices=["native", "pdf-mcp", "both"],
        default="both",
        help="Which mode to test",
    )
    parser.add_argument("--export-csv", help="Export results to CSV")
    parser.add_argument("--export-md", help="Export results to Markdown")
    parser.add_argument(
        "--results",
        default="benchmark_results.json",
        help="Results file (default: benchmark_results.json)",
    )
    parser.add_argument("--list", action="store_true", help="List all available tests")
    args = parser.parse_args()

    data = load_test_prompts()
    tests = data["tests"]

    # List mode
    if args.list:
        print(f"\nAvailable Tests ({len(tests)} total):\n")
        current_cat = ""
        for t in tests:
            if t["category"] != current_cat:
                current_cat = t["category"]
                print(f"\n  [{current_cat}]")
            winner = t.get("expected_winner", "?")
            print(f"    {t['id']}  {t['name']:<45}  (expected: {winner})")
        return

    # Load existing results
    suite = BenchmarkSuite()
    results_path = Path(args.results)
    if results_path.exists():
        with open(results_path) as f:
            existing = json.load(f)
            for r in existing:
                suite.add_result(TestResult(**r))

    # Export mode
    if args.export_csv:
        suite.export_csv(args.export_csv)
        return
    if args.export_md:
        suite.export_markdown(args.export_md)
        return

    # Filter tests
    selected = tests
    if args.test:
        selected = [t for t in tests if t["id"] == args.test]
    elif args.category:
        selected = [t for t in tests if args.category.lower() in t["category"].lower()]

    if not selected:
        print("No matching tests found.")
        return

    if not args.pdf and not args.pdf_dir:
        print("Error: provide --pdf or --pdf-dir")
        print("\nAvailable tests:")
        for t in selected:
            reqs = t.get("pdf_requirements", {})
            print(f"  {t['id']}: needs {reqs.get('type', 'any')} PDF")
            if "suggested_source" in reqs:
                print(f"        suggestion: {reqs['suggested_source']}")
        return

    # Run tests
    modes = ["native", "pdf-mcp"] if args.mode == "both" else [args.mode]
    pdf_path = args.pdf or ""

    for test in selected:
        for mode in modes:
            result = record_manual_result(test, mode, pdf_path)
            suite.add_result(result)

            # Save after each result
            with open(results_path, "w") as f:
                json.dump([asdict(r) for r in suite.results], f, indent=2)

    print(suite.summary())
    print(f"\nResults saved to {results_path}")


if __name__ == "__main__":
    main()
