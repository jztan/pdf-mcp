# Chart-extraction benchmark — results snapshot (v4/v5, 2026-07-13)

## v5 update: validated on the actual issue-#23 sample PDFs

The reporter supplied his real target PDFs (issue #23 comments). All three are
**born-digital vector** (zero raster chart images) — squarely in scope.

| Sample | Result |
|---|---|
| Littelfuse SP05 datasheet p2, "Typical Diode Capacitance vs. Reverse Voltage" (his primary ask; bot-walled URL, download manually) | **Extracted clean**: single green curve (0V, 50.5pF) → (5V, 25.6pF), matches figure; no junk series |
| arXiv 2605.06546 p20 Fig. 11 (6 small-multiple panels × 6 series) | **All 6 panels extracted**, 6 viridis series each at x=1..12; spot-verified point-for-point against a zoomed render (e.g. dark-blue x=2 → 0.369 vs 0.3688 on chart) |
| arXiv 2605.06546 p20 Fig. 10 (log-log, y-axis has only two composite "N×10^k" labels) | Declines (calibration needs ≥3 parseable ticks) — honest coverage gap |
| arXiv 2203.15556 (Chinchilla; reporter himself called it "imo not possible") | Vector too, but IsoFLOP figures are hundreds of crossing curves → declines, as expected |

v5 fixes driven by these samples (all general, no per-sample hacks; full
regression suite stayed green — synthetic 0.27% / 0 wrong-emit, real 0/14):
1. **Bezier sampling** in `path_pts` — smooth curves drawn with few cubic
   segments starved the cloud (Littelfuse curve had 4 endpoints < 8 minimum).
2. **Tick-strip / grid-lattice filter** — per-*segment* alignment test (pen
   jumps between stubs are diagonal); kills tick rows/columns drawn as one
   path. Trade-off: a perfectly flat data line is also dropped (rare —
   e.g. Fig. 11's r=0 baseline).
3. **Monotonic-run splitting** of label clusters — small-multiple layouts put
   several subplots' ticks in one row/column cluster.
4. **Anchored-axis validation** — a real axis label row/column must have a
   long axis line/frame edge beside it; kills "fake x-rows" assembled from
   side-by-side panels' y-labels.
5. Tighter right-axis band (a true right axis hugs the panel edge; anything
   further is a neighboring subplot's left axis).

Remaining known gaps: composite "N×10^k" mantissa labels (Fig. 10); flat-line
trade-off above; small-multiple pages still over-ask dual-axis hints (safe
direction, noisy).

## v6 update: reporter's samples added to the benchmark + chimera class killed

The issue-#23 samples are now permanent scored cases in `bench_real.py`
(arXiv ones auto-fetch; the Littelfuse datasheet is bot-walled/proprietary —
download manually into `benchmark_data/.chart_samples/`, cases SKIP if absent).

Scored results: littelfuse capacitance curve **1.3%** vs visual GT; 2605.06546
Fig 11 dark-blue series **0.7%** vs zoomed-render GT; Chinchilla p5 emits its
two tractable panels (center Parameters-vs-FLOPs scatter; right panel's fit
line — both value-checked, e.g. 1.5e12 tokens at Gopher's 5.76e23 FLOPs) while
the crossing-curves envelope declines.

v6 fixes (chasing cross-panel "chimera" emissions — right x-axis, wrong
y-axis — which regenerated twice under heuristic shifts):
1. Suffix-magnitude labels (100M / 1.0B / 1T) parse as numbers — without
   this the center Chinchilla panel's y-axis is invisible and pairing falls
   through to a neighbor's axis.
2. Exact-duplicate (not subset) cluster dedup + prefer-more-ticks axis
   pairing — a clean label subset must survive a polluted superset.
3. **Anchor-corner consistency**: each axis records its anchor line (nearest
   to the labels, not longest — page-background rect edges are longer); an
   x/y pair is accepted only if the y-spine meets the x-anchor's end (≤15pt).
   Kills neighbor-axis pairing structurally.
4. Frame-refinement requires mutual-majority overlap (a neighbor's frame
   merely touches the region edge).
5. **Bars must stand on the axis baseline** (one-sided: baseline may sit
   below the lowest labeled tick, never meaningfully above) — kills
   marginal-distribution histograms (1406) for good, keeps real histograms
   whose 0 is unlabeled (0802).
6. Step functions survive decoration filters via stroke connectivity
   (grids/tick strips are disjoint; a staircase is a connected chain).

Post-v6 state: synthetic 0.27% / 0-of-10 wrong-emit; real suite (17 cases incl.
reporter samples) 0 wrong-emit, every emission adjudicated; 0802 caption check
0.3% / 0.1%.

Reproduce with `uv run python benchmark_data/chart_extraction/bench_synthetic.py`
and `bench_real.py`. See `README.md` for caveats (recorded vs live hints;
manual adjudication of real-page wrong-emit).

## Synthetic corpus (10 archetypes, exact ground truth)

| case | status | verdict | err %range |
|---|---|---|---|
| line_color_linear | ok | OK | 0.03 |
| line_mono_grid | ok | OK | 1.59 |
| line_logx | ok | OK | 0.45 |
| line_dual_axis (blue/left) | ok | OK | 0.04 |
| line_dual_axis (red/right) | ok | OK | 0.04 |
| line_two_legend_dashed (green) | ok | OK | 0.03 |
| line_two_legend_dashed (purple, dashed) | ok | OK | 0.12 |
| bar_simple | ok | OK | 0.03 |
| hist_mono | ok | OK | 0.37 |
| scatter_simple | ok | OK | 0.03 |
| line_mono_crossing | declined | correct decline | — |
| decoy_diagram | declined | correct decline | — |

**Emitted-series accuracy: 0.27% of y-range (n=10). WRONG-EMIT: 0 / 10.**

## Real arXiv pages (14 chart-signature pages, agent-in-loop)

| page | result | verdict (manual adjudication) |
|---|---|---|
| 0710.2265 p7 | emit 2 | plausible/questionable (jagged spectrum) |
| 0711.3236 p7 | emit 1 of 2 panels | partial (got b(x), missed s(x)) |
| 0802.0733 p10 | emit 5 | plausible (overlaid histograms) |
| 0802.0733 p12 | emit 3 | **GOOD** — cluster-local means 1312→1315, 4727→4722, ~2755→2670 (0.1–3.1%) vs caption |
| 0811.0781 p29 | declined | safe decline (tiny / loose-segment plot) |
| 0811.0781 p31 | emit 3 | **GOOD** — Fig C samples match 160–200 band; inset panel dropped |
| 0904.1520 p9 | declined | safe decline — element labels sit on markers, defeats legend masking (only real scatter) |
| 0905.3502 p8 | declined | safe decline (gates) |
| 0905.3502 p14 | declined | safe decline (no signature) |
| 0905.3502 p17 | declined | safe decline (no signature) |
| 1406.4582 p4 | declined | **FIXED** — was wrong-emit in v3 (scatter-with-marginal-histograms); out-of-range gate now declines |
| 1501.05624 p8 | declined | correct decline (crossing curves) |
| 1501.05624 p9 | emit 1 | ok (after axis hint) |
| 1807.11632 p4 | emit 2 | **GOOD** — dual-axis via hints: blue(left) 1.5%, red(right) 4.1% |

**WRONG-EMIT: 0 / 14** (manually adjudicated). GOOD (ground-truth verified): 3.
Safe/correct declines: 8. Plausible/partial: 3.

## v3 → v4 (what the hardening changed)

| | v3 | v4 |
|---|---|---|
| Synthetic wrong-emit | 0/10 | 0/10 |
| Real wrong-emit | **1** (1406) | **0** |
| Real safe/correct declines | 4 | 8 |

Three fixes: (1) same-frame axis pairing (corner consistency) — kills cross-figure
wiring; (2) marker-glyph capture (wider size cap + fill/stroke dedup) — scatter on
synthetic; (3) **out-of-axis-range gate** — the fix that actually converted 1406
from wrong-emit to clean decline (marginal-distribution bars map above the axis
max). All residual failures now fail *safe* (decline), never wrong-emit.
