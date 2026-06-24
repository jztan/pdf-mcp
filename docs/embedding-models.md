# Embedding Models

pdf-mcp uses [`fastembed`](https://github.com/qdrant/fastembed) for local, offline embedding. The four models below are validated end-to-end against the project's arxiv benchmark corpus (see [Live Benchmark Results](#live-benchmark-results)). Any other model in the [fastembed TextEmbedding catalogue](https://qdrant.github.io/fastembed/examples/Supported_Models/) is accepted by the BYOM config, but is unvalidated — see the [Unvalidated section](#unvalidated-models) below for the gotchas we've already hit.

## Configuration

Add to `~/.config/pdf-mcp/config.toml`:

```toml
[embedding]
model = "snowflake/snowflake-arctic-embed-s"
```

Missing key → default `BAAI/bge-small-en-v1.5`. The model downloads once on first use. Switching models clears the embedding cache for that PDF; re-embedding happens automatically on the next search.

---

## Validated Models

These four models have been live-tested against the 7-scenario arxiv ground-truth corpus (Attention paper + GPT-3 paper). MTEB Retrieval = nDCG@10 averaged over 15 retrieval tasks (English MTEB benchmark). Higher is better. Sources: model cards on HuggingFace; see [Notes](#notes) below.

### Fast English (384 dimensions)

| Model | Size | MTEB Retrieval | License | Notes |
|-------|------|---------------|---------|-------|
| `BAAI/bge-small-en-v1.5` *(default)* | 67 MB | 51.68 | MIT | Best retrieval-per-MB at this size; proven default |
| `snowflake/snowflake-arctic-embed-s` | 130 MB | 51.98 | Apache 2.0 | Slightly better retrieval than default; good Apache 2.0 alternative |

### Mid-Size English (768 dimensions)

| Model | Size | MTEB Retrieval | License | Notes |
|-------|------|---------------|---------|-------|
| `BAAI/bge-base-en-v1.5` | 210 MB | 53.25 | MIT | Solid mid-size step-up |
| `snowflake/snowflake-arctic-embed-m` | 430 MB | 54.90 | Apache 2.0 | Best MTEB under 500 MB |

---

## Selection Guide

| Goal | Model |
|------|-------|
| Keep it simple | `BAAI/bge-small-en-v1.5` *(default)* |
| Apache 2.0 drop-in for default | `snowflake/snowflake-arctic-embed-s` |
| Mid-size step-up (MIT) | `BAAI/bge-base-en-v1.5` |
| Best validated retrieval | `snowflake/snowflake-arctic-embed-m` |

---

## Unvalidated Models

The fastembed catalogue includes additional models (long-context, very-large, multilingual) that the BYOM config will accept but that we have **not** validated end-to-end. Use at your own risk; if you successfully run the benchmark on one, send numbers and we'll promote it.

Known gotchas we've already hit:

- **`nomic-ai/nomic-embed-text-v1.5`** (520 MB, 768-dim, 8192-token context) — fastembed's default `batch_size=256` makes the model OOM/hang when embedding PDFs with ~75+ pages of long text on commodity hardware. Lowering `batch_size` helps but didn't make it reliable in our tests.
- **`mixedbread-ai/mxbai-embed-large-v1`** (640 MB, 1024-dim) — not run against the live corpus.
- **`BAAI/bge-large-en-v1.5`** (1.2 GB, 1024-dim) — not run against the live corpus.
- **`intfloat/multilingual-e5-small`** (384-dim, 100+ languages) — not run against the live corpus.
- **`intfloat/multilingual-e5-large`** (2.2 GB, 1024-dim, 100+ languages) — not run against the live corpus.

If you need any of these (long contexts, multilingual, larger English models), pin via BYOM and validate the retrieval yourself before depending on it.

---

## Notes

- **MTEB scores** for BGE v1.5 models from their [HuggingFace model cards](https://huggingface.co/BAAI/bge-small-en-v1.5).
- **Snowflake Arctic Embed** scores from [snowflake-arctic-embed-m](https://huggingface.co/Snowflake/snowflake-arctic-embed-m) and [-l](https://huggingface.co/Snowflake/snowflake-arctic-embed-l) model cards.
- All validated models run fully locally via fastembed — no external API calls.

---

## Live Benchmark Results

Measured on the existing arxiv ground-truth corpus (Attention paper + GPT-3 paper, 7 hand-annotated scenarios). MRR aggregated across all 7 scenarios at each scenario's native k. Latency = p50 query time on a warm embedding cache. Run via `scripts/benchmark_embedding_models.py`.

> For a comparison of **search modes** (keyword vs semantic vs hybrid RRF) rather than embedding models, see [`benchmark_data/rrf_v2_results.md`](../benchmark_data/rrf_v2_results.md) — hybrid `auto` scores NDCG@10 0.77 vs 0.63 keyword / 0.66 semantic on a stemming/substring-stress corpus.

| Model | MRR | p50 latency | Size | MTEB |
|-------|-----|-------------|------|------|
| `BAAI/bge-small-en-v1.5` *(baseline)* | 0.806 | 6.1 ms | 67 MB | 51.68 |
| `snowflake/snowflake-arctic-embed-s` | 0.690 | 4.1 ms | 130 MB | 51.98 |
| `BAAI/bge-base-en-v1.5` | 0.667 | 5.7 ms | 210 MB | 53.25 |
| `snowflake/snowflake-arctic-embed-m` | 0.029 | 5.8 ms | 430 MB | 54.90 |

**Default decision (2026-05-09):** kept — no challenger passed the gate (MRR lift ≥ 0.05 AND p50 ≤ 1.5x baseline). bge-small wins MRR by 0.116 over the best challenger on this corpus. The arctic-embed-m collapse (0.029) likely reflects a missing query/passage prefix protocol that fastembed does not apply automatically; users running BYOM with that family should validate their results before relying on them.
