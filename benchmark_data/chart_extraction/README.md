# Chart-extraction benchmark (issue #23)

Feasibility prototype + benchmark for extracting `(x, y)` data from charts in
born-digital PDFs — the "auto WebPlotDigitizer" ask in
[issue #23](https://github.com/jztan/pdf-mcp/issues/23).

This is a **benchmark artifact, not shipped code**. It validates the "Approach A"
architecture (exact vector-geometry extraction + chart-type classifier + an
agent-answered hint protocol, with hard gates that decline rather than emit
wrong numbers). The go/no-go verdict and failure-mode catalogue are in
[`../chart_digitization_feasibility.md`](../chart_digitization_feasibility.md);
this directory is the runnable evidence behind it.

## What's here

| File | Purpose |
|---|---|
| `src/pdf_mcp/chart_extractor.py` | Chart extraction module (promoted to production). `extract_charts(pdf, page_num, hints=None)` → `ok` / `needs_hint` / `declined`. Pure vector geometry for calibration + coordinates; hints are semantic enums only (never values). Imported by benchmark scripts below. |
| `gen_synthetic.py` | Regenerates `syn_corpus/` — 10 chart archetypes with exact ground truth. Only step needing matplotlib. |
| `bench_synthetic.py` | Scores the prototype on `syn_corpus/` with an oracle agent (answers hints correctly). Reports accuracy + wrong-emit rate. |
| `bench_real.py` | Scores it on the arXiv chart-signature pages in `../.reading_order_pdfs/`. Replays **recorded** agent (vision) hint answers so it's deterministic, and prints error vs hand-verified ground truth. |
| `bench_discovery.py` | Sweeps EVERY corpus page with the chart-signature check (the `detect_charts` discovery signal): recall/false-positives vs adjudicated labels, flag rate, per-page runtime. |
| `syn_corpus/` | Generated synthetic PDFs + `ground_truth.json` (committed so benchmarks run without matplotlib). |
| `RESULTS.md` | Snapshot of the last run + the per-page adjudication (incl. the manually-checked ones). |

## Running

Needs only `pymupdf` + `numpy` (already in the project venv):

```bash
uv run python benchmark_data/chart_extraction/bench_synthetic.py
uv run python benchmark_data/chart_extraction/bench_real.py

# one page, ad hoc:
uv run python -c "import pymupdf, json; from pdf_mcp import chart_extractor as ce; \
d = pymupdf.open('benchmark_data/.reading_order_pdfs/1807.11632.pdf'); \
print(json.dumps(ce.extract_charts(d, 3), indent=1, default=str))"
```

Regenerating the synthetic corpus (only if you change `gen_synthetic.py`) needs
matplotlib:

```bash
python benchmark_data/chart_extraction/gen_synthetic.py
```

### What travels with the repo vs. what's fetched

- **Synthetic corpus is committed** (`syn_corpus/`), so `bench_synthetic.py` runs
  fully offline from a clean checkout.
- **Real arXiv PDFs are NOT committed** — the `benchmark_data/.reading_order_pdfs/`
  corpus is gitignored (same as the reading-order benchmark). `bench_real.py`
  **downloads the pages it needs from arxiv.org by ID on first run** (cached
  thereafter), mirroring `scripts/benchmark_reading_order.py`. So the real-page
  benchmark needs network the first time; after that it's offline. If a fetch
  fails, that case prints SKIP.
- The real-page numbers are also snapshotted in `RESULTS.md`, so the results are
  referenceable even without re-fetching.

## Headline results (v4)

- **Synthetic:** 0.27% mean error on emitted series, **0 / 10 wrong-emit**, correct
  declines on the crossing-line and decoy-diagram cases.
- **Real (14 chart-signature pages, agent-in-loop):** **0 / 14 wrong-emit.**
  3 ground-truth-verified GOOD (0802 p12 bars 0.1–3.1% vs caption; 0811 p31 lines;
  1807 p4 dual-axis blue 1.5% / red 4.1%), the rest emit plausibly or decline safely.

## Key caveats (read before trusting)

- **Vision hints are recorded, not live.** `bench_real.py` replays the answers a
  human/agent gave looking at the renders. It is *not* re-deriving them.
- **"Wrong-emit: 0" on real pages was adjudicated manually** against rendered
  figures — there is no machine ground truth for arXiv charts. The gates
  (out-of-axis-range, multivalued, same-frame axis pairing) are what catch the
  bad cases; see `RESULTS.md` for the per-page calls.
- **Coverage is narrow.** Only born-digital vector charts with text tick labels,
  a coherent data curve/bars/markers, and non-crossing series. Raster charts and
  loose-segment producers are out. Scatter is proven on synthetic but not yet on
  a real page (the one real scatter, 0904 p9, declines: element labels sit on the
  markers and defeat legend masking).
