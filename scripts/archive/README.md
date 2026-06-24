# scripts/archive/ — superseded benchmark experiments (kept for provenance)

These scripts and their unit tests back **investigated-and-rejected / shelved** lines of
work recorded in [`ROADMAP.md`](../../ROADMAP.md)'s "Investigated / Rejected" log. They no
longer guard any shipping feature, so they are **not** collected by CI (`pytest tests/ -v`
only walks `tests/`). They are kept — not deleted — because they generated the
`benchmark_data/*.md` results cited in the ROADMAP and blog posts; removing them would
undercut the "prove it with benchmarks" reproducibility story.

Run them on demand with:

```bash
uv run pytest scripts/archive/            # the co-located helper tests (28 tests)
uv run python scripts/archive/benchmark_<name>.py
```

## What's here and why it was archived

| Script | Status | Reason |
|---|---|---|
| `benchmark_hybrid_sections.py` | rejected (2026-05-04) | hybrid-section search regressed lexical ranking (−33%) |
| `benchmark_mlx_backend.py` | rejected | MLX fork showed no lift over bge + fastembed-CPU |
| `benchmark_minilm_mlx.py` | rejected | MLX MiniLM variant, same rejected line |
| `benchmark_coreml_provider.py` | rejected | CoreML provider, same rejected line |
| `benchmark_large_models.py` | rejected | no large-model challenger cleared the gate |
| `benchmark_drilldown.py` | rejected | MLX / embedding drilldown, same rejected line |
| `benchmark_e5_prefix.py` | rejected | E5 prefix evaluation, same embedding line |
| `benchmark_pooling_attribution.py` | rejected | pooling-strategy attribution, same embedding line |
| `benchmark_boilerplate.py` | shelved (2026-06-11) | running-header stripping: only 0.2–1.6% token savings |
| `benchmark_search_impact.py` | shelved (2026-06-11) | search-impact leg of boilerplate work; BM25 IDF already neutralizes it |

## Co-located tests

`test_benchmark_{hybrid_sections,mlx_backend,large_models,drilldown}.py` are unit tests for
the helper functions (`mrr`, `recall_at_k`, `save_results`, …) of the scripts above. They
were moved out of `tests/` so they no longer run on every push, but stay runnable here for
provenance. Each adds both this directory and the parent `scripts/` to `sys.path` because a
few archived scripts still import the **active** `benchmark_embedding_models` module.

## Active benchmarks live elsewhere

Current, shipping-behavior benchmarks (`benchmark_rrf.py`, `benchmark_sections.py`,
`benchmark_embedding_models.py`, the RRF-v2 gate) and the competitor comparisons
(`benchmark_vs_pdf_reader_mcp.py`, `benchmark_vs_pdf_oxide.py`) remain in `scripts/`.
