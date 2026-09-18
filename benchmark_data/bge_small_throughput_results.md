# bge-small remote throughput — fastembed CPU vs Vulkan llama-server, real warm chunks

Settles the second of jztan's two asks on [jztan/pdf-mcp#42](https://github.com/jztan/pdf-mcp/issues/42):
> "a throughput run on real warm chunks (~300 tokens, e.g. pages/corpus)
> instead of 30-word passages."

The previous (dropped) `feature/external-embedder` attempt at this benchmark
(`scripts/benchmark_remote_embedder.py` on that branch) used synthetic
30-word passages and never ran cosine parity at all — exactly the two gaps
jztan called out. This run uses real page text and the same chunker
production code uses.

## Setup

| | |
|---|---|
| Host | Linux, AMD iGPU (Vulkan) |
| Local backend | `fastembed` 0.8.0, `BAAI/bge-small-en-v1.5`, CPU |
| Remote backend | `llama-server` (Vulkan build), `bge-small-en-v1.5-q8_0.gguf`, `--pooling cls` |
| Remote launch | `llama-server -m bge-small-en-v1.5-q8_0.gguf --embedding --pooling cls --port 8712 -ngl 99 --host 127.0.0.1 --parallel 8 -c 4096` |
| Passages | 612 real page-chunk windows from `pages/corpus/*.pdf` (all 6 real PDFs, every page), via `pdf_mcp.extractor.chunk_page_text` — the same chunker `corpus.warm_docs`/real embedding warming uses |
| Used | 600 / 612 (12 skipped — see Caveats) |
| Avg passage length | 979 chars (~245 tokens by the 4-chars/token estimate) |
| Client batch size | 16 texts/request (`[embedding].batch_size` default) |
| Concurrency levels | 1, 4, 8 (`[embedding].max_concurrency` range) |
| Script | `scripts/benchmark_bge_small_throughput.py` |

## Method

`collect_chunks` walks every page of every corpus PDF through the exact same
`extractor.chunk_page_text` sub-page windowing real warm embedding uses
(`extractor.page_embedding_units`), keeping windows ≥200 characters. Each
candidate's REAL subword token count is checked against the server's own
`/tokenize` endpoint (not chunk_page_text's char-based estimate) and dropped
if it would exceed bge-small's 512-token context — see Caveats.

Local fastembed throughput is one `embedder.encode` call over all 600 texts
(fastembed handles its own internal batching; there's no client-side
concurrency knob to sweep for it, unlike the remote path). Remote throughput
is measured at `max_concurrency` 1/4/8 via the shipped
`remote_embedder.encode`'s bounded thread pool, same code path
`server.py`/`corpus.py` use in production.

## Results (600 real ~300-token chunks)

| backend | concurrency | time | texts/s | speedup vs fastembed CPU |
|---|---:|---:|---:|---:|
| fastembed CPU | (n/a — 1 process) | 19.29 s | **31.1** | 1.00× (baseline) |
| remote (llama-server, Vulkan) | 1 | 4.54 s | **132.3** | **4.25×** |
| remote (llama-server, Vulkan) | 4 | 4.04 s | **148.5** | **4.77×** |
| remote (llama-server, Vulkan) | 8 | 4.11 s | **146.1** | **4.70×** |

## Verdict

**The remote Vulkan backend is a genuine 4.2–4.8× throughput win on real
warm chunks**, not just on short synthetic passages — this is the number
that matters for a cold-cache PDF's first search, where every page/chunk in
the document gets embedded. Concurrency 4 edges out 8 slightly (148.5 vs
146.1 texts/s) on this 8-slot server configuration; the gain from
concurrency 1→4 (4.25×→4.77×) is real, but 4→8 is flat to slightly negative
here, most likely because this small/fast model's per-request overhead
(HTTP round-trip, batching bookkeeping) starts to dominate before the GPU
itself saturates. `[embedding].max_concurrency = 4` (the shipped default)
is a reasonable choice on this hardware; a different GPU/driver might shift
where the curve flattens.

## Caveats

- **12 of 612 candidate chunks were skipped.** `chunk_page_text`'s ~300-token
  target is a character-count estimate (4 chars/token); measured against
  this real corpus, a handful of dense NIST/USDA pages tokenize as low as
  ~1.4 chars/token, so some real ~1200-character chunks are 700-800+ real
  subword tokens — over bge-small's hard 512-token context even though the
  character estimate said ~300. `llama-server` hard-errors on an oversized
  input rather than truncating, so these are filtered by REAL token count
  (via `/tokenize`) before being sent, not silently dropped without
  explanation. This is a genuine characteristic of chunk_page_text's
  estimate on dense technical text, independent of the remote backend --
  fastembed's local path would truncate such an input to 512 tokens
  instead of erroring, which is worth being aware of if adopting a strict
  remote endpoint that returns an error on overflow.
- **Single-host measurement.** One Linux machine, one AMD iGPU, one driver
  stack (Vulkan). Absolute texts/s will differ elsewhere; the qualitative
  finding (remote wins by ~4-5× on real chunks, concurrency 4-8 both
  saturate it) is what should transfer.
- **Not a network-latency stress test.** `base_url` here is loopback
  (127.0.0.1); a real network hop to a LAN or remote host adds per-request
  latency this measurement does not capture.

Reproduce: `uv run python scripts/benchmark_bge_small_throughput.py`
(requires a real `llama-server` running per Setup above — this is not run
in CI).
