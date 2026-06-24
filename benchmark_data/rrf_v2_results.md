# RRF hybrid-search benchmark (v2)

NDCG@10 of `pdf_search` in each mode — `keyword` (FTS5/BM25), `semantic`
(bge-small embeddings), and `auto` (hybrid Reciprocal Rank Fusion) — over a
graded corpus deliberately built to stress keyword search's failure modes
(Porter-stemming variants, hyphen/space substring boundaries). Higher is better,
max 1.0. Embeddings are deterministic (`BAAI/bge-small-en-v1.5`, CPU, fastembed
`0.8.0` pinned), and the gate runs against a dedicated corpus-only cache so
FTS5 `bm25()` IDF is computed over the benchmark corpus alone — the numbers are
reproducible across machines and unaffected by whatever else is cached.

## Aggregate (28 queries)

| mode | NDCG@10 |
| --- | --- |
| keyword | 0.625 |
| semantic | 0.656 |
| **auto (hybrid RRF)** | **0.767** |

Hybrid beats keyword-only by **+14.2pp** and semantic-only by **+11.1pp**. This
is the first empirical confirmation that hybrid `auto` mode is worth defaulting
to — on a corpus where keyword is at a deliberate disadvantage.

## By query class

| class | n | keyword | semantic | hybrid |
| --- | --- | --- | --- | --- |
| stemming | 9 | 0.597 | 0.680 | **0.725** |
| substring | 10 | **0.872** | 0.617 | 0.869 |
| fusion | 4 | 0.385 | 0.728 | **0.774** |
| distractor | 5 | 0.372 | 0.632 | **0.634** |
| **all** | 28 | 0.625 | 0.656 | **0.767** |

## Findings

- **Hybrid wins overall and on 3 of 4 classes.** It is most valuable exactly
  where keyword collapses: `fusion` (keyword 0.385 → hybrid 0.774) and `stemming`
  (0.597 → 0.725).
- **Honest caveat — substring is the exception.** On substring queries, plain
  `keyword` (0.872) edges out `hybrid` (0.869): BM25 already nails these, and RRF
  slightly dilutes its clean rank-1 by fusing in a weaker semantic ranking. This
  matches the project's earlier hybrid-section finding (RRF dilutes clean keyword
  wins). The net is still strongly positive because keyword falls apart on the
  other classes.
- **4 of 28 queries score keyword-NDCG = 0** (`fusion-03`, `distractor-03`,
  `stem-09`, `fusion-04`): FTS5 keyword search misses them entirely (morphological
  variants and hyphen/space boundaries it cannot tokenise across). These are
  precisely why the gate asserts on the **keyword arm**: the planned CJK FTS5
  tokenizer change must not add more such misses.

## Corpus

28 graded queries (relevance 0–3) over 4 arXiv PDFs (`attention`, `gpt3`,
`bert`, `resnet`). Classes: 9 stemming, 10 substring, 4 fusion, 5 distractor.
Source: [`rrf_v2_queries.json`](rrf_v2_queries.json).

## Method & reproduction

- Metric: NDCG@10, computed per query per mode, averaged.
- The gate runs against an isolated, corpus-only cache
  (`~/.cache/pdf-mcp-rrf-gate`), separate from the normal pdf-mcp cache. This is
  required for reproducibility: FTS5 `bm25()` derives IDF from the whole shared
  table, so unrelated cached PDFs would shift keyword ranking and the baseline
  would not reproduce on a clean machine.
- The committed baseline ([`rrf_v2_baseline.json`](rrf_v2_baseline.json)) is a
  snapshot of **pre-CJK** keyword-mode quality. The `slow`-marked gate
  (`test_rrf_v2_no_regression_vs_baseline`) fails if any query's keyword NDCG
  regresses beyond the tolerance band, or if the fastembed version drifts.
- Run the gate: `uv run python scripts/benchmark_rrf.py --graded`
- Re-baseline (after a reviewed, intended change):
  `uv run python scripts/benchmark_rrf.py --graded --update-baseline`
