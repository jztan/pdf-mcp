# bge-small remote cosine parity — fastembed vs Q8_0 GGUF over llama-server

Settles the first of jztan's two asks on [jztan/pdf-mcp#42](https://github.com/jztan/pdf-mcp/issues/42):
> "cosine against fastembed on the Q8_0 GGUF, since quantization can shift
> vectors on top of any pooling difference."

## Setup

| | |
|---|---|
| Local backend | `fastembed` 0.8.0, `BAAI/bge-small-en-v1.5`, CPU, CLS pooling (fastembed's built-in pooling for this model) |
| Remote backend | `llama-server` (Vulkan/iGPU build), `bge-small-en-v1.5-q8_0.gguf` (Q8_0 quantization), `--pooling cls` |
| Remote launch | `llama-server -m bge-small-en-v1.5-q8_0.gguf --embedding --pooling cls --port 8712 -ngl 99 --host 127.0.0.1` |
| Passages | 36 real ~300-token page-chunk windows, 6 passages/PDF, from `pages/corpus/*.pdf` (6 real PDFs: GAO, NASA Artemis, NIST router security, NIST zero trust, USDA agri policy, Fed consumer context) |
| Chunking | `pdf_mcp.extractor.chunk_page_text` (the SAME chunker `corpus.warm_docs`/real page-embedding warming uses) — not synthetic passages |
| Script | `scripts/benchmark_bge_small_cosine_parity.py` |
| Threshold | 0.99 (jztan's number) on the **minimum** per-passage cosine |

## Method

For each passage: embed once via local fastembed (CLS-pooled, L2-normalized)
and once via the remote GGUF endpoint (L2-normalized client-side, matching
`embedder.py`'s convention of never trusting a backend's raw norm). Cosine
similarity is the dot product of the two normalized vectors.

`--pooling cls` is not optional here: fastembed pools bge-small with CLS, not
mean (see `docs/investigated-rejected.md`'s MLX entry and
[`mlx_backend_results.md`](mlx_backend_results.md), which measured cosine 0.89
between CLS and mean pooling on the same weights). Running this without
`--pooling cls` would measure a pooling-mismatch confound, not the
quantization effect this benchmark exists to isolate.

A handful of real chunks in this corpus tokenize far below chunk_page_text's
4-chars/token estimate (as low as ~1.4 chars/token on dense NIST/USDA text) and
land over bge-small's 512-token context; those are filtered out via the
server's own `/tokenize` endpoint before scoring, not by character count.

## Results (36 passages, 6 PDFs)

| metric | value |
|---|---:|
| mean cosine | **0.99993** |
| min cosine | **0.99989** |
| max cosine | 0.99995 |
| passages below 0.99 threshold | **0 / 36** |
| verdict | **PASS** |

Per-passage cosine ranged 0.99989–0.99995 across all 6 documents — no
document, page, or chunk position stood out as an outlier; the full
per-passage table is reproduced by the script (`--json` dumps it).

## Verdict

**Quantization to Q8_0 costs essentially nothing beyond floating-point noise.**
With pooling held constant (CLS on both sides), the Q8_0 GGUF's vectors are
cosine-indistinguishable from full-precision fastembed's (min 0.99989, four
nines away from 1.0) — comfortably clears jztan's 0.99 bar with more than an
order of magnitude of margin. This isolates the earlier MLX-backend pooling
confound (cosine 0.89) from quantization: once pooling matches, quantization's
own contribution to vector drift is negligible for this model/quantization
combination.

Reproduce: `uv run python scripts/benchmark_bge_small_cosine_parity.py`
(requires a real `llama-server` running per Setup above — this is not run in
CI).
