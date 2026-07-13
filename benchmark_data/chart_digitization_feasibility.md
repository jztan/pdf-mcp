# Chart Digitization Feasibility Spike + Benchmark (issue #23)

Date: 2026-07-13. Corpus: 44 arXiv papers in `benchmark_data/.reading_order_pdfs/`
(830 pages). A runnable prototype + benchmark now lives in
[`chart_extraction/`](chart_extraction/) — see the **v4 update** at the bottom;
the sections below record the original v2 spike that motivated it.

## Question

Can pdf-mcp automatically extract `(x, y)` data tables from charts in real PDFs
("auto WebPlotDigitizer", issue #23), reliably enough that emitting numbers is
safer than an agent eyeballing a rendered image?

## Approach

Vector-geometry extraction, not pixel CV: born-digital PDF charts carry their
polyline vertex coordinates in the drawing commands, and tick labels in the text
layer. Calibrate pixel→data from tick-label positions; isolate data curves from
axes/grids/glyphs structurally; map vertices to data space. This is a capability
pixel tools (WebPlotDigitizer, engauge) cannot have — they see rasters, we see
coordinates.

## Results

### Synthetic ceiling (known ground truth)
| Case | Mean error |
|---|---|
| Clean matplotlib line chart | **0.035% of y-range** (essentially exact) |
| Monochrome + black gridlines (structural isolation) | **0.11%** |

### Corpus reality (44 papers, 830 pages)
| Filter stage | Pages |
|---|---|
| Vector-heavy (≥150 segments) | 104 |
| + naive preconditions (labels + coherent polyline) | 76 ("73%") |
| + strict chart signature (monotonic evenly-spaced tick series, x AND y) | **14** |
| Visually confirmed real (x,y) charts among 6 sampled candidates | 4 |

Key correction: most "vector-heavy pages with numbers" are NOT charts — they are
geometric diagrams, game boards, topology illustrations, heatmap panels. Naive
precondition counting overestimated the addressable set by ~5x.

### Ground-truth benchmark on the 4 confirmed real charts
| Page | Type | Outcome |
|---|---|---|
| 1807.11632 p4 | 2-series line, log-x, dual-y, legend | **Extracted: mean err 2.1% (blue), 2.8% (red) of y-range** — at visual-GT noise floor. Dual-axis pairing NOT automatic (risk flag fired; red needs manual axis choice) |
| 1501.05624 p8 | ~10 crossing mono polylines | Correctly declined (multivalued gate) — but ALSO emitted the plot frame as a fake "curve" |
| 0710.2265 p7 | Jagged mono multi-curve, dashed | Correctly declined (multivalued gate) |
| 0802.0733 p12 | Histogram | **DANGER: emitted confidently-wrong numbers** (claims y=-0.007 at the x of a true 0.11-high peak) — polyline model applied to bars passes the gates |

### Failure modes catalogued (all hit on real pages)
1. Superscript log labels: "10⁰" flattens to token "100" → poisoned calibration
   (fixed via rawdict font-size span pairing — must-have, log axes are ubiquitous)
2. Vector-glyph text (outlined fonts) mimics polylines → needs bbox-span filter
3. Legend line-samples contaminate curve clouds → needs legend masking
4. Dual y-axes: geometric extraction exact, but curve→axis assignment is
   genuinely ambiguous without reading the legend/axis-label semantics
5. Chart-type mismatch (histograms, scatter/marker plots) → plausible junk
6. Plot frame leaks through as a fake curve without explicit frame detection
7. Loose-segment producers (~11% of vector pages): curve emitted as thousands of
   ungrouped 2-pt segments; dashed lines are inherently this
8. Raster-image charts: 0% addressable by this approach

## Verdict

- **The unique-to-pdf-mcp claim is REAL**: on the one clean real-paper line chart,
  vector-geometry extraction matched visual ground truth to ~2% of range
  (synthetics: 0.03–0.1%), better than any pixel digitizer and with zero
  hallucination risk on the geometry itself.
- **The addressable set is SMALL**: true single-panel line charts with text tick
  labels and non-crossing curves. In this 44-paper corpus: 14 chart-signature
  pages / 830 total; of 4 benchmarked real charts, 1 extracted cleanly.
- **The risk is asymmetric and confirmed**: two of four real charts produced
  *confidently wrong* outputs from model mismatch (histogram, frame). Shipping
  without a chart-type classifier + frame filter + strict decline behavior would
  emit false tables — worse than no feature.
- Realistic engineering scope for a shippable v1: chart-type gate (line vs bar vs
  scatter vs other → decline all but line), frame removal, the superscript/glyph/
  legend fixes above, confidence report (calibration R², isolation margin,
  multivalued check, dual-axis flag), and decline-with-rendered-image fallback.

## Recommendation

Feasible as a narrowly-scoped, aggressively-gated tool ("extract data from simple
born-digital line charts; decline everything else honestly"). NOT feasible as the
general "read any chart" feature issue #23 asks for. The decline path (fall back
to `pdf_render_pages` + multimodal reading) is mandatory, not optional. Decision
on building v1 should weigh the small addressable set against the near-exact
accuracy when in-scope.

## v4 update (2026-07-13) — "Approach A" prototype built + benchmarked

Rethought via the brainstorming skill. **Approach A** = exact vector extraction +
a chart-type classifier (line / bar / scatter) + an *agent-answered hint protocol*
(the calling vision model resolves chart-type / dual-axis ambiguity via closed
enum questions; hints carry semantics only, never values) + hard gates. Built and
benchmarked; runnable artifact in [`chart_extraction/`](chart_extraction/)
(`bench_synthetic.py`, `bench_real.py`, `RESULTS.md`).

Results (v4): synthetic **0.27% mean error, 0/10 wrong-emit**; real 14 pages
**0/14 wrong-emit** (was 1/14 pre-hardening), 3 ground-truth-verified GOOD (incl.
dual-axis 1807 p4 blue 1.5% / red 4.1% via the hint round-trip), rest decline
safely. Three gates did the work: same-frame axis pairing, marker capture, and an
**out-of-axis-range gate** (the one that fixed the 1406 marginal-histogram
wrong-emit). Classifier eliminated the v2 histogram-as-line disaster.

This **validates the architecture to the trust bar** (agents can trust any emitted
table; everything else declines). Remaining gap is *coverage*, not correctness:
scatter proven on synthetic but not a real page (the one real scatter, 0904 p9,
declines — element labels sit on markers, defeating legend masking); raster and
loose-segment producers still out. Next step if pursued: promote the prototype to
a real `pdf_extract_chart` tool + write the design doc.
