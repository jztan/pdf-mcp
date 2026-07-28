# Grouped-by-Document Response Shape: REJECTED (2026-07-28)

The last open candidate from the corpus routing investigation, killed by
its own A/B. Harness: `scripts/spike_grouped_response.py`; raw rows:
`grouped_response_ab.json`. All arms deterministic, caller-emitted
queries where they exist (described/needle/spread), raw strings for trap.

## Why it was tried

The shape decomposition (`spike_spread_shape.py`) showed the ranking
layer identifies 93% of spread gold documents (`doc_match_counts`) while
the flat top-10 response carries 75%, population-stable across raw and
caller-emitted queries. A grouped response looked like a free win.

## What was measured

Four response assemblies against gold labels (doc-cov = gold docs
represented; page-hit = gold docs whose labeled gold page is in the
response; hit1 = first-ranked doc is gold, the do-no-harm control):

| class | flat (shipped) | quota (cap 3, same budget) | doc-sections, fused order | doc-sections, kw-count order |
|---|---|---|---|---|
| needle | 100% / 100% / 14 | 100% / 100% / 14 | 100% / 100% / 14 | 100% / 100% / 14 |
| trap | 100% / **100%** / 25 | 100% / 80% / 25 | 100% / 80% / 25 | 100% / 80% / **23** |
| spread | 70% / **48%** / 19 | 76% / 39% / 19 | 71% / 47% / 19 | 72% / 47% / **17** |
| described | 96% / **76%** / 19 | 96% / 64% / 19 | 92% / 68% / 19 | 96% / 68% / **18** |

(cells: doc-cov / page-hit / hit1; doc-section arms average 14.5 hits vs
flat's 10, i.e. they fail even with a larger budget)

## Why every variant loses

1. **Per-doc caps evict gold pages.** Trap gold pages rank below their
   own document's top-3 (boilerplate terms match many pages), so any
   small cap loses them: 100% → 80% page-hit in all three grouped arms.
2. **Doc sections cannot reach the 93% coverage ceiling** because the
   gold documents the flat list misses also cannot be *ranked* into the
   top 5 sections by any available ordering (fused appearance: 71%;
   keyword count: 72%). Surfacing them requires a document-level ranking
   signal that does not exist, i.e. the C1 doc-prior, whose own
   justification shrank to 1 recall loss in 25.
3. **Reordering documents by keyword count corrupts the head** (hit1
   drops on trap/spread/described): the fused page ranking is a better
   doc ranking than raw hit counts, so "put the busiest doc first" is
   actively wrong.

## The conclusion

**The shipped design was already right.** The flat list carries the best
pages; `doc_match_counts` carries the full document coverage (93%) as an
unordered map; the tool description already tells callers to re-ask
per-document for multi-document questions. Every attempt to merge those
two views into one ranked, paged response either lost gold pages, gained
no coverage, or broke the head of the ranking. The "shape gap" is real
but its remedy is the existing two-field contract, not a third view.

Do not re-propose a grouped/nested corpus response without a validated
document-level ranking signal first (see `what-we-tried.md` §8).
