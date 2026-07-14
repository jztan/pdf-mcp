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

## v7 update: tick-label parsing gates (locale + unicode minus)

An LLM-consumer review identified tick-label parsing as the trust contract's
real floor: calibration is the pipeline's one non-geometric input, and a
consistent misparse yields a high-R², mis-scaled axis no downstream gate
catches. Both predicted risks **verified empirically**, then fixed:

| Case | Before | After |
|---|---|---|
| `line_locale_de` (German thousands-period axis, "10.000" = 10000) | **1000× wrong-emit** (`status: ok`, x range 0.00006–20 vs true 0–20000) | correctly **declines** (locale-ambiguity gate: ambiguous tokens dropped → axis lacks ticks) |
| `line_neg_log` (matplotlib `10^-k` ticks, U+2212 minus) | fully declined (all negative exponents unparseable) | **extracts at 0.66%** (U+2212 → `-` normalization) |

Gate design: period/comma + exactly-3-digit-group tokens with non-zero integer
part are genuinely ambiguous (EN decimal vs DE thousands) and are dropped —
ambiguity → decline, never a guess. Leading-zero decimals (`0.395`) cannot be
thousands-groups and are kept (protects the 2605 Fig 11 case). Unambiguous
comma-decimals (`0,5`) normalize. Both archetypes are now permanent synthetic
corpus cases (12 total). Post-v7: synthetic **0.31% mean err / 0-of-11
wrong-emit**, real suite unchanged (0 wrong-emit, littelfuse 1.3%, 2605 0.7%,
1807 1.5/4.1%).

Not in threat model (documented): OCR-style glyph confusion — tick labels come
from the born-digital text layer (exact character codes), never OCR; mojibake
fonts fail parsing → decline.

## Discovery benchmark (`bench_discovery.py`, 2026-07-13)

Tests the `detect_charts` signal from the design (`find_panels ≥ 1` per page)
over the full corpus (640 pages incl. issue-23 samples):

| Metric | Result |
|---|---|
| Recall on adjudicated chart pages | **17/17** |
| False positives on adjudicated non-chart pages (boards/diagrams/heatmaps) | **0/4** |
| Flag rate over corpus | 6.2% (40/640) — selective, won't spam agents |
| Runtime per page | median 10ms, p95 42ms, max 710ms (pathological 300k-seg page → the tool should carry a per-page time guard) |
| Unlabeled flags, sampled visually | 3/3 were genuine charts (incl. 0709.4466 p3, which corrected an earlier by-inference NON-chart label — the signal beat the human label) |

23 flagged pages remain unadjudicated (most in chart-dense papers); the
sampled precision suggests they are overwhelmingly real charts.

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

## Out-of-sample validation (2026-07-14, pre-release gate)

10 fresh arXiv PDFs never used in tuning (1409.1556, 1412.6980, 1502.03167,
1512.03385, 1608.06993, 1706.03762, 1810.04805, 2001.08361, 2005.14165,
2010.11929), 219 pages swept with discovery + extraction; flagged pages and a
sample of unflagged pages adjudicated visually against renders.

**The gate worked: it caught one out-of-sample wrong-emit, which was fixed
before release.** On 2001.08361 (Kaplan scaling laws) p24, Fig 18's right
panel emitted 33 curves with y calibrated against the COLORBAR ticks (linear
4..10 "Test Loss") instead of the panel's own log Tokens axis. Root cause:
the panel's compact 3-tick y-axis (59pt label span) was rejected by the 60pt
minimum-span filter by one point, and the anchored colorbar column won the
right-axis slot by default. Fix (commit 0628e0d): span threshold 60→45pt +
`_looks_like_colorbar` rejection of right-axis candidates backed by a narrow
raster/rect strip; new synthetic archetype #14 `line_colorbar`; two TDD
regression tests (both confirmed failing pre-fix).

Post-fix out-of-sample results:
- 18/219 pages flagged (8.2%, consistent with in-sample 6.7%); every flagged
  page adjudicated is a genuine chart page.
- Adjudicated extractions: Kaplan Fig 2 (p4) — dark-purple 10^3-param curves
  start at loss 10.8 and plateau at 6.4, matching the figure; Kaplan p24 now
  emits 48 series against the correct log axes; ResNet Fig 4 (p5) — ResNet-18
  ends 31.2%, ResNet-34 ends 28.4%, matching the printed curves. No wrong
  emissions post-fix; remaining pages emit plausibly or decline/ask safely.
- GPT-3 (2005.14165) correctly produced ZERO flags: every chart in that PDF
  is a raster image (no vector geometry) — out of scope by design, and the
  discovery signal correctly stays silent rather than false-flagging.
- In-sample side effects of the fix: two real pages now extract MORE
  (0711.3236 both panels; 0811.0781 p31 6 series), discovery flag rate
  41→43/640, recall still 17/17, false positives still 0/4, both benchmark
  suites still 0 wrong-emit.

Verdict: out-of-sample wrong-emit = 0 after the colorbar fix; discovery
generalizes (8.2% flag rate, no false flags observed); the release gate is
satisfied. Residual known limits unchanged (raster charts, crossing
same-style curves, composite mantissa labels).

## Response-contract hardening (consumer validation, 2026-07-14)

Beyond the extraction numbers above (producer-side: 0 wrong-emit), the tool's
response *contract* was hardened over three rounds of external-LLM consumer
testing — uniform series schema (`style` always a dict), axis titles/range with
body-text/caption/cross-panel-title pollution rejected (null on miss),
`y_axis_right` exposed on dual-axis charts, list-return with inline MCP image
blocks on `needs_hint`/`declined`. Extraction accuracy is unchanged (this was
shape/semantics, not values). Guards added so the class can't silently regress:
`tests/test_chart_response_contract.py` (cache-isolated consumer-side invariants
+ a schema↔`CHART_EXTRACTION_VERSION` coupling check).

## Investigated + REJECTED: scalar-labeled-log calibration path (2026-07-14)

A consumer flagged Kaplan et al. Fig 1 (2001.08361 p3) declining and proposed
adding a "fit log10(value) vs pixel against scalar tick labels" calibration
path. Investigated rigorously; NOT implemented — two reasons:

1. **Misdiagnosed root cause.** Fig 1's tick labels are drawn as VECTOR
   OUTLINES, not text (`numeric_tokens` finds 2 on the whole page; the center
   panel's "2.7..4.2" and left "2..7" return False for every value in the text
   layer). There is nothing to calibrate against — no scalar-log path helps.
   The decline is correct (the now-documented outlined-labels limitation). By
   contrast Kaplan Fig 2 (p4) HAS text labels and extracts fine; its scalar
   "10,8,6,4" Test-Loss axis calibrates as linear at r2=0.9999 (its ticks are
   at evenly-spaced pixels — genuinely linear, not compressed).

2. **The general fix would regress the trust contract.** Measured on genuine
   narrow-range LINEAR axes, the log fit is near-perfect too (1807 y: r2_lin=1.0
   vs r2_log=0.9996; 22..12 axis: 1.0 vs 0.992). A "prefer/accept log when it
   fits" rule would silently mis-model linear axes as log and emit subtly-wrong
   interpolated values — violating exact-or-decline. The only SAFE variant
   (log ONLY when linear fails the R² floor AND the value range is wide enough
   to separate the two models) fires on zero confirmed real samples.

Per the sample-driven post-ship policy: no calibration change without a
confirmed real chart that has TEXT scalar labels on a genuinely
pixel-compressed log axis and currently declines. None found. Restraint here
is the 0-wrong-emit discipline in action.

## Base^exponent superscript recovery + log-title contradiction guard (2026-07-14)

A consumer caught a confident wrong-emit in the wild: Hestness et al.
(1712.00409 Fig 1) has an x-axis with power-of-two ticks (2¹⁹…2²⁷) typeset as
`base` + superscript `exponent`. The old `superscript_pow10` handler only
recognised base-10 decades, so the two-glyph labels read as the literal
concatenations "219…227" and calibrated **linear** — a geometrically-exact-
looking but wrong table. This is a *read* error (mis-parsed tick label), not a
scale-choice judgement, so it sits outside the exact-or-decline gray zone: the
right behaviour is to read the label correctly.

Two changes (both general, no per-sample hacks):

1. **`superscript_powers`** (renamed from `superscript_pow10`) recovers
   `base^exponent` for `base ∈ {10, 2}` — the two bases that appear as real log
   axes. Constrained to those two: an earlier any-integer-base generalization
   false-matched incidental super/subscripts on 2605.06546 p20 ('9²', '8⁰',
   '20⁸'), blowing that chart from 0.8% to 204% error. With `{10,2}`, Hestness
   now reads x = log[524288 … 134217728] and 2605 stays at 36 series / 0.8%.

2. **Contradiction guard** (`_title_says_log` + a check in `extract_charts`):
   if an axis *title* declares a log scale ("(log-scale)", "logarithmic") but
   the axis calibrated linear, the chart **declines** instead of emitting the
   mis-scaled table. This is a narrow backstop — it only fires when the log
   title is a short, extractable label; Hestness's own title is a long sentence
   the title extractor rejects, so the base² reader (not the guard) is what
   actually fixes Hestness. `_title_says_log` deliberately does NOT match
   "log likelihood" / "log loss" (logged quantities, not scale declarations).

New synthetic archetype `line_log2` (base-2 log x-axis, ticks 2¹⁹…2²⁷) pins the
recovery: scores 0.10% of y-range, 0/15 wrong-emit. Real corpus stays 0 uncaught
wrong-emit. `CHART_EXTRACTION_VERSION` → 5 (cache rows carry the version).

## Real-world failure catalog — broad out-of-corpus sweep (2026-07-15)

Feature held EXPERIMENTAL (silent-wrong-emit risk). To map the true failure
surface before any fix, swept two fresh out-of-corpus batches and adversarially
verified every emit against a render (not just the 0-wrong-emit gate):

- **Batch A — 11 typography-diverse ML papers** (scaling laws / training curves
  / LR schedules): 55 chart-signal pages → 130 emits, 5 declines.
- **Batch B — ~38 non-ML papers already in `.reading_order_pdfs/`** (physics /
  astro / stats, 2007–2018; gnuplot/matlab/xmgrace toolchains): 26 signal pages
  → 27 emits, 4 declines.

(PDFs auto-fetched into gitignored `.reading_order_pdfs/`; triage script +
renders in session scratchpad, not committed.)

### CATASTROPHIC — log axes with `10^k` exponent tick labels (silent, orders of magnitude)

The dominant real-world failure, same family as the base-2 Hestness bug but far
more common (log axes are ubiquitous in ML papers). The mis-read axis still
calibrates at r²≈1.0, so `status="ok"` and nothing signals the error. Two
sub-mechanisms confirmed on two real papers:

1. **Sign dropped — `10^-6` read as `10^6`.** Henighan 2010.14701 Fig 16:
   x-axis "Compute (PF-days)" is `10^-6 … 10^1`, emitted x runs up to **76,280**.
   Root cause: the minus sign on negative exponents is a **vector stroke, not
   text** (zero minus glyphs in the page text layer), so `superscript_powers`
   silently reads the magnitude with the wrong sign.
2. **Base not paired — exponents read as a linear axis.** SGDR 1608.03983 Fig 1:
   y-axis "Learning rate" is log `10^-4 … 10^0`, emitted as **linear `[-4, 0]`**
   (a negative learning rate). The `10` base isn't attached to its exponent, so
   the exponent labels become literal linear tick values.

**Invisible to range sanity checks:** 0/130 Batch-A emits would be caught by a
"log range dips below 1" heuristic — dropping the sign keeps the range ≥1, and
the linear-misread isn't even flagged as log. Only a visual render comparison
catches it. Positive-decade log axes (2206.07682 emergent-abilities,
`10^18 … 10^24`) DO calibrate correctly.

### MEDIUM — dense multi-panel `pgfplots` figures (panel/axis/title mixing)

2206.07682 Fig 2 (8-panel emergent abilities): x-axis (positive-decade log)
reads correctly, but y-ranges come out `[0,50]` where the panels are 0–70,
emitted y-values don't match the plotted curves, and the bold subplot captions
("(E) TruthfulQA") are captured as x-axis titles. Tight small-multiple grids
still confuse panel↔axis↔title association.

### LOW / fidelity — noisy-trace `multivalued-x` emit

Non-ML batch: noisy experimental traces (0811.0781 force-extension curves,
0904.1520) and wiggly time series get emitted as a single sampled polyline even
though the same x carries multiple y. Values aren't orders-of-magnitude wrong,
but a jagged experimental trace arguably should decline rather than emit a clean
(x,y) table. Cosmetic sub-case: subscript splits in titles ("CO₂" → "CO 2" on
1302.4245 p5) — display-only, values unaffected.

### What the sweep did NOT break (correct emits)

- **Genuinely-negative linear axes** — every negative-linear-y flag was a false
  alarm: 1302.4245 Fig 3 "Covariance" `[-200,200]`, 0711.3236 `b(x)` `[0,-1.5]`,
  "Log Spectral Density" `[-15,10]` all emitted correctly (a logged *quantity*
  on a linear axis is not a log scale — the `_title_says_log` exclusion holds).
- **Positive-decade log axes** and ordinary linear physics/stats plots.

### Takeaway

The curated 0-wrong-emit corpus was **not representative** — it used
well-behaved ASCII positive-decade log axes. Real ML papers break the
tick-label reader constantly via `10^k` exponent typography, and the failure is
silent + confident. Cataloged here; not yet fixed (decision: keep gathering
before committing to a reader fix vs. an aggressive decline guard).

## Catalog growth — econ / finance / bio / stats batch (2026-07-15)

Third batch, for locale/toolchain diversity: 36 recent applied papers pulled via
the arXiv API across econ.GN, q-fin.ST, q-fin.RM, q-bio.PE, q-bio.NC, stat.AP
(R / Stata / seaborn / matplotlib toolchains). 43 chart-signal pages → 174
emits, 13 declines, 51 triage-flagged; flagged emits verified against renders.

**No new CATASTROPHIC class.** These domains are almost entirely linear-axis,
and the linear tick parser is solid: 4-decimal volatility ticks
(2607.09566 efficient frontiers, `0.0005…0.0025`) and large-count density ticks
(2607.08291 `0…70000`) both calibrate correctly. Genuine-negative-x axes
(standardized-return / SDF domains, `[-2,2]`, `[-3,3]`) emit correctly, like the
non-ML negatives.

**Confirmed / reinforced (medium & low):**
- **Title pollution is pervasive in multi-panel econ/finance figures** — bold
  subplot captions are captured as axis titles across dozens of panels:
  `(a) DAX 100`, `(c) Nikkei 225`, `30 days EP`, `2 quantile`, `(d) TWLO.`
  The real axis title (e.g. "Risk") is displaced. Display-only, values
  unaffected, but the `title` field is not trustworthy on grid figures.
- **Dense overlapping-distribution grids over-emit instead of declining** (new,
  medium). 2607.08291 Fig 19 is a 6×4 grid of seaborn KDE+histogram panels with
  three translucent Train/Validation/Test fills each; the tool traces a single
  "curve" per panel. Axis ranges are roughly right, but a stack of overlapping
  translucent densities has no single extractable curve — the series data is
  semantically meaningless and should decline (multivalued/overlapping fills).
- **Mixed curve+scatter panels**: efficient-frontier panels carry a smooth curve
  plus two scatter clouds; the tool emits the curve and its scatter handling is
  inconsistent across the pair.

**Still untested — thousands-separators & comma-decimals.** matplotlib/R (what
arXiv applied papers use) don't emit `1,000` grouping or European `0,5` decimal
commas, so this parser path remains unexercised. It would need Stata/Excel/SPSS
or journal-typeset PDFs, which arXiv doesn't readily provide — an open gap, not
a clean result.

**Running tally across all three batches:** one catastrophic silent class
(log-`10^k`-exponent axes, ML), medium (multi-panel axis/title mixing;
overlapping-distribution over-emit), low (noisy-trace multivalued; title
subscript splits). Linear tick parsing, genuine negatives, and positive-decade
log all correct. Still held experimental; nothing fixed.
