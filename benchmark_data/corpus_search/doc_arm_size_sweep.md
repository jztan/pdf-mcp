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
- Below 100 (three 50-doc subsets of the gold docs, only fully covered
  queries graded, n per class 6 to 14): described +4, +3, +2 queries with
  no loss; spread flat in two subsets and -1 (spread-08 again) in the
  third; needle flat; trap hit@3 flat. The arm helps described at every
  size measured, and spread-08 is the one recurring casualty below 150.
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

## size 50 (gold subsets)

Three fixed-seed 50-doc draws from the 100 gold docs (seeds 20260829,
20260830, 20260831; deterministic shuffle of manifest order, first 50),
run on cache100, no re-warming. A query is graded only when every one of
its gold documents is inside the subset; a query with any gold doc
missing from the subset is skipped entirely, so n varies per subset and
per class and is smaller than the full 25/25/14/25 counts used at sizes
100 and up.

### seed 20260829 (37/89 queries eligible)

| class | n | control h@3 | shipped h@3 | delta (queries) | control dNDCG | shipped dNDCG |
|---|---|---|---|---|---|---|
| described | 14 | 0.571 | 0.857 | +4 | 0.699 | 0.724 |
| spread | 6 | 1.000 | 1.000 | 0 | 0.914 | 0.797 |
| needle | 6 | 1.000 | 1.000 | 0 | 1.000 | 1.000 |
| trap | 11 | 1.000 | 1.000 | 0 | 1.000 | 1.000 |

Flipped: described gained described-16, described-18, described-21,
described-24; nothing lost in any class.

### seed 20260830 (36/89 queries eligible)

| class | n | control h@3 | shipped h@3 | delta (queries) | control dNDCG | shipped dNDCG |
|---|---|---|---|---|---|---|
| described | 14 | 0.643 | 0.857 | +3 | 0.758 | 0.791 |
| spread | 7 | 1.000 | 1.000 | 0 | 0.808 | 0.896 |
| needle | 5 | 1.000 | 1.000 | 0 | 1.000 | 1.000 |
| trap | 10 | 1.000 | 1.000 | 0 | 1.000 | 0.963 |

Flipped: described gained described-07, described-10, described-18;
nothing lost in any class.

### seed 20260831 (32/89 queries eligible)

| class | n | control h@3 | shipped h@3 | delta (queries) | control dNDCG | shipped dNDCG |
|---|---|---|---|---|---|---|
| described | 8 | 0.750 | 1.000 | +2 | 0.799 | 0.816 |
| spread | 6 | 1.000 | 0.833 | -1 | 0.642 | 0.694 |
| needle | 6 | 1.000 | 1.000 | 0 | 1.000 | 1.000 |
| trap | 12 | 1.000 | 1.000 | 0 | 1.000 | 1.000 |

Flipped: described gained described-13, described-17; spread lost
spread-08, the same query the 100-doc corpus loses.

### Reading

The arm loses a query at 50 docs only in one of three subsets (seed
20260831, spread-08, the same gold document the 100-doc corpus also
loses to the arm); the other two seeds show described-only gains and no
losses anywhere. Needle is flat (h@3 1.000, dNDCG 1.000) in all three
subsets. Trap h@3 is flat at 1.000 in all three, but trap dNDCG dips for
the shipped row at seed 20260830 (1.000 control vs 0.963 shipped, n=10)
with no hit@3 change, so the arm reorders a trap result there without
demoting the correct top pick.

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
300) to +5 (350, 500), and never negative.

**The spread loss is not the same two queries at every size.** spread-03
and spread-08 are lost to the arm only at size 100; from 150 onward
control already misses both regardless of the arm (buried by
distractors), so there is no incremental arm-caused loss on them past
100. From 350 onward the arm nets a spread gain instead: spread-07 at
350, spread-11 at 400, spread-11 and spread-18 at 500. These are three
different queries across three sizes, not one query recurring, so the
net spread effect flips sign across the sweep and keeps changing which
query it is made of: -1 query at 100, 0 at 150-300, +1 at 350 and 400
(different query each time), +2 at 500.

**Needle never moves.** Doc-hit@3 and doc-NDCG are 1.00/1.000 for both
rows at every size, including 350; the arm has zero measurable effect on
this class anywhere in the sweep.

**Trap moves only with corpus size, never with the arm.** hit@3 stays
1.00 throughout. dNDCG stays 1.000 for both rows through size 400
(350 included) and drops to 0.985 for both rows at size 500, identically,
meaning the drop comes from distractor dilution at 500 docs, not from the
doc-profile arm (control and shipped are equal at every size).

**Hit@3 curves are not cleanly monotone; doc-NDCG curves mostly are, once
350 is in.** Control described h@3 is non-increasing across all seven
points (0.64, 0.64, 0.64, 0.56, 0.52, 0.52, 0.48 at 100/150/200/300/350/
400/500). Shipped described h@3 is not: 0.72, 0.72, 0.64, 0.64, 0.68,
0.68, 0.68, a dip to 16/25 queries at 200-300 followed by a rise to 17/25
that holds from 350 through 500. Control spread h@3 is non-increasing
(0.92 down to 0.64, flat at 300-350). Shipped spread h@3 is not: 0.88,
0.84, 0.80, 0.72, 0.76, 0.72, 0.72, a one-point bump at 350 (spread-07,
the query that is gained only at that size) sitting between two 0.72
plateaus.

Doc-NDCG is cleaner: both spread curves (control 0.722, 0.658, 0.582,
0.556, 0.546, 0.521, 0.508; shipped 0.690, 0.667, 0.642, 0.594, 0.587,
0.572, 0.543) are monotone decreasing across all seven sizes, including
350, correcting an earlier read of this data before 350 was added that
called the shipped spread dNDCG curve non-monotone. Control described
dNDCG is also monotone decreasing (0.730, 0.678, 0.663, 0.635, 0.618,
0.615, 0.610). Shipped described dNDCG is the one doc-NDCG series that is
not monotone: 0.755, 0.711, 0.644, 0.607, 0.615, 0.607, 0.587, a small
bump at 350 (0.607 -> 0.615) before falling back to 0.607 at 400. So the
only two non-monotone series in the sweep, shipped described h@3 and
shipped spread h@3, are both hit@3 metrics on the shipped row, and 350 is
where the spread one shows its bump.
