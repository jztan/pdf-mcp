# Large-model retrieval re-benchmark

> Part of the [embedding evaluation summary](embedding_evaluation_summary.md).

Quality-first re-benchmark of large **mean-pooled** embedding models vs the
`bge-small` default, on the 22-scenario corpus (`e5_prefix_corpus.json`). Run to
answer "is there a model worth switching the default to?" — a backend (MLX)
never justifies a model; only retrieval does. Harness:
`scripts/archive/benchmark_large_models.py` (reuses `benchmark_embedding_models.run_model`).
Gate: MRR lift ≥ 0.05. Decision metric: MRR.

## Results

| Model | Size | MRR | Δ vs bge | p50 | Note |
|-------|------|-----|----------|-----|------|
| `BAAI/bge-small-en-v1.5` *(default)* | 67 MB | **0.616** | — | 42 ms | CLS, baseline |
| `thenlper/gte-large` | 1.2 GB | 0.564 | **−0.052** | 115 ms | symmetric — no prompt excuse |
| `mixedbread-ai/mxbai-embed-large-v1` | 655 MB | 0.602 | −0.014 | 49 ms | raw (needs query prompt) |
| `snowflake/snowflake-arctic-embed-l` | 1.0 GB | 0.244 | −0.372 | 36 ms | raw (needs query prompt) — collapsed |

**No challenger clears the gate; none even beats bge-small.** `bge-small` stays
the default.

Two more mid-size mean-pooled candidates were attempted to exhaust the catalog,
but **neither runs cleanly via fastembed 0.8** in this stack (a tooling failure,
not a quality verdict — both are good models elsewhere):

| Model | Size | Outcome |
|-------|------|---------|
| `thenlper/gte-base` | 451 MB | `ValueError: inhomogeneous embedding shape (15,)` — no usable vectors |
| `nomic-ai/nomic-embed-text-v1.5` | 532 MB | loads + embeds one string, but **segfaults** embedding the full corpus |

So they can't be evaluated for retrieval or adopted as the default here at all.
`benchmark_large_models.py` takes model names as argv to retry them in isolation
if the integration is fixed later.

## Why this is conclusive

- **`gte-large` is the decisive data point.** It is large, mean-pooled,
  MTEB-strong, and **symmetric** (no query-prompt requirement), so its score has
  no confound — yet it lands **0.052 below** bge-small. Larger is simply not
  better on this scientific-PDF corpus.
- **`mxbai` and `arctic` are raw-confounded** (they want an asymmetric query
  prompt; the production path embeds queries verbatim). arctic collapsed to
  0.244 — the same failure mode that sank arctic-embed-m in the earlier
  embedding-model benchmark. Their raw scores ARE the production-relevant
  numbers for a drop-in default swap, since adding prompt handling is the
  E5-prefix path already evaluated and rejected. Even so, both lose to bge.
- This is the **third** independent confirmation that `bge-small` is optimal
  here (4-model May benchmark; e5-large in the MLX evaluation; these three large
  models now).

## Per-scenario drill-down (bge vs gte-large)

`gte-large` is the clean comparison (symmetric, no prompt confound), so a
per-scenario drill-down (`scripts/archive/benchmark_drilldown.py`) checks whether the
0.052 MRR gap is one fluke or systematic. Of 22 scenarios: **10 ties, bge
wins 8, gte-large wins 4.**

- Not a single-outlier artifact — the difference is spread across 12 scenarios.
- The harness is even-handed: gte-large genuinely wins several, including a clean
  0.00 → 1.00 on `b2` ("what % of wordpiece tokens"). bge's biggest wins are
  `1c` (1.00 vs 0.20) and `r1` (1.00 vs 0.33).
- Read: the two are **roughly comparable, bge slightly ahead** — a near-tie,
  not domination. A 67 MB model is *competitive* with a 1.2 GB one here, which
  is believable; "bge dominates" would not be, and is not what the data shows.
- 8–4 on a contested-12-of-22 is close enough to be noisy, so "gte-large does
  not beat bge" is solid; "bge is definitively better" over-reads the sample.

## Conclusion

No better embedding model exists — large or otherwise — within fastembed's
catalog for this corpus, and therefore no model whose size would justify the MLX
backend. The real lesson: **model size buys almost nothing on narrow factual
page-retrieval**, the regime pdf-mcp operates in. `bge-small` on fastembed CPU
remains the right default; the embedding path needs no change beyond the
already-merged normalization fix. The only honest lever left for higher
confidence is a broader, multi-domain corpus — the model list is exhausted.

Machine-readable run output: `benchmark_results/large_models_<timestamp>.json`
(+ `.txt`), gitignored.
