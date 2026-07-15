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

## Scoping: is the log-`10^k`-exponent class fixable vs. hard-decline? (2026-07-15)

Root-caused the two confirmed sub-mechanisms down to the primitive level. The
class is NOT one bug — it is a difficulty gradient of renderer-specific quirks:

**Sub-case A — exponent is TEXT, pairing fails (SGDR 1608.03983 "Learning
rate", read as linear `[-4,0]`).** The tick label is fully present as text:
base `10` + exponent `−4` (real U+2212), which `superscript_powers` already
normalizes to ASCII. The sole blocker: the raised exponent is kerned to
*overlap* the base by 0.007pt (`exp.x0 − base.x1 = −0.0069`), and the pairing
gate `0 <= gap < 3` rejects the negative overlap. The orphaned exponents then
calibrate as a spurious linear axis. **Verdict: FIXABLE, EASY, LOW-RISK** —
loosen the gate to `-2 <= gap < 3`. Still requires adjacent+raised+smaller, so
low false-pair risk. Likely covers a large share of matplotlib
LogFormatterMathtext axes. Must re-validate against the good log corpus.

**Sub-case B — minus is a VECTOR primitive, not text (Henighan 2010.14701
"Compute (PF-days)", `10^-6` read as `10^6`).** The base `10` and exponent `6`
are text, but the minus is drawn as a mathtext *hrule* (a thin filled bar,
`x≈174.9–176.4, y≈151.3`) — zero minus characters in the text layer. No
regex/pairing fix can help; the sign lives only in drawing commands.
Recovering it means correlating hrule bars with exponents AND separating true
tick-minuses from decoys (fit-label `e−12`, tick marks, axis frame, other
figures' minuses — 63 short bars on the page, only ~26 near exponents).
**Verdict: reading is FEASIBLE-BUT-FRAGILE (matplotlib-specific, false-positive
risk); prefer DETECT + DECLINE.**

**Decline detector — needs per-panel context.** Naive global signals
false-positive: pooled multi-panel tick rows read as NON-MONO even for the
correct positive-decade case (2206.07682 `10^18..10^24`), and an "unpaired
base" signal catches A but not B (B's bases pair fine, just wrong-signed).
A viable detector must run inside each panel's calibration: (1) a `10`/`2`
base with no paired exponent, or (2) an hrule adjacent to a paired exponent, or
(3) a recovered log axis that is descending/non-monotonic within one panel →
decline. Its false-positive rate against the many CORRECT log axes
(1803.03635 has dozens; 2206.07682 x) is the crux and must be benchmarked.

**Recommendation (staged, if the feature is ever un-held):**
1. Sub-case A read-fix (loosen pairing gate) — small, high-value, validate.
2. Decline detector (per-panel unpaired-base / hrule-near-exponent /
   non-monotonic) as the safety backbone — makes sub-case B SAFE without
   reading it. Medium; false-positive validation is the real work.
3. (optional, later) Sub-case B hrule read to recover instead of decline —
   medium/fragile, defer.
The space of exponent typographies is unbounded and renderer-specific, so the
backbone should be robust DECLINE; correct reads (unicode-minus/overlap) are
enhancements on top, never the primary defense. Still held experimental; the
above is a scoping verdict, not an implementation.

## Implemented: log-exponent read-fix + unreadable-ticks decline guard (2026-07-15, v6)

The staged plan from the scoping section, implemented and validated:

1. **Pairing-gate fix (sub-case A, read).** `superscript_powers` accepts a
   kerned base/exponent overlap down to −2pt (was 0). SGDR's "Learning rate"
   axis now reads `log [1e-4, 1.0]` (was the linear `[-4, 0]` wrong-emit).
   Loosening exposed a second latent bug the original 0-floor had masked:
   x-overlap with NO vertical bound let an x-tick "-3" pair with body text
   88pt below it (2607.08500 p25, bogus `2^-3` ate the tick and broke a
   genuinely-linear [-3,3] axis). Fixed with a vertical-bands-overlap gate —
   a superscript sits BESIDE its base.
2. **Unreadable-ticks decline guard (sub-case B, per-panel).** Two signals on
   each calibrated axis's own ticks: (a) *vector-minus* — an hrule bar inside
   a recovered `10^k` tick's bbox at superscript height (one hit falsifies the
   axis sign); (b) *orphan exponents* — a linear calibration whose ticks sit
   at raised-exponent geometry immediately right of a larger unpaired
   `10`/`2` base. Fires per-panel, so multi-panel pages keep their good
   panels. Henighan Fig 16 (and Fig 8, p12) now decline with
   "tick label sign is drawn, not typed".

**Adversarial review pass (required for trust-contract code) caught two
false-decline defects in the first guard cut, both fixed before the final
validation:** (1) the vector-minus bar test had no LOWER vertical bound, so
dashed curves / minor tick marks in the same x-column far above a positive
`10^k` label falsely declined all-positive axes (Henighan Fig 7 p12,
Chinchilla p23, 7 panels corpus-wide) — the bar must sit INSIDE the tick's
own bbox; (2) the guard fired even on ticks whose minus was TYPED and parsed
(`raw="10^-12"`) — a parsed sign is proof the drawn-minus signal doesn't
apply (1406.6799 p7's typed 10^-12..10^-9 axes now READ correctly instead of
declining). The review also exposed an adjudication error in the interim
notes: Henighan p12_c1 was Fig 7's positive fourth panel (a good emit the
unbounded guard had falsely declined), not a Fig 8 wrong-emit.

**Final validation — full re-sweep of all three corpora (58 papers, ~330
emits), every flip adjudicated against renders:**

| Corpus | pre→post emits | dropped | new |
|---|---|---|---|
| ML (11 papers) | 130→131 | 4 — the Henighan Fig 16 wrong-emits (10^-6 read as 10^6) | 5 — all correct (recovered pgfplots panels: factor-4 log y `[0.05,3.2]`, correct `[0,70]` y-ranges) |
| econ/bio (36) | 174→175 | 0 | 1 — correct (eigenmodes panel) |
| non-ML local (38) | 27→30 | 0 | 3 — all correct (MATLAB panels: typed negative-decade log axes `[1e-12,1e-9]` now read; N∈[2,10] linear) |

Zero good emits lost; guard false-positive rate 0 after the review fixes
(one 1406.6799 panel declines via the orphan-exponent signal — correct: its
y-ticks failed to pair and would have calibrated linear). New synthetic
archetype `line_logneg` (10^-4..10^0 log y, typed minus) pins the read;
real-PDF regressions for SGDR (reads log), Henighan p22 (declines,
vector-minus reason), and 2607.08500 p25 (the vertical-overlap collateral
case) added to the fast suite (skip when corpus absent) and to `bench_real`
CASES. `CHART_EXTRACTION_VERSION` → 6.

Residual honesty: the decline guard covers the two *known* mechanisms.
Exponent typography remains unbounded (outlined glyphs, unusual kerning,
non-matplotlib renderers), so the class is *mitigated*, not closed — the
posture stays exact-or-decline with the sweep loop as the detector of the
next variant.

## Out-of-sample test of v6 — fresh astro/cond-mat/hep/optics/ML batch (2026-07-15)

24 new papers from six previously-unsampled arXiv categories (astro-ph.GA/HE,
cond-mat.stat-mech, hep-ph, physics.optics, cs.LG — log-axis-heavy by design,
stressing exactly the class v6 fixed): 131 emits, 30 panel declines, flagged
emits + all negative-decade log reads verified against renders.

**The v6 read-fix works in the wild.** 8 negative-decade log emits on
cond-mat 2607.02157 (x `log [0.1, 100]`, y `log [0.001, 1]`) verified
point-for-point (a1: J=0.1 → 1.315 vs ~1.32 on the figure; b1 0.027 vs ~0.03;
c1 0.315 vs ~0.31). Under v5 these would have been silent linear/sign-dropped
wrong-emits. Astro log-log axes up to `10^43..10^48` erg read correctly.

**The v6 decline guard works in the wild.** hep-ph 2607.08082 draws its
exponent minuses as rules (Henighan-style) across the whole paper — all 11
affected panels decline with the vector-minus reason; verified the y-axes
fire on their own tick bboxes (not incidental geometry).

**One guard refinement from this batch.** The bar test's -0.5pt top slack let
an error-bar cap grazing a tick's bbox top (0.2pt ABOVE it) false-decline one
of nine panels on astro 2607.06360 p20. Tightened to strictly-inside
(`bb.top + 0.2 < bar.y`); a real drawn minus is centered on the exponent
(~2pt inside — Henighan +2.2), so all true positives hold. Post-fix: the
panel emits, Henighan/2607.08082 still decline, suites + benchmarks 0
wrong-emit.

**Confirmed known classes, no new ones:** title pollution (subplot captions,
split superscripts "E 2 dN/dE"), multiplier-notation axis labels ("[10^-4]" —
values match the printed ticks; the unit multiplier lives in the title, which
the caller must read), step-histogram multivalued flags (hep-ph event walls,
2607.08175 p23). **Wrong-emits found: 0.**

## Adjudicated: the "empty panel" bucket (2026-07-15)

Panel census across all four corpora (109 papers): 595 detected panels →
468 emitted (78.7%), 61 declined-with-reason (10.3%), 66 "empty" (11.1%,
attempted but zero series). Every empty panel was collected with diagnostics
and a region render; representative cases adjudicated visually.

**Headline: the empty bucket is not a silent shrug.** 64/66 are
`chart_type: "unknown"` — the tool returns `needs_hint` with a closed-enum
`chart_type` question and an annotated render, not an empty OK. Only 2/66 are
true silent failures (classified `bar`, zero series emitted).

**Dominant root cause (~2/3 of the bucket): SPARSE charts — 3–7 points per
series.** The classify gates require ≥8 points spanning ≥25% width for a line
cloud and ≥5 markers per style group for a scatter. Real charts below these
thresholds fall through to `unknown`: EfficientNet's 6-point frontier
(1905.11946), chain-of-thought's 5-point emergence lines (2206.07682), 3-point
error-bar scatters (0811.0781 p29), 8-series-one-point-each comparison
scatters (2206.07682 p24). One point per model size is the canonical
scaling-law figure — this class is common and *valuable*.

**Hint recovery was broken for BOTH sparse lines and sparse scatters**
(correction to this section's first draft: the "recovered" EfficientNet
panel turned out to be a pre-existing emit of a different panel on the same
page — `extract_line` re-applies its own ≥8-point gate, and `extract_scatter`
its ≥5-unique-points gate, so the chart_type answer changed nothing). The
recall lever: relax the per-extractor gates when the type is explicitly
hinted, and let classify treat few-point marker-connected polylines as line
candidates directly. Implemented as v7 — see the next section.

**Second cluster (~1/4): no extractable vector data in the panel.** Vector
axes framing rasterized plot content (physics.optics 2607.03442's field-map
panels), seaborn KDE grids whose translucent fills survive no gate
(2607.08291 p78), and one clean vector curve that `collect` finds no cloud
for (2607.03442 p21 — unexplained, worth a probe). These are correct
non-emits; they'd serve agents better as reasoned declines
("no vector plot geometry found — data may be rasterized").

**True silent failures (2): open-marker scatters misclassified as bars.**
Large open squares/triangles register as bar_rects (73 on astro 2607.06338
p7), bar extraction then finds no shared baseline and emits nothing. Should
fall back to unknown/question when extract_bar yields zero series.

Follow-ups (open, not implemented): sparse-chart recovery (relax post-hint
scatter gate + few-point line classify), reasoned decline for
no-vector-geometry panels, bar→unknown fallback on zero series.

## Implemented: sparse-chart recovery + reasoned empty-panel outcomes (2026-07-15, v7)

The follow-ups from the empty-bucket adjudication, implemented and validated:

1. **Sparse marker-connected lines extract.** A 3–7 vertex polyline whose
   vertices coincide with plotted markers (>=3 hits, >=50% of vertices) is a
   data line — one point per model size is the canonical scaling-law figure.
   `classify` recognises it and `extract_line` accepts it (span gate stays:
   brackets/arrows are short-span). Honesty notes accompany the sparse path:
   `sparse line capture (N vertices)` on short captures, and a
   `line cloud(s) below extraction gates not emitted` note when data-like
   clouds are left behind — the table never silently pretends completeness.
2. **Explicitly hinted types get relaxed gates.** Hinted "line" bypasses the
   dense-cloud minimum (>=3 vertices + span still required); hinted "scatter"
   lowers the marker minimum 5→2. A hinted type that still extracts nothing
   DECLINES with the reason instead of returning a typed-but-empty chart.
3. **Reasoned declines for unanswerable panels.** A panel whose interior has
   essentially no vector geometry (<=2 vertices — rasterized data, phantom
   axis pairings) declines with "no extractable vector plot geometry" instead
   of asking a chart_type question no answer can satisfy. Classified-bar
   panels with zero baseline series fall back to the chart_type question
   (open markers misread as bar rects).

Corrections from implementation (honesty): the earlier adjudication's
"EfficientNet sparse frontier" empties were actually PHANTOM panels around a
figure whose real panel already emitted; and the 0811.0781 3-point-scatter
markers never reach geometry collection at all (unsupported marker shapes),
so it now declines with a reason rather than emitting — its printed value
labels remain readable as text.

**Validation (58 papers, full re-sweep + census):**
- Empty panels 66 → 34 (11.1% → 5.7% of detected panels); emitted 78.7% →
  80.7%; reasoned declines absorb the rest (10.3% → 13.6%).
- 13 new emits, every one adjudicated against its render, all correct:
  emergent-abilities MMLU/instruction-following sparse pgfplots panels
  (x 10^20..10^24, y 0..100 ✓), ViT Figure 4 (6 series × 4 markers,
  x "10M..300M" ✓), a Pareto-frontier dashed line (finance), one astro
  luminosity panel. 0 dropped emits. **Wrong-emits: 0.**
- Suites: 1031 passed; synthetic 0/17 wrong-emit (new `line_sparse`
  archetype: 5 points exact + sparse-capture note); real bench 0 wrong-emit.
- `CHART_EXTRACTION_VERSION` → 7.

Residual: series whose markers geometry collection can't capture (oversized/
compound shapes) and 1-point-per-style panels stay declined/questioned —
documented, sample-driven follow-ups.

**Adversarial review of v7 caught two wrong-emit defects in the hinted
relaxations — both fixed before finalizing.** The principle both violated:
*a type hint confirms the CHART, never per-series evidence.* (1) hinted
"line" waived marker-connection for every cloud in the panel, so a
significance bracket (4 vertices, wide span) emitted as a curve — marker-
vertex coincidence is now mandatory on the sparse path even under a hint;
(2) hinted "scatter" at min-2 points emitted a pair of same-color annotation
arrowheads as a data series — the hinted floor is now 3. Also fixed: an
empty hinted-line extraction declined with the misleading "all multivalued"
message (now the honest hinted-type reason), and answering `not_a_chart`
looped forever re-asking the question (now a terminal decline). The
reviewer's probe PDFs are promoted to permanent fixtures
(`line_bracket_decoy`, `scatter_arrow_decoy` — pytest hint-flow tests, kept
out of the oracle benchmark which never answers the dangerous way). All 13
adjudicated v7 recoveries survive the tightening (they are all genuinely
marker-connected); suites 1034 passed, synthetic 0/17, real 0 wrong-emit.

## Implemented: legend-signature masking — the composite-figure fix (2026-07-15, v8)

Probing the composite-figure empty class disproved its "needs dash-stitching"
label: dashed/dotted lines never shatter (matplotlib writes ONE path with a
dash-pattern attribute — vertices intact). The real cause: `legend_masks`
masked EVERY in-panel word, and on text-dense panels (EfficientNet Fig 1:
~15 point annotations + an inset table = 72 masks covering 135% of the panel)
the per-vertex masking ATE the data curves.

**The fix — masks now require a legend signature:**
- a short thin stroke sample immediately left of a text row (line legends), or
- a marker glyph at a CONSISTENT label offset on >=2 stacked left-aligned rows
  (scatter legends; stacked point annotations like Fig 3's "r=1.3/1.5/1.7"
  have varying offsets and no longer mask), or
- a compact frame box enclosing >=2 label rows (framed legends whose samples
  defeat the strip geometry — 2607.09566 p30).
Legend FRAME BORDERS are masked as thin bands (the frame stroke otherwise
enters clouds as a fabricated grey "curve" at legend position — caught by
adjudication); interiors are NOT masked (interior masking ate curves passing
near legends).

**Adjudication catches during this wave (all fixed before commit):**
1. legend frame border emitted as a 12-pt grey near-vertical "curve"
   (2607.09566 p27) → border-band masking;
2. full-interior frame masking ate 4 real curves (1905.11946 p8) → bands only;
3. stacked point annotations mimicking a marker legend re-ate EfficientNet
   Fig 3's r-panel → consistent-offset rule;
4. a NEW panel unlocked by the fix exposed a pre-existing tick-reader gap:
   an A&A SED plot's 10^-11..10^5 y-axis has a DRAWN minus occupying a
   4.6pt base-exponent gap — beyond both the 3pt pairing gate and the
   orphan-guard's 4pt window — emitting linear [1,11]. Orphan window widened
   to 8pt; the panel now declines with the honest unreadable-ticks reason.

**Adversarial review of the first v8 cut found TWO wrong-emit regressions on
vanilla matplotlib legends** — a single-entry framed legend emitted its own
frame as the only "curve", and unframed/ncol marker legends injected
fabricated points into real scatter series — plus a border-banding rule that
could eat a data curve's own apex. All fixed: frame boxes must be STROKED
(shaded fill-only regions are not legends), frames enclosing >=1 row count,
border bands apply only to perimeter-hugging paths, ncol same-baseline rows
qualify as legend neighbors, and a lone marker-row masks when its marker
style recurs as panel data (>=5 glyphs — fabrication prevention beats the
annotation-row recall it costs). The reviewer's 11 attack PDFs are permanent
fixtures (`legend_attacks/` + tests/test_chart_legend_attacks.py, 8 tests).

**Final numbers (595 panels, 109 papers):** emitted 80.7% → **80.8%**,
declined 13.6% → 14.3%, empty 5.7% → **4.9%**. The headline is not the
percentage — it is what moved: EfficientNet Fig 1 and other text-dense
composite figures now emit (values match Fig 1's own inset table), FIVE
latent wrong-emits were caught and now decline (the A&A SED drawn-minus
family: linear [1,11] / [11,15] for 10^k log flux axes — orphan-exponent
window widened 4→8pt, zero false positives corpus-wide), and the masking
layer is now fabrication-proof against the attack set. Some annotation-heavy
panels re-masked by the fabrication rules returned to the question path —
recall traded for the trust contract. Suites 1042 passed; synthetic 0/17;
`CHART_EXTRACTION_VERSION` → 8. Recovered: EfficientNet Fig 1
(values match its own inset table: B1 = 7.8M/79.1% vs emitted 7.803/79.25)
and Fig 3, ViT Fig 4 region panels, efficient-frontier scatter series,
astro/optics panels. 4 dropped emits are all safe-direction declines
(multivalued / out-of-range). The 1807 dual-axis benchmark error IMPROVED
2.2% → 0.8% (previously-masked curve vertices restored). Suites 1034 passed;
synthetic 0/17; `CHART_EXTRACTION_VERSION` → 8.

## Implemented: drawn-minus READING — stage 3 of the log-exponent plan (2026-07-15, v9)

The deferred stage-3: instead of declining, `_power_pairs` now READS a
vector-drawn exponent minus — a thin bar in the base→exponent gap at
superscript height (strictly inside the exponent band, +0.2pt top margin so
grazing error-bar caps never negate a positive tick) negates the exponent.
Wide gaps (3–9pt, the A&A family where the drawn minus occupies the space)
pair ONLY when the bar is present; typed-minus exponents never double-negate.
The detect-and-decline guards remain as backstops for unpaired geometries.

Validation: Henighan Fig 16 — the ORIGINAL catastrophic wrong-emit — now
reads x = log [1e-06, 10] with values matching the figure (first datum
1.79e-6 PF-days / 1.51e4 params); A&A flux axes read (10^-11..10^5,
10^-15..10^-11). 27 decline→emit conversions across the corpus, ZERO drops,
all adjudicated (incl. a genuine 21-decade axis, 10^-16..10^5 every 3
decades). 2607.08082 stays declined (partial negation — safe direction).
Negative controls unchanged: positive decades, base-2, typed minus,
grazing-cap page. Regression tests updated (Henighan now must READ; new
grazing-cap must-not-negate test).

Adjudicating the conversions caught a second latent bug: `tick_series`'s
dv-uniformity floor was ABSOLUTE (1e-9), so micro-magnitude tick sets
(fluxes 1e-15..1e-11) trivially passed as "uniform" and calibrated LINEAR on
log axes — interpolated values silently wrong. Floor is now scale-aware
(1e-9 x |v|max); tiny-decimal genuine linear axes (0.0005..0.0025) unaffected.

Suites 1043 passed; synthetic 0/17; `CHART_EXTRACTION_VERSION` → 9.

## Consumer round 2 fixes: dash-aware styles + log-aware range gate (2026-07-15, v10)

The second external-consumer round (testing v9) verified all three historical
repro cases fixed against renders, then found two defects on Henighan Fig 16:

1. **Same-color data+fit merged into one interleaved series** — the solid
   data curve and its dashed power-law fit share a color, and the style key
   (color, fill, width) ignored dash. The merged "curve" was a sawtooth
   tracing neither real curve, flagged multivalued:false. Fix: dash pattern
   (normalized, float-noise-rounded) is part of the style key and surfaced
   as `style.dash` (null = solid) so callers can tell data from fit.
2. **False decline of a clean panel** — the merged series inherited the
   fit's run past the last tick, and `in_range_series` computes margins in
   LINEAR units, microscopic at the top of a log axis. Fix: decade-based
   margins on log axes. Text-to-Image now emits data+fit separately.

Post-fix: Fig 16 emits monotone data/fit pairs (sawtooth gone), the other
chimera panel declines safely, SGDR/marginal-histogram/colorbar negative
controls unchanged, suites 1044 passed, benches 0 wrong-emit.
`CHART_EXTRACTION_VERSION` → 10 (style dict gains `dash`).

## Implemented: base-level drawn-minus sign gate (2026-07-15, v12)

Consumer round 4 closed the SuperMongo branch (fully-drawn Hershey labels
decline safely — nothing reaches the sign logic) but left a claim untested:
"digits-as-text + drawn minus only exists at superscript level." It doesn't.
A synthetic probe (matplotlib chart, minus glyphs redacted and redrawn as
filled rules — the Origin/journal typography) produced a full sign-flip:
x [-24,-18] emitted as [18,24], y [-5,-1] as [1,5], both r2=1.0, reachable
through an HONESTLY-answered chart-type hint (the minus rules themselves
triggered the question as phantom bar-rects). `numeric_tokens` reads |value|
and NO guard inspected plain-number ticks.

Hunting the corpus for the same typography found a LIVE wild wrong-emit:
2607.03442 p31 (phase noise, y −70..−20 dBc/Hz, digits text / minus drawn)
emitted both curves against y [20, 70]. Pages 21/23/30 of the same paper
carry the identical defect masked by other declines (their declined chart
metadata still advertised sign-flipped axis ranges).

Fix: third `_ticks_unreadable` signal — on an axis where NO tick carries a
typed minus, a plain-number tick with a thin FILLED bar hugging its left
edge (right edge within −2.2..+0.6pt of the digit, bar at digit mid-height)
declines the axis as sign-unreadable. Two scoping lessons baked in:

1. **Width is base-level, not superscript-level**: `_hrule_bars`' 4.5pt cap
   (tuned on exponent-sized rules) misses full-size minus rules (~0.6-0.8em:
   6.5pt in the probe, 2.8pt at 2607.03442's font) — the gate sweeps its own
   `max_w=9.0`.
2. **Fill vs stroke is the tick-mark discriminator**: 1807.11632 p4's
   right-axis tick marks end 1.6pt from their labels — inside any workable
   x-gap window — but ticks/dashes/error-caps are STROKED paths while every
   observed minus rule is a FILLED rect (`fill_only=True`).

Not attempted: READING the sign (negating the token). The v9 read-unlock had
corpus-wide 0-FP evidence for its bar signal first; base-level bars have one
wild sample so far. Decline is the trust-contract move until the sample base
grows.

Validation: synthetic corpus +2 (`line_neg_linear` typed-minus control must
EMIT with negative ranges; `line_drawn_minus` doctored sibling must DECLINE)
= 0/19 wrong-emit; bench_real byte-identical pre/post (zero collateral);
2607.03442 p31/p30/p21 decline with the sign reason (new regression test);
1807.11632 dual-axis and 2607.06360 grazing-cap negative controls unchanged;
chart suites 86 passed. `CHART_EXTRACTION_VERSION` → 12 (declined-chart
metadata changes for affected pages; cached rows must invalidate).

## Implemented: drawn-glyph decline-reason discrimination (2026-07-15, v13)

The round-4/5 consumer note, twice flagged: the generic decline
`"no chart signature (no valid tick-series axes)"` conflates "not a chart"
with "a chart whose tick labels never reach the text layer" (SuperMongo/
PGPLOT Hershey strokes — Blanton astro-ph/0210215 p33; outlined-text
exports). The latter is a typography ceiling the consumer should eyeball
around, not a tool failure to debug.

`_no_panel_reason` now discriminates on the drawn-glyph fingerprint: >=2
long horizontal + >=2 long vertical axis-anchor segments whose frame region
(+25pt label margin) holds fewer than 3 numeric text tokens → the decline
names the situation ("axis-like frame geometry but no readable tick-label
text — either not a data chart, or the labels are drawn/outlined glyphs…").
Data tables keep the generic reason (their rules enclose their numbers);
prose/header-rule pages lack the perpendicular pair. Wording deliberately
claims only what is measured — line drawings and schematics share the
fingerprint, so it offers both readings.

Validation: Blanton p33 gets the specific reason; 40-PDF wild sweep: 41 of
532 no-panel declines flip, adjudicated = unlabeled/drawn-label plots
(correct) + boxy schematics (covered by the either/or wording), zero
misleading; syn corpus decline reasons unchanged (decoy_diagram and
locale_de keep the generic reason); chart suites 88 passed.
`CHART_EXTRACTION_VERSION` → 13 (decline-reason strings change for cached
no-panel pages).
