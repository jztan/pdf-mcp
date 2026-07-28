# Excerpt Picker Investigation (2026-07-28)

Root-cause analysis of the dominant remaining quality defect (8 of 25
caller-emitted described questions lose a retrieved answer to excerpt
selection; fidelity 42-60% in every arm). Spike:
`scripts/spike_excerpt_picker.py`; raw variant race:
`excerpt_picker_variants.json`.

## The defect set, corrected

7 true misses, not 8: described-09's "miss" is measure semantics (its
span was delivered on a returned gold page; only the best-ranked-page
measure fails it, because the best-ranked gold page is a figure-caption
page). 5 of 7 reproduce identically in the single-doc arm: the picker,
not the corpus.

## Root causes (block-level forensics on all 7)

1. **Document-order tie-breaking** (~4 cases). The picker scores blocks
   by distinct query tokens; ties go to the first block in document
   order, and on a paper's page 1 the span-bearing abstract sits after
   the title/caption blocks it ties with.
2. **Hyphen-blind substring matching** (1-2 cases). 'pretraining'
   cannot match 'pre-training', scoring the span block at 0.
3. **The objective is query-side** (all cases, structurally). The
   picker maximizes similarity to the question; the caller needs the
   block carrying the answer, which the question's tokens cannot see.

## What shipped: hyphen-folded matching only

`_fold_for_match` (lowercase + drop hyphens) in the picker's scoring
and in `count_query_tokens` (which must stay consistent for the
short-block retry). Verified: full suite green, excerpt gate PASS
(0.485 containment / 0.455 bbox, 0 regressions, bbox fidelity 15/15),
and **measured exactly neutral end-to-end on both fidelity sets** (zero
changed rows on 10-K, unchanged arXiv arm). A strict correctness fix
that removes root cause 2; the surviving misses are score-level.

## What was rejected: the occurrences tie-break (three designs)

Block-level it looked strong: +5/25 on the described set (4 of 7 misses
fixed, 3 additional oks). Every end-to-end design failed the excerpt
gate a different way:

| design | gate result |
|---|---|
| tie-break by total occurrences | bbox_containment 0.455 → 0.394 (the tie-break seeks token-dense blocks; on dense pages that is the oversized one, which voids the pick and downgrades to a bboxless snippet) |
| + oversized blocks excluded from candidacy | containment 0.485 → 0.424, 2 regressions (the oversized-void-then-snippet fallback was load-bearing: the snippet carries the answer more often than the next-best block) |
| + oversized blocks merely demoted in ties | containment 0.455, 1 regression (l04) |

The l04 forensics closed the question. On its page, the score-tied
candidates are a 150-char definitional sentence carrying the literal
answer and a 1022-char topical block that abbreviates it away; the
occurrences tie-break prefers the long block. On the arXiv set the
answer lives in the long block (abstracts); on the gate set, in the
short one. **On score ties, no query-side signal separates the two.**
Document order is not "wrong" — it is one arbitrary resolution of a tie
the objective genuinely cannot resolve, and it happens to be the one
the existing corpus was tuned around.

## Where the remaining headroom is

4-6 of the 7 misses are tie-ambiguity; the honest fixes change scope:

- **Return both blocks when the top score ties** (response contract
  change: bigger payloads, demo parity, its own benchmark pass). The
  fidelity measure already joins per-page excerpts, so tied-block
  inclusion mechanically resolves the ambiguity.
- **Answer-side signals** (score blocks by what answers look like, not
  what the query says) — reranker-adjacent territory with a closed
  history; would need a fresh benchmarked case.

Do not re-propose a static tie-break change to the block picker without
a signal that separates long-block answers from short-block answers
across BOTH benchmark sets (see `what-we-tried.md` §8).
