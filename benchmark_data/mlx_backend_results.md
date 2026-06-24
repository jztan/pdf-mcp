# MLX (Apple-GPU) embedding backend evaluation

> Part of the [embedding evaluation summary](embedding_evaluation_summary.md).

Benchmark-first evaluation of an optional MLX backend, run **before** adopting
it. MLX is a *performance* backend (it does not change the prefixes/model), so
it is judged on two axes: embedding latency, and output equivalence vs the
fastembed/ONNX path.

Harness: `scripts/archive/benchmark_mlx_backend.py` — implements the MLX encode path
faithfully (single `batch_encode_plus`, mean-pooled `text_embeds`) and compares
against the production fastembed path on 58 real page-text chunks from 4 PDFs.
**No production code changed.** Host: Apple M4 Pro.

## Latency — MLX is a genuine win on Apple Silicon

| Model | fastembed/CPU | mlx/GPU | Speedup |
|-------|---------------|---------|---------|
| `bge-small-en-v1.5` (default) | 2692 ms (21.5 chunks/s) | 968 ms (59.9 chunks/s) | **2.78×** |
| `multilingual-e5-large`        | 17578 ms (3.3 chunks/s) | 6882 ms (8.4 chunks/s) | **2.55×** |

pdf-mcp embeds every page on the first search of a PDF, so this ~60% cut in
cold-cache embedding time is directly user-visible on Apple Silicon. The
speedup is real and reproducible.

## Output equivalence — model-dependent, and NOT a drop-in for the default

Both backends compared on the same model + raw text, vectors L2-normalized
before cosine (do not assume either backend returns unit vectors — see below).

| Model | raw norm (fe / mlx) | row cosine | rank overlap@5 | verdict |
|-------|---------------------|-----------|----------------|---------|
| `bge-small-en-v1.5` (default) | 1.000 / 1.000 | **0.894** | **0.84** | **DIVERGES** |
| `multilingual-e5-large`        | **27.68** / 1.000 | 1.000 | 1.00 | EQUIVALENT |

- **The default `bge-small` DIVERGES.** fastembed runs bge with **CLS pooling**;
  the MLX path **mean-pools every model**. Same weights, different
  pooling → genuinely different embeddings (cosine 0.89) and 16% of
  nearest-neighbour rankings shift. **So an MLX backend does NOT leave retrieval
  unchanged for the default model.** A safe MLX backend would have to match
  pooling per model (CLS for bge, mean for e5), which this MLX path does not.
- **`e5-large` is EQUIVALENT** once normalized (cosine 1.000, rank overlap 1.00).
  The MLX path is geometrically faithful for mean-pooled models.

### Proof of the pooling cause (not just the symptom)

The table above shows bge *diverges* but does not by itself prove *why*.
`scripts/archive/benchmark_pooling_attribution.py` settles it: it reconstructs both a
CLS-pooled and a mean-pooled sentence vector from the model's own
`last_hidden_state`, then checks which pooling each backend reproduces.

| Model | fastembed ≡ CLS | fastembed ≡ mean | MLX ≡ mean | MLX ≡ CLS | fe vs MLX |
|-------|-----------------|------------------|-----------|-----------|-----------|
| `bge-small-en-v1.5` | **1.0000** | 0.9508 | **1.0000** | 0.9507 | 0.9508 |
| `multilingual-e5-large` | 0.9161 | **1.0000** | **1.0000** | 0.9161 | 1.0000 |

Each backend's output matches exactly one reconstructed pooling at cosine
**1.0000**: fastembed pools **bge by CLS**, the MLX path pools **every model by
mean**. The bge cross-backend gap (0.9508) *is* precisely the CLS↔mean
distance — nothing else. For e5 both backends mean-pool, so they are identical.
Mechanism confirmed end-to-end.

(The cross-backend bge cosine is 0.9508 on 5 short clean sentences here vs 0.894
on 58 truncated real page-chunks above — same mechanism, larger CLS↔mean gap on
longer multi-topic text. The 0.894 figure is the representative one for real
PDF pages.)

## Bonus finding (independent of the backend) — e5 vectors are unnormalized

fastembed 0.8 returns **unnormalized** `multilingual-e5-large` vectors
(mean norm **27.68**, not 1.0), a side effect of its CLS→mean-pooling change.
pdf-mcp assumes `dot product == cosine` and scores semantic matches with a raw
`matrix @ query_vec` (`server.py:1579`), no re-normalization. Consequences for
any e5 user (the default bge is unaffected — it stays unit-norm):

- reported `score` is ~784× inflated nonsense, not a cosine;
- `low_confidence = score < 0.5` (`server.py:1595`) is **always False** — the
  confidence signal is silently dead;
- ranking is only mildly distorted (e5 page norms are near-uniform ~27), so
  retrieval still mostly works, masking the bug.

A one-line safeguard — L2-normalize in `embedder.encode`/`encode_query`
regardless of model — restores the contract for any model fastembed leaves
unnormalized. The MLX path already normalizes, so it incidentally avoids this.

## Alternative checked — CoreML execution provider (no second backend)

Before concluding, the simpler alternative was measured: pass
`providers=['CoreMLExecutionProvider', 'CPUExecutionProvider']` to fastembed,
keeping its own model + pooling (so no divergence risk) and adding no
dependency. Harness: `scripts/archive/benchmark_coreml_provider.py`.

| Model | CPU EP | CoreML EP | Speedup | row cosine vs CPU |
|-------|--------|-----------|---------|-------------------|
| `bge-small` (default) | 2651 ms | 2578 ms | **1.03×** | 1.00000 |
| `e5-large`            | 15856 ms | 16095 ms | 0.99×  | 1.00000 |

Output is perfectly equivalent (cosine 1.00000, as expected — same pooling), but
**there is no speedup**. CoreML supports only 880 of the model's 1237 graph
nodes → **193 partitions**; the BERT export fragments so badly that CPU↔ANE
data-shuffling cancels any gain (bge 1.03×). For e5 the CoreML EP fails to
initialise outright (`EP Error SystemError: 20`) and silently falls back to CPU.

So the safe path doesn't accelerate, and the fast path (MLX) isn't safe.

## Alternative checked — MLX with a small mean-pooled model

If MLX mean-pools everything, pair it with a model whose *native* pooling is
mean — then MLX is correct and the pooling-parity layer disappears. Candidate:
`sentence-transformers/all-MiniLM-L6-v2` (384-dim, 92 MB, mean pooled). Harness:
`scripts/archive/benchmark_minilm_mlx.py`. All three axes measured:

1. **Equivalence: perfect.** fastembed pools MiniLM by mean (cosine 1.0000 vs
   mean, 0.55 vs CLS), unit-norm; fastembed vs MLX = **1.0000**. MLX *is* correct
   for this model — the pooling blocker is gone.
2. **Latency: MLX is slower — 0.80×** (260 ms CPU vs 324 ms GPU). MiniLM is small
   enough (6 layers, 384-dim) that GPU dispatch/transfer overhead exceeds the
   compute saved. The MLX win on bge/e5 only came from their larger size.
3. **Retrieval: MiniLM is worse than bge — −0.106 MRR** (0.511 vs 0.616) on the
   22-scenario corpus.

Speedup and correct-pooling are in tension via model size: bge (small, CLS) →
MLX fast but wrong; e5 (large, mean) → MLX fast + correct but 2 GB / slow on CPU
/ heavy cache; MiniLM (small, mean) → MLX correct but *slower* than CPU. No model
is simultaneously small enough to be a good default, mean-pooled so MLX is
correct, and large enough for MLX to accelerate.

This leg also settles the previously-unmeasured model comparison: **bge-small
(MRR 0.616) beats MiniLM (0.511) and e5-large (~0.53)** — its default status is
earned by measurement.

## Verdict

- **Latency:** MLX delivers a real 2.5–2.8× speedup on Apple Silicon.
- **But it is not a safe drop-in:** it silently changes retrieval for the
  default `bge` model (pooling mismatch). Adopting it would require per-model
  pooling parity, which the evaluated MLX path doesn't implement — so it's a
  larger change than it looks, gated to Apple Silicon, against a maintenance cost.
- **No free-lunch alternative:** CoreML EP is equivalent but gives no speedup on
  these BERT-family models; MLX with a small mean-pooled model (MiniLM) is
  correct but *slower* than CPU and retrieves worse than bge. There is no cheap,
  safe way to speed up embedding — the only fast option (MLX on a large model)
  costs correctness work and a heavy default.
- **bge-small on fastembed CPU is the right configuration** — confirmed best on
  retrieval (MRR 0.616 vs MiniLM 0.511, e5 ~0.53), already correctly pooled and
  normalized. No change to the embedding path is warranted.
- **Highest-value takeaway is the normalization bug**, which is worth fixing on
  its own merits and affects e5 users today regardless of any backend choice.
