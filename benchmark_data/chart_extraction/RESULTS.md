# Chart-extraction benchmark — results snapshot (v4, 2026-07-13)

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
