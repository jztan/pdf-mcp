# Header-anchored packed-cell split: excerpt-quality gate results

Feature: split packed table cells (`MIN = "4.5 16"`) into their true header
columns using cell/header x-geometry, plus a `columns_reliable` flag on every
`pdf_read_pages` table entry. Branch `fix/header-anchored-cell-split`.

Harness: `scripts/benchmark_excerpt_quality.py` (85 graded queries, cold cache).
Structured output: `benchmark_data/excerpt_quality_split_results.json`.
Run date: 2026-08-27. **GATE VERDICT: PASS (exit 0), all four clauses green.**

## Table-class containment (the class this feature targets)

| metric | before | after | note |
|---|---|---|---|
| `interpretable_with_context` | 28.6% | **52%** | column identity now resolvable |
| `answerable_from_response` | 73.8% | **83%** | rose (was required only to not fall) |
| `clip_points_wrong` | 1/42 (2%) | **2%** | held; a reliable table drops its clip |
| detection recall (sweep) | 42/42 | **42/42** | split touches cell text, not detection |

Named target queries d04 (reset voltage), d05 (threshold current), d06 (pin-7
leakage) on TI LM555 p5, which failed on exactly this packing bug, all now
containment-PASS on both snippet and paragraph. Frozen row d01 remains frozen
(`clause_4_stale_known_fail` count=0): no row was un-frozen to force a green gate.

## Full containment matrix (all 85 queries)

```
cell                   prose    structured   table    all
snippet                 78%          90%       58%     67%
paragraph               91%         100%       71%     80%
bbox                    87%         100%       73%     80%
qualified                 -            -       67%     67%
interpretable             -            -       21%     21%
interpretable_with_context -           -       52%     52%
answerable_from_response  -            -       83%     83%
clip_points_wrong         -            -        2%      2%

excerpt_containment=0.800   bbox_containment=0.800
clause_1_containment: paragraph=0.861, snippet=0.646, n_live=79
clause_2_regressions: count=0
clause_3_bbox_fidelity: scoped_bbox=68, scoped_excerpt=67, n_bbox_present=81
clause_4_stale_known_fail: count=0
```

## Wrong-split audit (the hard gate)

**0 wrong splits across all 6 pinned datasheets.** On TI LM555, 5 tables split;
every 2-or-more-number row was verified against the published LM555 spec
(Supply Voltage MIN 4.5 / MAX 16; Supply Current TYP 3 / MAX 6; Reset Voltage
MIN 0.4 / TYP 0.5 / MAX 1; Threshold Current TYP 0.1 / MAX 0.25; Pin 7 Leakage
TYP 1 / MAX 100; Control Voltage 9/10/11 and 2.6/3.33/4; Storage temp -65/150).
Single-number cells were left un-split. Vishay, Diodes, and Microchip are
column-clean and untouched.

## Generalization caveat

Two additional vendors were pinned (STMicroelectronics LM158, onsemi LM358),
but both are fully vertically-ruled and contain 0 packed cells. This is a true
negative: it confirms the split does not falsely fire on already-clean tables,
but it does not positively exercise the split on non-TI packed typography.
Positive generalization beyond TI is unproven as of this run. The split's safety
is empirical (corpus-validated, 0 wrong across 6 vendors), guarded by five
fail-closed rules, not proven for all typography.
