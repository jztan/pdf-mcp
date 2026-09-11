# Warm/parallelism benchmark — AMD Strix (24 threads) results

Reference numbers for the `_MAX_PARALLEL_WORKERS` cap raise and for why
this branch does NOT change `WARM_TEXT_CAP`/`WARM_EMBED_CAP` or move
embedding into worker processes, despite that being the original plan.
Machine: AMD Ryzen AI 9 HX PRO 375 (12C/24T), 62 GB RAM, Linux, Python
3.12.2 via `uvx`/`uv venv` (NOT the system 3.14 — see "Start method" note
below), fork start method, `pdf-mcp` dev install (`.[dev,fast-ocr]`).
Corpus: a 24-doc synthetic corpus (5-40 pages each, 465 pages total,
generated with `pymupdf`, varied paragraph text) unless noted; not
committed (matches this repo's own convention for benchmark corpora —
see `benchmark_data/corpus_search/manifest.json`'s "local-only" note).

## Start method correction

The plan that led to this benchmark assumed Python 3.14's Linux default
multiprocessing start method (`forkserver`, changed from `fork`) applied
to the real deployment, and that this made `docs/investigated-rejected.md`'s
fork-based numbers stale. It does not apply here: `uvx pdf-mcp` and
`uv venv` both resolve to a uv-managed Python 3.12.2, not the system
3.14, and 3.12's Linux default is still `fork`. Confirmed directly:
`uvx --from pdf-mcp python -c "import multiprocessing;
print(multiprocessing.get_start_method())"` prints `fork`. No action
taken on this point.

## OCR / render: real, substantial headroom past the old cap of 8

`scripts/benchmark_parallel_pages.py --pages 16 --runs 1 --workers 1,4,8,16`
(synthetic corpus, this machine):

| operation | workers | speedup |
|-----------|--------:|--------:|
| OCR (Tesseract, 300 DPI) | 8  | 5.90x |
| OCR (Tesseract, 300 DPI) | 16 | **8.09x**, still climbing |
| Render (200 DPI) | 8  | 4.56x |
| Render (200 DPI) | 16 | **6.04x**, still climbing |

Both operations were still gaining at 16 workers (the top of this run) —
well past the M4 Pro reference numbers in
`benchmark_data/parallel_pages_results.md` (OCR 6.34x, render 1.64x/2.23x
at 8 workers on a 14-CPU machine), consistent with more cores giving more
room before the per-worker spawn cost (~0.3-0.5s) stops being worth it.
This directly motivates raising `_MAX_PARALLEL_WORKERS` past the flat 8.

**Decision:** `_MAX_PARALLEL_WORKERS = min(os.cpu_count() or 8, 16)` —
scales with the host, ceilinged at 16 (as far as this measurement went —
raise it again only with new numbers past 16 workers). No separate floor
below 16 is needed: the worker count actually used is
`min(os.cpu_count(), n_pages, cap)` (`parallel.resolve_workers`), so on
fewer than 16 cores the `os.cpu_count()` term already governs regardless
of what the cap says — an earlier version of this change added a
`max(cpu_count, 8)` floor on the cap itself on the reasoning that it
would prevent any regression below 8 cores, but that floor could never
have changed the outcome (caught in review) and was removed.
`parallel.resolve_workers`'s own env-var contract (`PDF_MCP_MAX_WORKERS`
clamps down, never raises the computed
default) is unchanged and still tested by `tests/test_parallel.py`.

Text extraction (non-OCR) at this small a page count (16) stayed noisy
and inconclusive either way, consistent with the project's own
`docs/investigated-rejected.md` verdict that per-page text-extraction
parallelism is not worth it — no change proposed there.

The `pdf_read_pages(render_dpi=200)` end-to-end path (render parallelism
plus the serial per-page work that stays in the parent — images, tables,
cache writes) *regressed* at this run's page count (1 worker 5.50s, 4
workers 0.92x, 8 workers 0.83x) — expected at 16 pages, right at
`_RENDER_PARALLEL_GATE`'s boundary, where spawn cost is not yet
amortized; not a signal to change the gate.

## Text-warm concurrency: matches the shipped default, no case to change it

`corpus.warm_docs` (text only), 24-doc / 465-page synthetic corpus, via a
local driver mirroring `scripts/benchmark_warm_concurrency.py`'s use of
the real `warm_docs` (not a prototype):

| workers | seconds | s/page | speedup |
|--------:|--------:|-------:|--------:|
| sequential | 17.67 | 0.0380 | 1.00x |
| 4  | 6.63 | 0.0143 | 2.67x |
| 8  | 5.38 | 0.0116 | 3.28x |
| 12 | 5.32 | 0.0114 | 3.32x |
| 16 | 5.32 | 0.0114 | 3.32x |
| 20 | 5.04 | 0.0108 | 3.50x |

Plateaus right around the shipped `WARM_TEXT_CAP = 8` (3.28x), with only
marginal further gain out to 20 workers (3.50x, +7%) — consistent with
the module's own comment ("spawn cost plus cross-process pickling of the
extracted text dominates"). **No change made to `WARM_TEXT_CAP`.**

## Embeddings-warm: investigated, does not scale on this hardware — reverted

The original plan (`this-is-a-pdf-swirling-candle.md` Phase 2) was to
move the encode call from the parent into each warm worker, on the
theory that the shipped architecture's ~4-worker plateau was the parent
single-threading an otherwise-parallelizable encode. Three focused
experiments, in order, overturned that theory:

**1. Pinning the *parent's* onnxruntime thread count** (to leave cores
free for concurrent extraction workers), real `warm_docs` calls, 10-doc /
168-page corpus:

| workers | parent threads | seconds | s/page | speedup |
|--------:|----------------|--------:|-------:|--------:|
| 1 (seq) | default (all cores) | 29.92 | 0.1781 | 1.00x |
| 4  | default | 29.04 | 0.1729 | 1.03x |
| 8  | default | 34.86 | 0.2075 | 0.86x |
| 16 | default | 36.91 | 0.2197 | 0.81x |
| 4  | 4 | 33.76 | 0.2009 | 0.89x |
| 8  | 4 | 36.41 | 0.2167 | 0.82x |
| 16 | 4 | 40.69 | 0.2422 | **0.74x** |

More workers made it *worse* regardless of thread pinning — expected,
since embedding stays serial in the parent either way; more workers only
adds extraction load competing for cores against that unchanged
bottleneck. Pinning fewer threads did not help either.

**2. Thread-based concurrent encode in one process** (does onnxruntime's
`Run()` release the GIL enough for real overlap?), 8 synthetic
72-unit batches, one already-loaded model:

| threads | seconds/doc | speedup |
|--------:|------------:|--------:|
| sequential | 0.442 | 1.00x |
| 2 | 0.401 | 1.10x |
| 4 | 0.394 | 1.12x |
| 8 | 0.362 | 1.22x |

Modest (1.1-1.2x), not the multi-x needed to justify the added
complexity.

**3. True process-based concurrent encode** (separate onnxruntime
sessions, the Phase-2 design itself, isolated from extraction so the
comparison is fair — no double-counted model-load, no extraction
competing for cores), same 8-batch workload:

| workers | threads/worker | wall speedup |
|--------:|----------------:|-------------:|
| sequential | n/a (all cores) | 1.00x |
| 2 | 12 | **1.12x** (best case) |
| 4 | 6  | 1.06x |
| 8 | 3  | 0.96x |
| 4 | unpinned (all cores each) | 0.28x |
| 8 | unpinned (all cores each) | 0.26x |

Even in the best case (2 processes, threads pinned), the ceiling is
~1.1x, and it gets *worse* with more workers. This matches
`docs/configuration.md`'s existing finding for this model on the CPU
path ("one intra-op thread ran as fast as fourteen... memory-bound, not
compute-bound") — extending it from "more threads in one session doesn't
help" to "more sessions across processes doesn't help either," because
both are bound by the same shared memory bandwidth, not by available
cores.

**Decision: reverted.** The worker-side-embedding code (`corpus.py`
extract-and-embed worker, `embedder.py`'s `threads=` plumbing) was
written, then reverted once the numbers came in — moving embedding into
workers is not a win on this hardware, and would have added real
complexity for it: the injected `embed` callable used by every warm
path (and by the hermetic unit tests that fake it to avoid a real model
load) is a Python closure, not picklable across the `spawn` boundary
`_warm_concurrent` requires, so a worker-side embed can only ever call
the real `pdf_mcp.embedder.encode` by name — which two of the existing
tests correctly exercise via a fake closure specifically to stay fast
and hermetic. No `WARM_EMBED_CAP` change either; its comment ("plateaus
at ~4 workers... oversubscribe the encode's own threads") still
describes the shipped pipelined-extraction-overlapping-serial-encode
architecture accurately.

## What this means for "the prewarm never finishes"

Since embedding throughput itself does not scale further on this
hardware, the fix for a corpus warm that never completes is not more
embedding parallelism — it is removing the two hard ceilings that make a
real prewarm require re-issuing the same MCP tool call by hand:
`pdf_corpus_warm`'s `budget_seconds <= 300` and `corpus.CORPUS_MAX_FILES
= 100` per call. `pdf-mcp-warm` (this branch's new CLI entry point) runs
outside any MCP client, so neither ceiling applies, and it warms a whole
folder to completion in one run — see the CLI's own docstring and
`docs/configuration.md`.

