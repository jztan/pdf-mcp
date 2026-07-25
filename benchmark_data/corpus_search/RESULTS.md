# Cross-Doc Keyword Ranking Spike: Results

Corpus: 100 docs, 2238 pages (0 manifest docs missing locally).

## Decision

**Winner: rrf-fusion**

- trap-class NDCG delta +0.006 < 0.05
- spread-class regresses 0.049 > 0.02
- arm-A mean per-query cost 2.327s >= 1.0s budget

## Per-class NDCG@10

| class | temp-fts (A) | rrf-fusion (B) |
|---|---|---|
| needle | 0.970 | 0.968 |
| spread | 0.332 | 0.381 |
| trap | 0.482 | 0.476 |
| overall | 0.531 | 0.547 |

## Cost

- Arm A mean per-query (incl. per-query index build): 2.327s
- Arm B one-time index build: 2.379s (amortized in production as persistent per-doc indexes)

## Sample

This is the expanded run. The corpus grew from the initial 21 docs / 301
pages to 100 docs / 2238 pages (the 79 added EN docs are pure distractors;
all previously labeled docs are retained, so every earlier label stays
valid). The query set grew from 36 to 64: needle 14 (unchanged), spread
10 to 25, trap 12 to 25. The two decision-driving classes were roughly
doubled so the outcome no longer rests on a handful of differing queries.
New gold labels draw heavily from the previously unlabeled distractor
docs. Trap discriminating terms were verified concentrated in their single
labeled doc by grepping every term across all 100 extracted docs; five
candidate terms were rejected for appearing substantively in more than one
doc.

## What changed from the initial 21-doc / 36-query spike

The initial spike selected temp-fts on a +0.062 trap-class margin that
rested on 2 of 12 trap queries. Both the corpus scale-up and the query
expansion overturned that:

- At 21 docs the trap edge looked real and even grew to +0.092 when only
  the corpus was scaled (36 queries, 100 docs). With the trap class
  expanded to 25 queries it collapses to +0.006, a tie. The earlier
  deltas were a small-sample artifact of a few queries where arm A
  happened to place the gold page one rank higher; across 25 trap queries
  only 6 differ at all, and the two arms' wins offset (arm A takes three
  at +0.369, arm B takes two at -0.5).
- The spread class, which the small corpus could not stress, regresses for
  temp-fts at scale: -0.049 overall, 21 of 25 queries differing.

## Interpretation

temp-fts (one corpus-wide FTS5 table, global IDF) has no robust ranking
advantage at scale. It ties on needle (0.970 vs 0.968) and trap (0.482 vs
0.476), and loses spread (0.332 vs 0.381), so rrf-fusion wins overall
(0.547 vs 0.531).

The spread result is the substantive finding, and its shape matters. In
raw count the spread queries are nearly balanced (11 favor arm A, 10 favor
arm B), but arm A loses the ones it loses catastrophically: four spread
queries land at -0.49 to -0.61 for arm A, against a best arm-A gain of
+0.47. That asymmetry is intrinsic, not a measurement artifact. A single
corpus-wide BM25 table ranks all 2238 pages together, so on a multi-doc
query the 79 distractor docs can flood the top-10 and bury the gold pages
that are spread thinly across 2-3 documents. Per-doc RRF fusion is
structurally robust to this: it fuses each document's own ranking, so
every gold document's best page competes for a slot regardless of how many
distractor documents exist. That robustness is what the small 21-doc
corpus could not exhibit, because there were not enough distractors to
flood arm A.

Cost is the second gate. Arm A rebuilds the corpus-wide FTS table per
query and costs 2.33s at this scale, above the 1.0s budget; arm B builds
per-doc indexes once (2.38s) and reuses them, so its query-time cost is
negligible. The cost gate depends on the per-query-rebuild assumption a
persistent corpus-wide index would neutralize, so it is weighted less than
the spread finding. It does not change the decision: rrf-fusion already
wins on quality alone (overall NDCG, no trap edge for A, spread win for B),
independent of cost.

## What stage 3 implements

The expanded, firmer benchmark reverses the initial spike. Stage 3 should
implement cross-document keyword search as **rrf-fusion of per-document
rankings**, not a corpus-wide temp-FTS table: it wins or ties on every
quality class, is robust to distractor flooding on multi-document queries,
and is far cheaper at query time. A hybrid that grafts global-IDF
discrimination onto fusion's distractor-robustness is a possible later
refinement, but the base design is per-document fusion.
