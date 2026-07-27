# Cross-Doc Keyword Ranking Spike: Results

Corpus: 100 docs, 2238 pages (0 manifest docs missing locally).

## Decision

**Winner: rrf-fusion**

- trap-class NDCG delta +0.006 < 0.05
- spread-class regresses 0.049 > 0.02
- arm-A mean per-query cost 2.327s >= 1.0s budget

> **Corrected 2026-07-27.** The two NDCG reasons above do not survive a
> tie-break permutation test: they measure this corpus's filenames, not the
> ranking. The cost reason stands. See "CORRECTION" below before citing
> anything in this section.

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

## CORRECTION (2026-07-27): the quality argument above does not survive

**The spread-class result, and therefore the overall winner, is an artifact
of this corpus's filenames.** The cost argument is unaffected and still
stands. Everything above is left as originally written; this section
corrects it.

`rrf_fuse_doc_rankings` gives every matching document its own rank-1 page,
so all matching documents tie at exactly `1/(k+0)` and the documented
`(doc_path, page)` tie-break decides their order. Alphabetical order
therefore carries the ranking whenever more than one document matches.

That is only harmless if filenames are uncorrelated with relevance. Here
they are not: the labelled documents are the original corpus (old, low
arXiv IDs) and the 79 distractors added in the expansion are newer, higher
IDs. Measured: 58 of 100 documents are labelled, mean alphabetical position
44.1 against 49.5 for uniform, 24 of the first 30 labelled against 16 of the
last 30. Arm B collects free credit from that skew on every tied query.

Renaming every document with a stable hash removes the skew and changes
nothing else (`scripts/recheck_tiebreak_permutation.py`, six seeds):

| run | needle | spread | trap | overall |
|---|---|---|---|---|
| arm A (control) | 0.970 | **0.332** | 0.482 | **0.531** |
| arm B, real filenames | 0.968 | **0.381** | 0.476 | **0.547** |
| arm B, permuted mean | 0.968 | **0.293** | 0.450 | **0.502** |
| arm B, permuted range | 0.968 | 0.254-0.326 | 0.436-0.462 | 0.489-0.510 |

**Both decision-driving comparisons reverse.** The published table has arm B
winning spread (+0.049) and overall (+0.016). Under all six renamings arm A
wins spread (0.332 against 0.254-0.326) and overall (0.531 against
0.489-0.510). The published arm B value lies outside the entire permuted
range on both.

Two internal controls say the effect is the tie-break and not the method:
arm A never uses a document tie-break and does not move; needle scores
0.968 under every seed, because a needle query matches a median of one
document and nothing ties. Spread queries match a median of six, which is
enough to tie without flooding the top ten -- NDCG is position-weighted, so
the skew pays out through ordering, not only through which documents make
the cut.

**What is retracted:** the sentence "rrf-fusion already wins on quality
alone (overall NDCG, no trap edge for A, spread win for B), independent of
cost", and the framing of distractor-flooding robustness as a measured
spread win. Arm B's structural robustness argument may still be sound; it
is simply not what these numbers demonstrate.

**What stands:** the cost gate (arm A rebuilds a corpus-wide index per query
at 2.24-2.33s against arm B's amortized per-document indexes), the needle
and trap ties, and the finding that the initial 21-document spike was too
small. The stage-3 decision to implement per-document fusion may still be
correct on cost and architecture grounds. It is not supported by a quality
margin.

This was found by the described-query arm below, which exposed the same
tie-degeneracy in its acute form: when ~98 of 100 documents match, the
entire top ten is decided alphabetically. The mild version had been
inflating this table since the expansion run.

**The mechanism was never hidden.** `tests/test_benchmark_corpus_search.py`
has asserted it since the spike:
`test_rrf_arm_cannot_discriminate_across_docs` states that "every doc's
within-doc best hit fuses at the same RRF score; alphabetical tie-break puts
a boilerplate page first ... it cannot discriminate across docs by content,
only by within-doc rank." It was recorded as a property of arm B and then
used to score arm B, and nobody connected the two. The lesson is narrower
than "check the code": a known ranking degeneracy is also a measurement
hazard, and the corpus has to be checked for correlation with whatever the
tie-break sorts on. `scripts/recheck_tiebreak_permutation.py` now does that
check in one command.

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
8 content tokens against roughly 3 for the lifted set. Measured with the
crude stemmer this validator uses, no query clears the gate by fewer than 2
absent content tokens; re-checked against real FTS5 porter stemming, the
true minimum is 1 (described-22).

**Pre-registered before the run.** `_fts5_or_fallback` (`src/pdf_mcp/cache.py`)
already retries OR-joined when a 3+ word query matches nothing, so:

> **H0:** keyword-mode doc-NDCG@10 on the described queries is within 0.05
> of the lifted queries.
>
> - **Confirm** -> the shipped fix generalizes to a second query
>   distribution, and the described set stays as a regression guard.
> - **Reject** -> the OR retry has a trigger gap, and that gap is the
>   finding.

H0 was rejected, but **the mechanism named in that second bullet turned out
to be wrong**: the OR retry fires on every one of these queries. The
prediction is left above exactly as registered; what actually causes the
failure is below.

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

The failure is real and large; see below for what actually causes it.

### The retry fires every time; RRF-over-per-document-rank collapses to alphabetical order

The obvious first hypothesis was that `pdf_corpus_search`'s corpus-wide
OR-retry gate (`src/pdf_mcp/server.py`, `if not rank_lists and
allow_or_fallback:`) was being suppressed by an incidental AND match
elsewhere in the corpus. That hypothesis is **falsified**: verified
directly against the shipped server on the real 100-doc corpus, all 25
described queries match zero documents under the strict AND form, so
`rank_lists` is empty and the OR retry fires every single time. The gate
suppresses nothing here.

The real mechanism is downstream, in fusion. After the OR retry, a
described query typically matches most of the corpus: 98 of 100 documents
for described-01. `rrf_fuse_doc_rankings` (`src/pdf_mcp/corpus.py`) scores
each item `1/(k + rank)`, and every matching document contributes its own
rank-1 page, so all 98 documents tie at exactly `1/60`. The documented
tie-break for equal scores is `(doc_path, page)`, which is alphabetical.
For described-01 the returned top-10 is exactly the ten alphabetically
first arXiv IDs in the corpus:

```
0705.4297, 0706.0028, 0706.0954, 0706.2397, 0707.0311,
0707.1301, 0707.3690, 0707.4042, 0709.2178, 0709.2857
```

Gold (`1502.03167`) is unreachable by construction: alphabetically it sorts
well past 10.

The contrast that establishes this as the mechanism: lifted queries match a
median of 1.5 documents after OR-retry (more than 10 documents for only 9
of 64 queries), so the degenerate 98-way tie never bites and they score
0.816. Described queries, being phrased around a fact rather than the
paper's own vocabulary, retry into near-corpus-wide OR matches almost every
time, so they hit the tie on nearly every query.

**True root cause: cross-document keyword ranking carries no relevance
signal once more than `top_k` documents match.** BM25 order is used only
*within* a document; RRF fusion over per-document rankings gives every
matching document the same score at its own rank-1 page, so once matches
exceed `top_k` the cross-corpus ordering collapses to alphabetical path
order, with no relationship to relevance.

The corpus-size dependence in the earlier draft of this analysis is real
but does not test the retry-suppression hypothesis: zero documents
AND-matched at every size tested (10, 25, 50, 100). Gold appeared at
n=10 only because all 10 documents OR-matched and `top_k=10` handed every
document a slot, with gold sitting at rank 10 of 10, last. At larger sizes
more than `top_k` documents OR-match and the tie-break pushes gold out
entirely. The size dependence is a `top_k`-versus-candidate-count effect,
not evidence of a suppressed retry.

This also means the "rrf-fusion wins on the spread class" conclusion
earlier in this document rests on the same per-document RRF fusion path
that produces this collapse. That conclusion was not retested here and
this finding does not overturn it, but it warrants a recheck against a
query class that, like the described set, drives match counts past
`top_k`.

The AND-cliff fix that shipped for `pdf_search` works correctly per
document and was validated per document; this failure is downstream of it,
in how the corpus tool fuses many correctly-produced per-document rankings
together.

**Not fixed here.** This branch measures the shipped server and does not
modify `src/`. The described-query class is now a standing regression guard
for whatever fix lands.

### What still works

Semantic and hybrid both score 0.698 doc-NDCG and 0.720 doc-hit@3 on the
described class, but not because they dodge a corpus-wide gate: hybrid's
keyword arm is disabled outright for this query class. `src/pdf_mcp/server.py`
sets `allow_or_fallback=(mode == "keyword")`, so in hybrid mode the OR
retry never fires at all, by design, regardless of what else matches in
the corpus. Hybrid's 0.698 here is really semantic alone; its keyword arm
contributes nothing because it never gets a chance to retry, not because a
corpus-wide gate suppressed it. Hybrid remains the strongest mode overall
(0.853 doc-NDCG across all 89 queries). A caller on the default
`mode="auto"` does not hit the fusion collapse above. A caller who selects
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
same disabled retry: these are hybrid-mode numbers, and hybrid's keyword
arm never retries on this query class (`allow_or_fallback=(mode ==
"keyword")` disables it by design in hybrid mode, not any corpus-wide
gate), so the corpus run is effectively semantic-only.

### Limitations of this query set

- **All 25 labels are on page 1.** Deliberate: 61 of the 102 lifted labels
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
