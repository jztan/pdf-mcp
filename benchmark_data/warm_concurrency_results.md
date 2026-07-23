# Concurrent Warm Benchmark: Results

Spike question: should `corpus.warm_docs` (sequential today) become
concurrent, and by how much would it help? Measured via
`scripts/benchmark_warm_concurrency.py` on the corpus_search manifest.
Machine: 14 cores; worker cap 8 (matches `parallel.py`). Cold cache each
run. Design under test: extract in a process pool, keep ALL SQLite writes
in the parent (the project's all-writes-in-parent rule).

## Text-only warm (100 docs, 2238 pages)

| config | wall(s) | docs/s | vs seq | corrupt | txt-diff |
|---|---|---|---|---|---|
| sequential (warm_docs) | 114.2 | 0.88 | 1.00x | | |
| concurrent workers=1 | 116.7 | 0.86 | 0.98x | 0 | 28 |
| concurrent workers=2 | 62.1 | 1.61 | 1.84x | 0 | 27 |
| concurrent workers=4 | 38.7 | 2.58 | 2.95x | 0 | 25 |
| concurrent workers=8 | 27.9 | 3.59 | **4.10x** | 0 | 28 |

Text warm is CPU-bound in PyMuPDF extraction and parallelizes cleanly:
4.1x at 8 workers, zero corruption. Extrapolated to 1000 docs that is
~19 min sequential to ~4.6 min concurrent.

## Embeddings warm (40 docs, 566 pages)

| config | wall(s) | docs/s | vs seq | corrupt | txt-diff |
|---|---|---|---|---|---|
| sequential (warm_docs) | 36.6 | 1.09 | 1.00x | | |
| concurrent workers=1 | 40.7 | 0.98 | 0.90x | 0 | 7 |
| concurrent workers=2 | 27.0 | 1.48 | 1.35x | 0 | 6 |
| concurrent workers=4 | 22.4 | 1.78 | 1.63x | 0 | 5 |
| concurrent workers=8 | 21.3 | 1.88 | **1.72x** | 0 | 6 |

Embeddings warm is dominated by the encode, which runs as one parent-side
batched call (fastembed/onnxruntime already threads it internally). Only
the extraction half parallelizes, so the win is modest (1.72x) and
plateaus at ~4 workers: past that, extraction processes oversubscribe
cores against the encode's own threads (workers 4 to 8: 22.4s to 21.3s,
almost flat).

## Column legend

- **corrupt**: docs with a wrong page count, or an empty page where the
  sequential reference had text. This is the real corruption gate. It is
  **0 in every run** across both modes and all worker counts: the
  concurrent design (extract in workers, write in parent) is correct.
- **txt-diff**: docs whose cached text differs from the sequential run.
  This is NOT corruption and NOT caused by concurrency: it is the
  extractor's own nondeterminism. `extract_text_from_page`'s multi-column
  clip branch calls PyMuPDF `get_text(clip, sort=True)`, which is
  intermittently nondeterministic on some multi-column pages (verified:
  same page, same stable column boxes, occasionally different char count).
  The count is identical at workers=1 (a single subprocess, no
  parallelism), confirming parallelism is not the cause. It affects ~25 to
  28 of 100 docs at corpus scale because ~30% of pages take that branch.

## Decisions

1. **Adopt concurrent warm.** Extract-in-workers, write-in-parent is
   correct (0 corruption) and a large win for text warm (4.1x), a modest
   one for embeddings warm (1.7x, encode-bound).
2. **Gate the pool by doc count.** workers=1 is slightly slower than
   sequential (spawn + IPC overhead with no parallelism payoff), so a
   small corpus should stay sequential, mirroring `parallel.py`'s existing
   page-count gate.
3. **Best worker count is mode-dependent.** Text: scale to the cap (8).
   Embeddings: ~4 workers; more oversubscribes the encode threads.
4. **Prerequisite for a testable implementation:** the extractor
   nondeterminism (txt-diff) blocks a clean `concurrent == sequential`
   regression test. Fix that first (see backlog); the corruption gate
   (page counts, non-empty) is the invariant that is already clean and
   testable today.
