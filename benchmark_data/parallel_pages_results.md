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

## End-to-end `pdf_read_pages(render_dpi)` — same M4 Pro, spawn, 24 pages synthetic

This section measures the **real shipped path**: render parallelism plus the
serial per-page work that stays in the parent (`extract_images_from_page`,
`extract_tables_from_page` / `find_tables`, cache writes). The isolated render
numbers above overstate the gain; Amdahl's law applies here.

### End-to-end pdf_read_pages(render_dpi=200): wall time including serial text/images/tables extraction per page.

| workers | wall (s) | speedup |
|--------:|---------:|--------:|
| 1 | 4.155 | 1.00x |
| 4 | 3.108 | 1.34x |
| 8 | 2.919 | 1.42x |

### Gate decision

Both 4 and 8 workers clear the ~1.3x end-to-end threshold (1.34x and 1.42x
respectively), so render dispatch is **enabled**. `_RENDER_PARALLEL_GATE = 16`:
at ≥16 pages the ~0.5 s/worker spawn cost is well-amortized; below that the
marginal gain does not justify the overhead.

## Real-document OCR — UNLV/ISRI corpus (`scripts/benchmark_ocr_corpus.py`)

The synthetic OCR number above (6.3x) is a *worst-case-dense upper bound*. To
get honest, representative numbers we benchmark the canonical Tesseract corpus —
**UNLV/ISRI** 300 dpi bitonal scans with ground truth, split by document class —
on the **real shipped path** (`pdf_read_pages(ocr=True)`). The benchmark script
imports `pdf_mcp.server` at module top *on purpose* so each spawned worker
re-imports the server exactly like the real STDIO deployment does (see "Spawn
regime" below); these are real-deployment numbers, not a library best case.
Apple M4 Pro, spawn, 8 pages/class, 8 workers:

| class | document type | sec/page (seq) | speedup ×8 | par==seq | word-recall vs GT |
|-------|---------------|---------------:|-----------:|:--------:|------------------:|
| bus   | business letters (sparse) | 0.76 s | **2.40x** | yes | 100% |
| news  | newspapers                | 1.15 s | **2.32x** | yes |  94% |
| mag   | magazines (dense)         | 1.58 s | **3.26x** | yes |  91% |

**Speedup scales with per-page density** — denser pages give the pool more work
to amortize spawn overhead against. `par==seq` confirms parallel OCR output is
byte-identical to sequential; `word-recall` (order-insensitive multiset overlap
of OCR vs ISRI ground truth) confirms Tesseract quality is unaffected.

### Honest range (reconciling all three corpora)

| corpus | sec/page | speedup | what it represents |
|--------|---------:|--------:|--------------------|
| synthetic scanned | ~6.7 s | 6.3x | artificially dense **upper bound** |
| **UNLV/ISRI** | 0.76–1.58 s | **2.3–3.3x** | **typical real scanned documents** |
| very sparse/light scans (e.g. low-res worksheets) | ~0.8 s | ~1.3x | low-end (spawn dominates; large color images add memory contention) |

**Honest claim: parallel OCR delivers ~2–3x on typical real scanned documents**,
up to ~6x on very dense pages, down to ~1.3x on sparse/light scans. The 6.3x
figure alone overstates the typical case.

### Spawn regime — why this is a separate script

The isolation benchmarks above measure *cheap* worker spawns because their
`__main__` (the benchmark script) imports only `pdf_mcp.extractor` (PyMuPDF).
The **real STDIO server** is different: `__main__` is `server.py`, and the spawn
start method re-imports `__main__` per worker, so every worker pays
`import fastmcp` (~0.5 s). Measured 8-worker pool startup:

| `__main__` regime | pool spawn |
|---|---|
| extractor-only (isolation benchmarks above) | 0.11 s |
| server-loaded (real deployment, this corpus) | 0.78 s |

Investigated mitigations: **lazy module-level singletons do *not* help** (the
cost is `import fastmcp`, not constructing `PDFCache`/`PDFConfig`, which is
~0.01 s). A **forkserver** start method cuts it to ~0.50 s (forks from a clean
preloaded process instead of re-importing `__main__`) but adds cross-platform
complexity (no Windows) and fork-safety risk for a modest ~0.28 s/call gain — not
adopted. The ~0.5 s/call server-reload tax is largely inherent to running a
process pool from inside an MCP server under `spawn`; it is bounded, only paid on
cache-cold parallel calls, and is already reflected in the numbers above.

Reproduce: `python scripts/benchmark_ocr_corpus.py` (downloads the ISRI corpus
on first run into the gitignored `benchmark_data/.isri_cache/`; needs Tesseract).
