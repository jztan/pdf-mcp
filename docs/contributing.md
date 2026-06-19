# Contributing

Contributions are welcome — bug fixes, new features, documentation improvements, and benchmark additions.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- System Tesseract if working on OCR features (`brew install tesseract` / `apt install tesseract-ocr`)

## Development setup

```bash
git clone https://github.com/jztan/pdf-mcp.git
cd pdf-mcp
pip install -e ".[dev]"
uv run pre-commit install  # one-time: runs black/flake8/mypy on every commit
```

## Code style

- Line length: 88 characters (Black default)
- Type hints required; `mypy src/` must pass clean
- PEP 8 naming; descriptive variable and function names
- No comments unless the *why* is non-obvious

## Running checks

```bash
# Tests
pytest tests/ -v

# Single test
pytest tests/test_pdf_reader.py::TestParsePageRange::test_range_string -v

# Coverage
pytest tests/ --cov=pdf_mcp --cov-report=term-missing

# Type checking
mypy src/

# Linting / formatting
uv run flake8 src/ tests/ --max-line-length=88
uv run black src/ tests/
```

OCR tests skip automatically when system Tesseract is absent. Benchmark tests (`tests/test_benchmark_*.py`) are fast unit tests for the benchmark scripts' helpers — they run by default and don't download models or run a benchmark.

Tests marked `slow` are excluded from the release pre-flight gate (`scripts/release.py` runs `pytest tests/ -m "not slow"`). The only `slow` test today is the billed coherence-regression guard (`tests/test_eval_coherence.py::test_coherence_no_regression_vs_baseline`), which shells out to the real `claude` CLI over the corpus. Run slow tests deliberately with `pytest -m slow`, and tag any new billed or multi-minute test with `@pytest.mark.slow` so it stays out of the gate.

## Submitting a PR

1. Fork the repo and create a branch from `develop`
2. Make your changes with tests covering the new behaviour
3. Ensure all checks pass (`pytest`, `mypy`, `flake8`, `black --check`)
4. Open a PR against `develop` with a clear description of what changed and why

## Quality loop

Features that change search or extraction quality must follow: **fix → benchmark → corpus expand → re-benchmark**. The initial small-sample benchmark overstates the gap; expanding the corpus narrows it to honest numbers and surfaces ground-truth errors. Don't skip steps.

## Coherence eval harness

`scripts/eval_coherence.py` has Claude read pdf-mcp's extracted text and classify its reading-order coherence (coherent / partial / scrambled) across a fixed corpus. It catches reading-order scrambling that containment and uniqueness metrics miss — those guard *performance* regressions, this guards extraction *quality*.

Requires the authenticated `claude` CLI (installed and signed in). Run from the repo root:

```bash
uv run python scripts/eval_coherence.py
```

The run judges each corpus page (majority-of-3), writes `benchmark_data/coherence_results.md`, and diffs against the committed baseline (`benchmark_data/coherence_baseline.json`), exiting non-zero on any regression. To re-baseline after an intended extraction improvement:

```bash
uv run python scripts/eval_coherence.py --update-baseline
```
