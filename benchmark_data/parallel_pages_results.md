# Parallel page processing benchmark — results

Reference numbers for `scripts/benchmark_parallel_pages.py`. Sections are
labelled with machine and multiprocessing start method, because fork (Linux)
vs spawn (macOS/Windows) materially changes process-pool results. Reproduce
with `python scripts/benchmark_parallel_pages.py` (add `--corpus` for the real
arXiv corpus).

## Takeaway

Parallelism pays in proportion to per-page work vs. ~per-worker startup cost
(spawn ≈ 0.3–0.5 s/worker). Threads never help (GIL-bound); the process pool is
the only viable parallelism, and chunking (one open per worker vs. per page)
adds nothing — PDF opens are cheap; the cost is worker startup + compute.

| operation | per-page work | process-pool result | ship? |
|-----------|---------------|---------------------|-------|
| Text (non-OCR) | ~4 ms  | 0.13–0.24x (loses on spawn; ≤1.6x only on Linux/fork + many pages) | **No** |
| Render         | ~60 ms | 1.6x (synthetic) – 2.2x (real arXiv) at 8 workers; sublinear, machine-sensitive | **Yes, modest** |
| OCR            | ~2–9 s | **6.3x at 8 workers** (near-linear); threads impossible (Leptonica not thread-safe) | **Yes, biggest win** |

## Apple M4 Pro — 14 CPUs, spawn, synthetic corpus (24 pages), runs=3

### Text extraction (non-OCR)

| mode | min (s) | median (s) | pages/s | speedup |
|------|--------:|-----------:|--------:|--------:|
| sequential | 0.087 | 0.087 | 275.9 | 1.00x |
| threaded x4 | 0.106 | 0.106 | 227.3 | 0.82x |
| process x4 | 0.653 | 0.660 | 36.7 | 0.13x |
| process-chunk x4 | 0.647 | 0.647 | 37.1 | 0.13x |
| process x8 | 0.695 | 0.695 | 34.6 | 0.13x |

### Render @ 200 DPI

| mode | min (s) | median (s) | pages/s | speedup |
|------|--------:|-----------:|--------:|--------:|
| sequential | 1.442 | 1.446 | 16.6 | 1.00x |
| threaded x8 | 1.466 | 1.468 | 16.4 | 0.98x |
| process x2 | 1.261 | 1.264 | 19.0 | 1.14x |
| process x4 | 1.024 | 1.030 | 23.4 | 1.41x |
| process x8 | 0.881 | 0.896 | 27.3 | 1.64x |
| process-chunk x8 | 0.879 | 0.884 | 27.3 | 1.64x |

### OCR @ 300 DPI (Tesseract)

| mode | min (s) | median (s) | pages/s | speedup |
|------|--------:|-----------:|--------:|--------:|
| sequential | 53.677 | 54.812 | 0.4 | 1.00x |
| process x2 | 28.312 | 28.318 | 0.8 | 1.90x |
| process x4 | 14.842 | 14.853 | 1.6 | 3.62x |
| process x8 | 8.471 | 8.569 | 2.8 | 6.34x |

## Corroboration: same M4 Pro, real arXiv corpus (`--corpus`, 6 docs, 37 pages)

Real PDF content vs. synthetic changes little — confirming start method, not
content, drove the earlier fork-vs-spawn discrepancy:

- Text: process x4 = 0.22x (same verdict — harmful).
- Render: process x8 = **2.23x** (slightly better than synthetic's 1.64x; real
  pages carry more per-page work to parallelize).

OCR is always measured on a synthetic scanned PDF — real arXiv papers have a
text layer, so `--corpus` skips OCR.
