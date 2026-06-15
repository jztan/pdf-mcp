# Contributing

## Development setup

```bash
git clone https://github.com/jztan/pdf-mcp.git
cd pdf-mcp
pip install -e ".[dev]"
uv run pre-commit install
```

## Running checks

```bash
pytest tests/ -v
mypy src/
uv run flake8 src/ tests/ --max-line-length=88
uv run black src/ tests/
```

## Coherence eval harness

`scripts/eval_coherence.py` has Claude read pdf-mcp's extracted text and classify its reading-order coherence (coherent / partial / scrambled) across a fixed corpus of representative pages. It catches reading-order scrambling that aggregate containment / uniqueness metrics are blind to — those metrics guard *performance* regressions, this guards extraction *quality*.

Requires the authenticated `claude` CLI (installed and signed in). Run from the repo root:

```bash
uv run python scripts/eval_coherence.py
```

The run judges each corpus page (majority-of-3), writes `benchmark_data/coherence_results.md`, and diffs against the committed baseline (`benchmark_data/coherence_baseline.json`), exiting non-zero on any regression. To re-baseline after an intended extraction improvement:

```bash
uv run python scripts/eval_coherence.py --update-baseline
```

## Quality loop

Features that change search or extraction quality must follow: **fix → benchmark → corpus expand → re-benchmark**. The initial small-sample benchmark overstates the gap; expanding the corpus narrows it to honest numbers and surfaces ground-truth errors.

