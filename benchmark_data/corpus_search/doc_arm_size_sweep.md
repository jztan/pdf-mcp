# Document arm: corpus-size sweep (2026-08-29)

Measured on branch `feat/doc-profile-routing` at 6c9700e, deterministic, no
LLM judge. One warmed isolated cache (100 gold arXiv docs from
`manifest.json` plus the 400 distractors from `distractor_manifest.json`,
text, page embeddings, and doc profiles). Nested corpora: the 100 gold docs
plus the first N of a fixed shuffle (seed 20260829) of the distractor
manifest, N in 0, 50, 100, 200, 300, 400, giving sizes 100 to 500. All 89
graded queries, two rows per size: `control_no_arm` (keyword + semantic
page arms only, the pre-arm hybrid) and `shipped_arm_w0.25` (adds the
document arm at `CORPUS_DOC_ARM_WEIGHT`). Arms are rebuilt from the server
internals (`_corpus_keyword_rankings` with the term-coverage tie-break,
`_corpus_semantic_scores`, `_corpus_doc_scores`, fused by
`corpus.rrf_fuse_rankings_scored`), graded by the modes harness's own
functions, top_k 10, ties by (doc_path, page). Raw numbers in
`doc_arm_size_sweep.json`.

Why this exists: the 100-doc and 500-doc gates alone could not show where
the arm starts paying or whether the one-query spread loss at 100 recurs.

## Reading

- Without the arm retrieval decays with corpus size (described doc-hit@3
  0.64 to 0.48, spread 0.92 to 0.64). With the arm, described holds at
  0.64 to 0.72 across the range and spread stops decaying past 300.
- The spread loss exists only at 100 (spread-03, spread-08). From 150 up
  the control already misses both, so the arm never costs a spread query
  there and starts paying at 350 (+1) and 400 (+1), +2 at 500.
- Needle never moves. Trap is identical in both rows at every size (the
  0.985 at 500 is a corpus effect, present with and without the arm).
- Described gain by size: +2, +2, 0, +2, +4, +4, +5 queries at 100, 150,
  200, 300, 350, 400, 500. Not perfectly monotone (0 at 200): n=25, one
  query is 4 points; read the trend, not single cells.

## Fidelity check

Metadata verified non-None for all 500 docs before the sweep ran.
Size-100 and size-500 rows reproduce the committed same-day numbers
exactly:

- 100: control described 0.64/0.730, spread 0.92/0.722; shipped
  0.72/0.755, spread 0.88/0.690.
- 500: control described 0.48, shipped 0.68, spread 0.64 -> 0.72, needle
  doc-NDCG 1.000, trap doc-NDCG 0.985.

Both match. Full numbers in `sweep.json`.

## described (n=25)

| size | control h@3 | shipped h@3 | delta (queries) | control dNDCG | shipped dNDCG |
|---|---|---|---|---|---|
| 100 | 0.64 | 0.72 | +2 | 0.730 | 0.755 |
| 150 | 0.64 | 0.72 | +2 | 0.678 | 0.711 |
| 200 | 0.64 | 0.64 | 0 | 0.663 | 0.644 |
| 300 | 0.56 | 0.64 | +2 | 0.635 | 0.607 |
| 350 | 0.52 | 0.68 | +4 | 0.618 | 0.615 |
| 400 | 0.52 | 0.68 | +4 | 0.615 | 0.607 |
| 500 | 0.48 | 0.68 | +5 | 0.610 | 0.587 |

## spread (n=25)

| size | control h@3 | shipped h@3 | delta (queries) | control dNDCG | shipped dNDCG |
|---|---|---|---|---|---|
| 100 | 0.92 | 0.88 | -1 | 0.722 | 0.690 |
| 150 | 0.84 | 0.84 | 0 | 0.658 | 0.667 |
| 200 | 0.80 | 0.80 | 0 | 0.582 | 0.642 |
| 300 | 0.72 | 0.72 | 0 | 0.556 | 0.594 |
| 350 | 0.72 | 0.76 | +1 | 0.546 | 0.587 |
| 400 | 0.68 | 0.72 | +1 | 0.521 | 0.572 |
| 500 | 0.64 | 0.72 | +2 | 0.508 | 0.543 |

## needle (n=14)

| size | control h@3 | shipped h@3 | delta (queries) | control dNDCG | shipped dNDCG |
|---|---|---|---|---|---|
| 100 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 150 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 200 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 300 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 350 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 400 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 500 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |

## trap (n=25)

| size | control h@3 | shipped h@3 | delta (queries) | control dNDCG | shipped dNDCG |
|---|---|---|---|---|---|
| 100 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 150 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 200 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 300 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 350 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 400 | 1.00 | 1.00 | 0 | 1.000 | 1.000 |
| 500 | 1.00 | 1.00 | 0 | 0.985 | 0.985 |

## Flips vs control, by size (hit@3 change only)

| size | described gained | described lost | spread gained | spread lost |
|---|---|---|---|---|
| 100 | described-07, described-21 | (none) | spread-06 | spread-03, spread-08 |
| 150 | described-07, described-21 | (none) | (none) | (none) |
| 200 | described-07, described-21 | described-02, described-12 | (none) | (none) |
| 300 | described-07, described-14, described-21, described-23 | described-02, described-12 | (none) | (none) |
| 350 | described-07, described-09, described-14, described-21, described-23 | described-12 | spread-07 | (none) |
| 400 | described-07, described-09, described-14, described-21, described-23 | described-12 | spread-11 | (none) |
| 500 | described-07, described-09, described-14, described-20, described-21, described-23 | described-12 | spread-11, spread-18 | (none) |

needle and trap: no query flips hit@3 at any size (both classes hold
1.00 h@3 identically for control and shipped at every size).

## What spread-03 and spread-08 actually do across the sweep

The 100-doc spike attributed the two spread losses to the arm's
re-weighting. The per-query trace shows a size effect underneath that:

| size | spread-03 control h@3 | spread-03 shipped h@3 | spread-08 control h@3 | spread-08 shipped h@3 |
|---|---|---|---|---|
| 100 | 1 | 0 | 1 | 0 |
| 150 | 0 | 0 | 0 | 0 |
| 200 | 0 | 0 | 0 | 0 |
| 300 | 0 | 0 | 0 | 0 |
| 350 | 0 | 0 | 0 | 0 |
| 400 | 0 | 0 | 0 | 0 |
| 500 | 0 | 0 | 0 | 0 |

At size 100 the arm is what costs these two queries: control still finds
the gold document, shipped does not. From size 150 on, control itself no
longer finds either gold document (distractors alone bury it before the
arm is even applied), so both rows already read 0 and the arm has nothing
left to lose on these two queries. This loss is a size-100 artifact, not
a persistent two-query tax.

## Reading

**Described gain first exceeds one query at size 100**, the smallest
corpus tested: 2 net gains (described-07, described-21) with zero losses.
The gain is present at every size in the sweep, ranging +2 (100, 150,
300) to +5 (500), and never negative.

**The spread loss is not the same two queries at every size.** spread-03
and spread-08 are lost to the arm only at size 100; from 150 onward
control already misses both regardless of the arm (buried by
distractors), so there is no incremental arm-caused loss on them past
100. From 400 onward the arm nets a spread gain instead (spread-11 at
400, plus spread-18 at 500), so the net spread effect flips sign across
the sweep: -1 query at 100, 0 at 150-300, +1 at 400, +2 at 500.

**Needle never moves.** Doc-hit@3 and doc-NDCG are 1.00/1.000 for both
rows at every size; the arm has zero measurable effect on this class
anywhere in the sweep.

**Trap moves only with corpus size, never with the arm.** hit@3 stays
1.00 throughout. dNDCG stays 1.000 for both rows through size 400 and
drops to 0.985 for both rows at size 500, identically, meaning the drop
comes from distractor dilution at 500 docs, not from the doc-profile arm
(control and shipped are equal at every size).

**Neither curve is cleanly monotone.** Control described h@3 is
non-increasing (0.64, 0.64, 0.64, 0.56, 0.52, 0.48) but shipped described
h@3 is not: 0.72, 0.72, 0.64, 0.72, 0.68, 0.68, so from a pure hit@3 point
of view it dips into a rest a hit-count away from its own value at 200. Once
converted to a query count out of 25 this is 18, 18, 16, 16, 17, 17. Spread
h@3 is non-increasing for control (0.92 down to 0.64) and for shipped
(0.88 down to, then flat at, 0.72 from 300 on). doc-NDCG for both classes
is not monotone at all: e.g. shipped spread dNDCG rises from 0.690 (100)
to 0.667 (150) to 0.642 (200) then falls back toward 0.543 (500),
non-monotone in both directions across the sweep. Described dNDCG for
control falls 0.730 -> 0.678 -> 0.663 -> 0.635 -> 0.615 -> 0.610
(monotone decreasing); shipped described dNDCG is roughly flat to
declining (0.755, 0.711, 0.644, 0.607, 0.607, 0.587) with one local
recovery pattern in the middle (0.644 at 200 close to the 300 value) but
overall non-increasing across the six points.
