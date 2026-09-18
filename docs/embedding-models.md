# Embedding Models

pdf-mcp uses [`fastembed`](https://github.com/qdrant/fastembed) for local, offline embedding. The four models below are validated end-to-end against the project's arxiv benchmark corpus (see [Live Benchmark Results](#live-benchmark-results)). Any other model in the [fastembed TextEmbedding catalogue](https://qdrant.github.io/fastembed/examples/Supported_Models/) is accepted by the BYOM config, but is unvalidated — see the [Unvalidated section](#unvalidated-models) below for the gotchas we've already hit.

## Configuration

Add to `~/.config/pdf-mcp/config.toml`:

```toml
[embedding]
model = "snowflake/snowflake-arctic-embed-s"
```

Missing key → default `BAAI/bge-small-en-v1.5`. The model downloads once on first use. Switching models clears the embedding cache for that PDF; re-embedding happens automatically on the next search.

All models on this page run through the local `fastembed`/onnxruntime path. For running the *default* `bge-small-en-v1.5` model against a remote OpenAI-compatible server instead (a compute-backend speed option, not a way to pick a different model), see "[Remote-served bge-small](configuration.md#remote-served-bge-small-embeddingbackend--openai)" in `docs/configuration.md`.

---

## Validated Models

These four models have been live-tested against the 7-scenario arxiv ground-truth corpus (Attention paper + GPT-3 paper). MTEB Retrieval = nDCG@10 averaged over 15 retrieval tasks (English MTEB benchmark). Higher is better. Sources: model cards on HuggingFace; see [Notes](#notes) below.

### Fast English (384 dimensions)

| Model | Size | MTEB Retrieval | License | Notes |
|-------|------|---------------|---------|-------|
| `BAAI/bge-small-en-v1.5` *(default)* | 67 MB | 51.68 | MIT | Best retrieval-per-MB at this size; proven default |
| `snowflake/snowflake-arctic-embed-s` | 130 MB | 51.98 | Apache 2.0 | Slightly higher MTEB than default; good Apache 2.0 alternative |

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
| Highest MTEB under 500 MB | `snowflake/snowflake-arctic-embed-m` |

---

## Unvalidated Models

The fastembed catalogue includes additional models (long-context, very-large, multilingual) that the BYOM config will accept but that we have **not** validated end-to-end. Use at your own risk; if you successfully run the benchmark on one, send numbers and we'll promote it.

Known gotchas we've already hit:

- **`nomic-ai/nomic-embed-text-v1.5`** (520 MB, 768-dim, 8192-token context) — fastembed's default `batch_size=256` makes the model OOM/hang when embedding PDFs with ~75+ pages of long text on commodity hardware. Lowering `batch_size` helps but didn't make it reliable in our tests.
- **`mixedbread-ai/mxbai-embed-large-v1`** (640 MB, 1024-dim) — not run against the live corpus.
- **`BAAI/bge-large-en-v1.5`** (1.2 GB, 1024-dim) — not run against the live corpus.
- **`intfloat/multilingual-e5-small`** (384-dim, 100+ languages) — not run against the live corpus.
- **`intfloat/multilingual-e5-large`** (2.2 GB, 1024-dim, 100+ languages) — not run against the live corpus.
- **`jinaai/jina-embeddings-v2-base-de`** (320 MB, 768-dim, German) — **does not load under pdf-mcp today.** fastembed hardcodes onnxruntime's `ORT_ENABLE_ALL` graph-optimization level and this model's exported graph fails a fusion pass there. The German benchmark harness works around it process-locally (`--patch-onnx-graph-opt`); `embedder.py` has no per-model session-options override, so configuring this model via BYOM fails at load.

If you need any of these (long contexts, multilingual, larger English models), pin via BYOM and validate the retrieval yourself before depending on it. For German specifically, see [`german_embedding_results.md`](../benchmark_data/german_embedding_results.md) — several models there clear this project's MRR-lift and latency gates on the tested corpus, but none has been adopted as a default.

---

## Notes

- **MTEB scores** for BGE v1.5 models from their [HuggingFace model cards](https://huggingface.co/BAAI/bge-small-en-v1.5).
- **Snowflake Arctic Embed** scores from [snowflake-arctic-embed-m](https://huggingface.co/Snowflake/snowflake-arctic-embed-m) and [-l](https://huggingface.co/Snowflake/snowflake-arctic-embed-l) model cards.
- All validated models run fully locally via fastembed by default — no external API calls, unless `[embedding].backend = "openai"` is set to point at a remote endpoint (see `docs/configuration.md`).

---

## Live Benchmark Results

Measured on the existing arxiv ground-truth corpus (Attention paper + GPT-3 paper, 7 hand-annotated scenarios). MRR aggregated across all 7 scenarios at each scenario's native k. Latency = p50 query time on a warm embedding cache. Run via `scripts/benchmark_embedding_models.py`.

> For a comparison of **search modes** (keyword vs semantic vs hybrid RRF) rather than embedding models, see [`benchmark_data/rrf_v2_results.md`](../benchmark_data/rrf_v2_results.md) — hybrid `auto` scores NDCG@10 0.77 vs 0.63 keyword / 0.66 semantic on a stemming/substring-stress corpus.

| Model | MRR | p50 latency | Size | MTEB |
|-------|-----|-------------|------|------|
| `BAAI/bge-small-en-v1.5` *(baseline)* | 0.726 | 41.9 ms | 67 MB | 51.68 |
| `BAAI/bge-base-en-v1.5` | 0.714 | 207.3 ms | 210 MB | 53.25 |
| `snowflake/snowflake-arctic-embed-s` | 0.607 | 67.0 ms | 130 MB | 51.98 |
| `snowflake/snowflake-arctic-embed-m` | 0.452 | 50.5 ms | 430 MB | 54.90 |

**Default decision (rerun 2026-09-15, Python 3.13.1, SQLite 3.51.0, macOS arm64):** kept. No challenger passed the gate (MRR lift ≥ 0.05 AND p50 ≤ 1.5x baseline). bge-small leads bge-base by only 0.012 MRR, and with 7 scenarios one query moving a single rank shifts MRR by up to about 0.07, so treat the two as tied on quality.

The MRR changes since the previous run (2026-05-09) all come from sub-page embeddings. Scoring with whole-page vectors alone reproduces that run's bge-small 0.806 and bge-base 0.667 exactly. arctic-embed-m's whole-page vectors rank the same few pages first for every query, while its ~300-token windows do not, which is why it went from 0.029 to 0.452. bge-small's drop is a single query, where a window on another page now outscores the labelled one.

p50 latency covers the whole warm `pdf_search` call, and most of it is building semantic excerpts for the top hits, which embeds candidate spans with the same model. Encoding the query itself takes about 1.5 ms with bge-small and 4 ms with bge-base. That is why bge-base is about 5x slower per search here, and why these latencies are not comparable with the 2026-05-09 run, which predates semantic excerpts.
