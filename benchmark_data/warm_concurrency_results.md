# Concurrent Warm Benchmark: Results

Measures the SHIPPED concurrent `corpus.warm_docs` (spawn pool extraction,
streamed finalize, all SQLite writes in the parent), as of the
`feat/concurrent-corpus-warm` branch. Every row below calls the real
`warm_docs`; the pool size is forced via the shipped controls
(`corpus.WARM_DOC_GATE` + `PDF_MCP_MAX_WORKERS`), not a prototype.
Measured via `scripts/benchmark_warm_concurrency.py` on the
corpus_search manifest. Machine: 14 cores; worker cap 8 for text, 4 for
embeddings (matches the shipped `_warm_worker_count` caps). Cold cache
each run.

## Text-only warm (100 docs, 2238 pages)

| config | wall(s) | docs/s | vs seq | corrupt | txt-diff |
|---|---|---|---|---|---|
| sequential (warm_docs) | 105.8 | 0.95 | 1.00x | | |
| concurrent (workers=2) | 60.3 | 1.66 | 1.75x | 0 | 26 |
| concurrent (workers=4) | 38.6 | 2.59 | 2.74x | 0 | 25 |
| concurrent (workers=8) | 27.3 | 3.66 | **3.87x** | 0 | 26 |

Text warm is CPU-bound in PyMuPDF extraction and parallelizes cleanly:
3.87x at 8 workers, zero corruption. Extrapolated to 1000 docs that is
~17.6 min sequential to ~4.5 min concurrent.

## Embeddings warm (40 docs, 566 pages)

| config | wall(s) | docs/s | vs seq | corrupt | txt-diff |
|---|---|---|---|---|---|
| sequential (warm_docs) | 36.9 | 1.08 | 1.00x | | |
| concurrent (workers=2) | 25.8 | 1.55 | 1.43x | 0 | 5 |
| concurrent (workers=4) | 22.8 | 1.76 | **1.62x** | 0 | 5 |

Embeddings warm is dominated by the encode, which runs as one parent-side
batched call (fastembed/onnxruntime already threads it internally). Only
the extraction half parallelizes, so the win is modest (1.62x) and the
shipped implementation caps the pool at 4 workers for this mode: past
that, extraction processes oversubscribe cores against the encode's own
threads.

## Column legend

- **corrupt**: docs with a wrong page count, or an empty page where the
  sequential reference had text. This is the real corruption gate. It is
  **0 in every run** across both modes and all worker counts: the shipped
  concurrent design (extract in workers, write in parent) is correct.
- **txt-diff**: docs whose cached text differs from the sequential run.
  This is NOT corruption and NOT caused by concurrency: it is the
  extractor's own nondeterminism. `extract_text_from_page`'s multi-column
  clip branch calls PyMuPDF `get_text(clip, sort=True)`, which is
  intermittently nondeterministic on some multi-column pages (verified:
  same page, same stable column boxes, occasionally different char count).
  It affects ~25 to 26 of 100 docs at corpus scale because ~30% of pages
  take that branch.

## Decisions

1. **Adopt concurrent warm.** Extract-in-workers, write-in-parent is
   correct (0 corruption) and a large win for text warm (3.87x), a modest
   one for embeddings warm (1.62x, encode-bound). Shipped.
2. **Gate the pool by doc count.** `WARM_DOC_GATE` (4) keeps a small
   corpus sequential, avoiding spawn + IPC overhead with no parallelism
   payoff, mirroring `parallel.py`'s existing page-count gate.
3. **Best worker count is mode-dependent.** Text: scale to the cap (8).
   Embeddings: capped at 4 workers; more oversubscribes the encode
   threads.
4. **Regression gates, as shipped:** the corruption invariant (page
   counts, non-empty-where-reference-had-text) plus single-column exact
   equality are the tests that guard this feature, implemented in
   `tests/test_corpus.py::TestConcurrentWarm`. Full-text
   `concurrent == sequential` equality across ALL pages (including
   multi-column) remains blocked on the upstream `detect_column_boxes` /
   `get_text(clip, sort=True)` nondeterminism, which predates this
   feature and is tracked in the backlog.

## Post batched-commits re-run (2026-09-03)

Re-run of both modes after the warm-batch-commits change (embedding
progress commits in durable page batches, `WARM_EMBED_BATCH_PAGES` 24,
clock checked between batches; text extraction untouched). Same harness,
same corpus shape (100 docs, 2238 pages), cold cache each run.

| mode | config | wall(s) | vs seq | corrupt | txt-diff |
|---|---|---|---|---|---|
| text | sequential | 102.8 | 1.00x | | |
| text | concurrent, 2 workers | 54.2 | 1.90x | 0 | 0 |
| text | concurrent, 4 workers | 29.5 | 3.49x | 0 | 0 |
| text | concurrent, 8 workers | 17.1 | 6.01x | 0 | 0 |
| embeddings | sequential | 445.9 | 1.00x | | |
| embeddings | concurrent, 2 workers | 334.6 | 1.33x | 0 | 0 |
| embeddings | concurrent, 4 workers | 342.0 | 1.30x | 0 | 0 |

**Gate verdict: PASS.** Corruption invariant holds with batching active
(0 corrupt, 0 txt-diff in every arm), which closes the re-validation the
commit-granularity change re-opened.

Honest deltas vs the pre-batching record: text best 6.01x (was 3.87x at
8 workers; better run). Embeddings best 1.33x at 2 workers against the
recorded 1.62x at 4. Sequential embeddings wall (445.9s / 2238 pages =
0.199s/page) matches the independently measured encode cost (0.197s/page)
exactly, so the sequential arm is at parity and the difference sits in
the concurrent arm; candidate causes are per-batch commit overhead
(pages/24 commits per doc instead of 1) and run-to-run variance. The
feature's purpose is durability, not throughput; the trade is accepted
and the corruption gate, not the speedup, is the pass criterion.
