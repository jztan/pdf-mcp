# Spread Fan-Out Workflow: First Measurement (2026-07-29)

The tool's designed workflow for multi-document questions (search, then
re-ask per document named in the response) had never been measured; all
prior spread numbers were single-call. Harness:
`scripts/spike_spread_fanout.py`; raw rows: `spread_fanout_results.json`.
Caller-emitted queries, hybrid, deterministic.

## The workflow, measured

| policy | k | calls | part coverage | complete answers |
|---|---|---|---|---|
| single call (baseline) | — | 1 | 48% gold-page | — |
| fused order | 3 | 4 | 57% | 5/25 |
| fused order | 5 | 6 | 66% | 7/25 |
| named (fused + doc_match_counts) | 3 | 4 | 59% | 6/25 |
| named | 5 | 6 | **68%** | **8/25** |

The workflow beats the single call but lands far below its arithmetic
ceiling, and two-thirds of spread questions still miss at least one part
after six searches.

## The decomposition (62 gold document-parts)

| factor | value |
|---|---|
| hop-2 ceiling: gold page found when searching the RIGHT doc | **94%** (58/62) |
| selection@5: gold doc within the first 5 named | **73%** (45/62) |
| gold-doc position in named order | 39 top-3, 6 at 4-5, **13 at 6+, 4 unnamed** |

**The bottleneck is document ordering, not retrieval.** Single-doc
search almost always delivers the part when pointed at the right
document, even with the corpus-phrased query (and a real caller would
likely re-phrase per document, so 94% is a floor). What fails is the
order in which the response names documents: fused first-appearance is
a page-level signal, `doc_match_counts` is unordered and per-doc
capped, and neither ranks documents by how much they matter to the
query. This is the "router has no document-level representation" gap,
finally measured where it bites.

## Consequence: C1/C7 reopen with a real target

The doc-level prior (research doc C1: MaxP + top-m page scores + match
counts; C7: doc-fingerprint embedding) was parked as marginal when its
target was 1-in-25 recall. Its target is now: **order the fan-out list
so gold parts land in the top 3-5** — 17 of 62 parts currently do not,
capping the designed spread workflow at 68% against a 94% ceiling.
Acceptance gate for any attempt: selection@3 and selection@5 on this
harness, permutation invariance, no regression on needle/trap/described
routing or the excerpt gate. Note this needs only an ORDERING of
documents for fan-out (or an ordered variant of the doc knowledge the
response already carries) — not a re-ranking of the fused page list,
which C6's rejection showed is a minefield.

## Follow-up: the ordering race (2026-07-29) — REJECTED, ceiling reached

A three-stream research sweep (resource selection read as a selection
task, RAG multi-evidence selection, OSS implementations) produced a
convergent candidate list, raced on this harness
(`scripts/spike_fanout_ordering.py`, `fanout_ordering_race.json`;
all arms permutation-clean, knob-free or literature defaults):

| variant | sel@3 | sel@5 |
|---|---|---|
| base (shipped ordering) | 61% | 69% |
| f1_vote (decayed-vote, the literature's #1) | 53% | 66% |
| f1_lex (best-then-count) | 61% | 69% |
| f2_xquad (residual-term-coverage greedy) | 61% | 69% |
| f3_docrrf (doc-level two-arm RRF) | 53% | 71% |
| f1+f2 composed | 53% | 66% |

(Baseline reads 69% here vs 73% in the workflow measurement because the
race replicates internals with kw-only counts and a deeper fused list;
comparisons are internally consistent.)

**No variant beats the shipped ordering.** The reachability diagnostic
explains why: of the 19 hard parts (gold doc outside base top-5), only
3 are reachable at @5 by ANY ordering, and 5 are absent from every
variant's entire top-10. The gold documents' evidence under the query —
keyword and semantic, under every aggregation — genuinely ranks below
5+ other documents. This is evidence ABSENCE, not evidence
mis-aggregation: each part-document matches a fragment of the
multi-part question while non-gold documents match the whole theme.

That is precisely the one-shot retrieval ceiling MultiHop-RAG
documents (Hits@10 0.672 for bge-large; rerankers buy only 7-14
points). The shipped ordering is at the practical ceiling of the
available signals.

**Where the residual actually lives**: interactive, hop-conditioned
retrieval — after reading part 1, re-query with what it taught you
(IRCoT-class, +11-21 recall points in the literature). In this
architecture that is the CALLER's natural behavior, not a server
ranking feature. Whether real callers do it is the only remaining
measurable question, and it is a caller-behavior eval, not server work.

Do not re-propose re-ordering the fan-out list from existing signals
(see `what-we-tried.md` §8). Second-wave candidates needing new
infrastructure (corpus-global FTS with global IDF, doc fingerprints,
FAST feature models) remain unraced but face the same reachability
wall: 16 of 19 hard parts are invisible to every existing signal.

## The width curve (2026-07-29): spread is a thoroughness dial

Ordering is at its ceiling, but fan-out WIDTH is a free knob the caller
already controls. Part coverage under the shipped ordering, same-query
hop-2 (each hop-2 search ~0.5s, local):

| fan-out | searches | part coverage | complete answers |
|---|---|---|---|
| top 3 docs | 4 | 56% | 6/25 |
| top 5 | 6 | 65% | 10/25 |
| top 7 | 8 | 73% | 13/25 |
| top 10 | 11 | 79% | 14/25 |
| **everything named** | ~11-21 | **87%** | **17/25** |

The 87% is the exact caller-reachable ceiling (cross-tab against the
real tool's named set): the response names 58/62 parts and same-query
hop-2 finds 58/62, but the failure sets are DISJOINT — 4 parts are
never named, a different 4 are named but need a re-phrased per-document
query to surface. So:

- A caller who checks 5 documents experiences ~65%.
- A caller who checks everything the response names reaches 87%.
- The last 13% needs either naming the 4 invisible docs (no existing
  signal reaches them; see the ordering race) or hop-conditioned
  re-phrasing (IRCoT-class caller behavior) for the other 4.

Practical consequence: the highest-leverage "fix" for spread is
caller thoroughness over the already-shipped `doc_match_counts` list —
documentation-grade guidance, not server code. Documented in
`docs/tool-reference.md`'s `pdf_corpus_search` limitations when the
corpus feature ships.

## Caveats

- n=25 queries / 62 parts; aggregate claims only.
- Hop-2 reused the corpus-phrased query per document; real callers may
  re-phrase per doc (floor, noted above).
- Whether real callers fan out at all is unmeasured (caller-behavior
  eval, billed, if server-side work lands).
