# Embedding evaluation — summary

One place for the whole embedding investigation: can pdf-mcp do better than its
default `BAAI/bge-small-en-v1.5` on fastembed/CPU, via different prefixes, an MLX
(Apple-GPU) backend, or a different model? Every path was benchmark-tested before
any production change.

**Conclusion: no, for English. `bge-small` on fastembed CPU stays the
default.** The only change worth making was an unrelated bug the
investigation surfaced — the embedding **normalization fix** (shipped to
`develop`).

**This conclusion is English-only.** A separate German-language benchmark
([`german_embedding_results.md`](german_embedding_results.md), against a
public statute PDF per
[jztan/pdf-mcp#46](https://github.com/jztan/pdf-mcp/issues/46), 119
scenarios, scored on the target norm page rather than the looser page set
that also counts a citing referrer page as a hit) found bge-small's German
hybrid MRR (0.222) meaningfully below three local models whose CIs exclude
zero: `multilingual-e5-large` (+0.075 MRR), the specific model the issue
asked about once a fastembed/onnxruntime graph-optimization incompatibility
is worked around, `jina-embeddings-v2-base-de` (+0.073 MRR), and
`paraphrase-multilingual-mpnet-base-v2` (+0.060 MRR). All three clear this
repo's existing MRR-lift *and* 1.5x latency gates on hybrid-mode numbers —
a change from the pre-review draft of this benchmark, which compared
against semantic-only latency and wrongly reported neither as gate-passing.
That does not make any of them the production default on its own (see the
detail doc for caveats), but the catalog is not exhausted for German the
way it is for English.

Detail docs (kept): [`e5_prefix_results.md`](e5_prefix_results.md) ·
[`mlx_backend_results.md`](mlx_backend_results.md) ·
[`large_models_results.md`](large_models_results.md) ·
[`german_embedding_results.md`](german_embedding_results.md).
Raw per-run output: `benchmark_results/*.json` (+ `.txt`), gitignored.

## Everything tested

| Experiment | Key result | Verdict | Detail |
|------------|-----------|---------|--------|
| **E5 query/passage prefixes** | +0.035 MRR (7 scn) → **−0.046** (22 scn) | ❌ rejected (net-negative) | e5_prefix |
| **MLX backend** vs default bge | 2.78× faster, but **wrong pooling** (cosine 0.89, 16% rankings shift) | ❌ not a safe drop-in | mlx_backend |
| MLX pooling attribution | each backend ≡ one pooling @ **cosine 1.0000** (fastembed=CLS, MLX=mean) | — (root-cause proof) | mlx_backend |
| **CoreML execution provider** | output-identical (cosine 1.0) but **~1.0× (no speedup)** | ❌ safe but useless | mlx_backend |
| **MLX + MiniLM** (mean-pooled) | correct (cosine 1.0) but **0.80× (slower)** + worse retrieval | ❌ rejected | mlx_backend |
| **Large-model screen** | all lose to bge (see below) | ❌ none clears +0.05 gate | large_models |
| Per-scenario drill-down (bge vs gte-large) | bge **8** / gte **4** / **10** ties | near-tie, bge ahead — not domination | large_models |
| **Normalization fix** | e5 vectors norm 27.68 → **1.0**; restores `dot==cosine` | ✅ **SHIPPED** (merged to develop) | CHANGELOG |

## Models tested (22-scenario corpus, MRR)

| Model | Size | MRR | vs bge | Note |
|-------|------|-----|--------|------|
| **`bge-small-en-v1.5`** *(default)* | 67 MB | **0.616** | — | works, best, CLS |
| `mxbai-embed-large-v1` | 655 MB | 0.602 | −0.014 | raw (wants query prompt) |
| `gte-large` | 1.2 GB | 0.564 | −0.052 | symmetric — clean loss |
| `multilingual-e5-large` | 2.3 GB | ~0.53 | ~−0.09 | raw (wants prefixes) |
| `all-MiniLM-L6-v2` | 92 MB | 0.511 | −0.105 | mean-pooled |
| `arctic-embed-l` | 1.0 GB | 0.244 | −0.372 | raw (wants query prompt) — collapses |
| `gte-base` | 451 MB | — | — | **fastembed 0.8 ValueError — won't run** |
| `nomic-embed-text-v1.5` | 532 MB | — | — | **segfaults on full corpus — won't run** |

## Why bigger doesn't win here

MTEB ranks several of these above bge-small, but that lift **does not transfer to
LLM-fed page retrieval** — short factual queries over PDF pages, results handed to
an agent. Confirmed three independent times (May 4-model run, e5-large, this
large-model screen) and corroborated by the literature (arXiv 2506.00049,
"small embeddings + LLM re-ranking beat bigger models"). Model size buys almost
nothing in pdf-mcp's regime, for English. The only remaining lever for more
confidence on the *English* path is a broader, multi-domain corpus — that
catalog is exhausted. Non-English is a different question with a different
answer; see [`german_embedding_results.md`](german_embedding_results.md).
