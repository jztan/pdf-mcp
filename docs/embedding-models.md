# Embedding Models

pdf-mcp uses [`fastembed`](https://github.com/qdrant/fastembed) for local, offline embedding. Any model in the [fastembed TextEmbedding catalogue](https://qdrant.github.io/fastembed/examples/Supported_Models/) works — set it once in config and the server handles the rest.

## Configuration

Add to `~/.config/pdf-mcp/config.toml`:

```toml
[embedding]
model = "nomic-ai/nomic-embed-text-v1.5"
```

Missing key → default `BAAI/bge-small-en-v1.5`. The model downloads once on first use. Switching models clears the embedding cache for that PDF; re-embedding happens automatically on the next search.

---

## Model Comparison

MTEB Retrieval = nDCG@10 averaged over 15 retrieval tasks (English MTEB benchmark). Higher is better. Sources: model cards on HuggingFace; see [Notes](#notes) below.

### Fast English (384 dimensions)

| Model | Size | MTEB Retrieval | License | Notes |
|-------|------|---------------|---------|-------|
| `BAAI/bge-small-en-v1.5` *(default)* | 67 MB | 51.68 | MIT | Best retrieval-per-MB at this size; proven default |
| `snowflake/snowflake-arctic-embed-s` | 130 MB | 51.98 | Apache 2.0 | Slightly better retrieval than default; good Apache 2.0 alternative |

### High-Quality English (768–1024 dimensions)

| Model | Dims | Size | MTEB Retrieval | License | Notes |
|-------|------|------|---------------|---------|-------|
| `BAAI/bge-base-en-v1.5` | 768 | 210 MB | 53.25 | MIT | Solid mid-size step-up |
| `snowflake/snowflake-arctic-embed-m` | 768 | 430 MB | 54.90 | Apache 2.0 | Best retrieval under 500 MB |
| `mixedbread-ai/mxbai-embed-large-v1` | 1024 | 640 MB | 54.39 | Apache 2.0 | Highest overall MTEB (64.68); beats OpenAI `text-embedding-3-large` |
| `BAAI/bge-large-en-v1.5` | 1024 | 1.2 GB | 54.29 | MIT | Large BGE; established and stable |

### Long Context (8 192-token window)

| Model | Dims | Size | MTEB Retrieval | License | Notes |
|-------|------|------|---------------|---------|-------|
| `nomic-ai/nomic-embed-text-v1.5` | 768 | 520 MB | ~53 | Apache 2.0 | Fully open — weights + training data + code; Matryoshka dimensions (64–768) |

### Multilingual

| Model | Dims | Size | Languages | License | Notes |
|-------|------|------|-----------|---------|-------|
| `intfloat/multilingual-e5-small` | 384 | — | 100+ | MIT | Low memory; requires `"query: "` prefix |
| `intfloat/multilingual-e5-large` | 1024 | 2.2 GB | 100+ | MIT | Best multilingual retrieval in fastembed |

---

## Selection Guide

| Goal | Model |
|------|-------|
| Keep it simple | `BAAI/bge-small-en-v1.5` *(default)* |
| Best retrieval, no size constraint | `snowflake/snowflake-arctic-embed-m` or `-l` |
| Best overall MTEB score | `mixedbread-ai/mxbai-embed-large-v1` |
| Long documents (contracts, books) | `nomic-ai/nomic-embed-text-v1.5` |
| Multilingual PDFs | `intfloat/multilingual-e5-large` |
| Apache 2.0 drop-in for default | `snowflake/snowflake-arctic-embed-s` |
| Fully open (audit / compliance) | `nomic-ai/nomic-embed-text-v1.5` |

---

## Notes

- **MTEB scores** for BGE v1.5 models from their [HuggingFace model cards](https://huggingface.co/BAAI/bge-small-en-v1.5).
- **Snowflake Arctic Embed** scores from [snowflake-arctic-embed-m](https://huggingface.co/Snowflake/snowflake-arctic-embed-m) and [-l](https://huggingface.co/Snowflake/snowflake-arctic-embed-l) model cards.
- **mxbai-embed-large-v1** score from its [model card](https://huggingface.co/mixedbread-ai/mxbai-embed-large-v1).
- **nomic-embed-text-v1.5** retrieval score inferred from its [model card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) comparison against `bge-base-en-v1.5`.
- Multilingual MTEB retrieval scores not listed — multilingual benchmarks use different task sets (Mr.TyDi, MIRACL) and are not directly comparable to English MTEB retrieval averages.
- All models run fully locally via fastembed — no external API calls.

---

## Live Benchmark Results

Measured on the existing arxiv ground-truth corpus (Attention paper + GPT-3 paper, 7 hand-annotated scenarios). MRR aggregated across all 7 scenarios at each scenario's native k. Latency = p50 query time on a warm embedding cache. Run via `scripts/benchmark_embedding_models.py`.

| Model | MRR | p50 latency | Size | MTEB |
|-------|-----|-------------|------|------|
| `BAAI/bge-small-en-v1.5` *(baseline)* | 0.806 | 6.1 ms | 67 MB | 51.68 |
| `snowflake/snowflake-arctic-embed-s` | 0.690 | 4.1 ms | 130 MB | 51.98 |
| `BAAI/bge-base-en-v1.5` | 0.667 | 5.7 ms | 210 MB | 53.25 |
| `snowflake/snowflake-arctic-embed-m` | 0.029 | 5.8 ms | 430 MB | 54.90 |

**Default decision (2026-05-09):** kept — no challenger passed the gate (MRR lift ≥ 0.05 AND p50 ≤ 1.5x baseline). bge-small wins MRR by 0.116 over the best challenger on this corpus. The arctic-embed-m collapse (0.029) likely reflects a missing query/passage prefix protocol that fastembed does not apply automatically; users running BYOM with that family should validate their results before relying on them.
