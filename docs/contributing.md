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

Always pass the `tests/` path. A bare `pytest` walks the whole repository and also collects retired spikes under `scripts/archive/`, which no gate runs.

The package supports Python 3.10 and up. CI runs 3.10 through 3.14 on Linux and 3.10 and 3.13 on Windows, but you are developing on one interpreter and one operating system. `tests/test_python_compat.py` catches the case that bites hardest: importing a stdlib module that does not exist on 3.10 (`tomllib`, added in 3.11) fails at *collection*, so it takes the entire suite down rather than failing one test. Gate such an import on a version check with a backport, the way `src/pdf_mcp/config.py` does. That check is static, so it cannot see new syntax or new methods on existing types. When a change leans on anything recent, run the suite on the floor version: `uv run --python 3.10 --extra dev pytest tests/ -m "not slow"`. Note that this rebuilds `.venv` on 3.10, so follow it with `uv sync --extra dev` to restore your normal environment.

Tests marked `slow` are excluded from the release pre-flight gate (`scripts/release.py` runs `pytest tests/ -m "not slow"`). There are four: the billed coherence-regression guard (`test_eval_coherence.py`), which shells out to the real `claude` CLI, plus the RRF v2 retrieval gate (`test_benchmark_rrf_v2.py`), the CJK keyword recall gate (`test_benchmark_cjk_keyword.py`) and the pre-gate validation (`test_pregate_validation.py`). Tag any new billed or multi-minute test with `@pytest.mark.slow` so it stays out of the release gate.

**`-m "not slow"` is the release gate, not what CI runs.** The Linux CI job runs the whole suite with no marker filter, so a change that breaks only a slow-marked path passes locally and fails on push. That has happened twice. Before pushing, run the three unbilled slow tests as well:

```bash
uv run pytest tests/ -m "not slow"          # fast suite
uv run pytest tests/test_benchmark_rrf_v2.py \
              tests/test_benchmark_cjk_keyword.py \
              tests/test_pregate_validation.py   # unbilled slow tests
```

The coherence guard is the one to leave alone unless you mean to spend money on it.

### Cross-platform notes

CI runs the suite on Windows as well as Linux, and it is not a formality: it has caught bugs no Linux job could see, including OCR failing outright on a default Windows Tesseract install (the install path contains a space) and cold `pdf_search` taking 17.5s there against 3.2s on Linux. Two habits keep changes portable:

- Never let a `tempfile.NamedTemporaryFile` handle stay open while something else writes to that path, and never assume a temp directory can be deleted while a file inside it is open. Windows refuses both. Use `tests/tmpfiles.py::unlink_quietly` and `TemporaryDirectory(ignore_cleanup_errors=True)`.
- Pass `encoding="utf-8"` to every `read_text`/`write_text`/`open` on a text file. The platform default is cp1252 on Windows, which corrupts anything non-ASCII.

`scripts/benchmark_platform_smoke.py` times the user-facing paths on one machine and prints a JSON blob; the `platform-bench` workflow runs it on both platforms. Reach for it when a change could plausibly cost more on one OS than another, rather than inferring it from how long CI took.

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
