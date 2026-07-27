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

---

## Described queries

The 64 queries above are 2-4 token noun phrases lifted verbatim from the
papers they target, so every token is present by construction and the FTS5
AND-join cannot fail. 25 `described` queries were added to remove that
guarantee: each describes a fact instead of naming it, and `--validate`
rejects any query whose content tokens all appear on its labelled pages.

Set properties: 25 queries over 25 distinct CS/ML papers, one each, median
8 content tokens against roughly 3 for the lifted set. No query clears the
gate by fewer than 2 absent content tokens.

**Pre-registered before the run.** `_fts5_or_fallback` (`src/pdf_mcp/cache.py`)
already retries OR-joined when a 3+ word query matches nothing, so:

> **H0:** keyword-mode doc-NDCG@10 on the described queries is within 0.05
> of the lifted queries.
>
> - **Confirm** -> the shipped fix generalizes to a second query
>   distribution, and the described set stays as a regression guard.
> - **Reject** -> the OR retry has a trigger gap, and that gap is the
>   finding.

The 0.05 band matches the trap-class decision threshold used above.

Excerpt fidelity carries no prior on this corpus and is reported
descriptively, with no threshold attached.

### Result: H0 rejected

Keyword-mode doc-NDCG@10, described against lifted, same corpus and run:

| mode | described (n=25) | lifted (n=64) | gap |
|---|---|---|---|
| **keyword** | **0.000** | **0.816** | **+0.816** |
| semantic | 0.698 | 0.829 | +0.131 |
| hybrid | 0.698 | 0.913 | +0.215 |

**H0 predicted a gap within 0.05. The measured keyword gap is 0.816, larger
by a factor of sixteen.** It is not a degradation: all 25 described queries
score exactly 0.000, and keyword doc-hit@3 on the class is 0.000. Not one
described query returned its gold document anywhere in the top 10.

The OR retry has a trigger gap, and this is the finding.

### The retry is gated corpus-wide, not per document

`pdf_corpus_search` collects per-document rank lists, then decides whether
to retry (`src/pdf_mcp/server.py`):

```python
rank_lists, doc_match_counts, payload = _collect(allow_or_fallback=False)
if not rank_lists and allow_or_fallback:
    rank_lists, doc_match_counts, payload = _collect(allow_or_fallback=True)
```

`rank_lists` is empty only when **no document in the entire corpus**
produced an AND match. One incidental match anywhere suppresses the retry
for every document, including the one that answers the query. The
single-document path retries per document (`if not rows:` in
`PDFCache.search_fts`), which is why `pdf_search` never showed this.

The failure is therefore a function of corpus size. Holding the query and
the gold document fixed and growing the corpus around it:

| corpus size | gold document returned |
|---|---|
| 10 | yes |
| 25 | no |
| 50 | no |
| 100 | no |

At 10 documents no document AND-matched, the retry fired, and the gold
document came back. By 25 some unrelated document matched all tokens, the
retry was suppressed, and the gold document was never reachable. Nothing
about the query or the gold document changed.

This is the AND cliff again, at corpus scale, in the tool that was supposed
to have been fixed. The fix that shipped works per document and was
validated per document; the corpus tool re-gated it and the regression went
unmeasured because every existing corpus query was a lifted phrase whose
tokens are all present by construction.

**Not fixed here.** This branch measures the shipped server and does not
modify `src/`. The described-query class is now a standing regression guard
for whatever fix lands.

### What still works

Semantic and hybrid are unaffected by the gate: both score 0.698 doc-NDCG
and 0.720 doc-hit@3 on the described class. Hybrid remains the strongest
mode overall (0.853 doc-NDCG across all 89 queries). A caller on the
default `mode="auto"` does not hit this cliff. A caller who selects
`mode="keyword"` for a described question gets nothing.

Note that hybrid earns no advantage over semantic on the described class
(0.698 both, identical). Its usual lead comes from the keyword arm, which
contributes nothing here.

### Excerpt fidelity

No prior; descriptive, per the pre-registration. Hybrid mode, both
measures reported.

| | single document | 100-doc corpus |
|---|---|---|
| `ok` (best-ranked / any gold) | 11 / 14 | 5 / 6 |
| `EXCERPT MISS` | 11 / 8 | 7 / 6 |
| `PAGE MISS` | 3 | 9 |
| `DOC MISS` | 0 | 4 |
| **recall** | **88%** | **48%** |
| **fidelity** (best-ranked / any gold) | **50% / 64%** | **42% / 50%** |

Excerpt-side and retrieval-side losses are comparable on a single document
(11 excerpt misses against 3 page misses) but retrieval dominates across the
corpus (7 against 13). That is the opposite of the financial corpus, where
snippet-side losses dominated under either measure. The likely cause is the
same gate: these are hybrid-mode numbers, and hybrid's keyword arm is dead
on this query class, so the corpus run is effectively semantic-only.

### Limitations of this query set

- **All 25 labels are on page 1.** Deliberate: 61 of the 95 lifted labels
  are also page 1, and holding page depth roughly constant is what isolates
  query phrasing, the variable H0 is about. A deep-page described set would
  confound phrasing with page depth. It is the natural follow-on and would
  measure something this set cannot.
- **25 queries support the aggregate claim only.** No per-type breakdown is
  reported. The headline effect is a 0.816 gap with every query at zero, far
  outside anything a sample of this size could manufacture, but a subtler
  effect would not be resolvable here.
- **One paper each, all CS/ML.** Conclusions about other fields in the
  corpus are not supported.
- The `diagnose_excerpt_fidelity.py` console header prints "24-doc corpus"
  regardless of dataset, a leftover from the financial arm. The run above
  searched all 100 documents. Cosmetic, affects no recorded number.
