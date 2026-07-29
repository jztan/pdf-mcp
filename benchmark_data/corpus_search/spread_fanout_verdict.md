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

**TRAP (flagged 2026-07-29, unresolved): every row of this table
assumes a fan-out budget k that no one has observed.** How many
documents a real agent actually re-searches after a corpus response is
unmeasured. If real callers check 2-3 docs, the field number is ~56%;
if they walk all of `doc_match_counts`, 87%. Until a caller-behavior
eval measures the k distribution (and whether tool-reference guidance
moves it — the C2 rejection proves teaching effects cannot be assumed),
quote this table as a curve, never as a single spread number. Same
trap class as "benchmark queries are not caller queries".

## The trap resolved (2026-07-29): caller k measured, and it is 2

`scripts/eval_spread_fanout_behavior.py` (25 caller simulations, real
docstring + real responses, deterministic grading;
`c2_rewrite/fanout_behavior_results.json`):

- **k distribution: median 2, mean 2.3, min 0, max 8.** The width
  curve's upper half is territory no caller visits unprompted, despite
  the description's "re-ask once per document" and the full
  `doc_match_counts` in hand.
- **Zero per-document re-phrasing (0/25).** The hop-conditioned
  behavior previously assumed to be "the caller's natural job" does not
  occur in one-shot planning.
- **Field-realistic part coverage: 56% (35/62), complete answers 6/25**
  — fairly graded as parts already delivered in the corpus response
  (20) plus parts the caller's own follow-ups found (8, +7 overlap).
  Single-turn simulation is a floor: a live agent iterating on results
  could do better, but the assumption that it does is now unbacked.

**Bottom line for the spread class: 56% today, 87% reachable, and the
entire 31-point gap is caller fan-out behavior — not ranking, not
retrieval.** The one testable lever left is description-level: an A/B
of the current text against a stronger fan-out instruction, graded on
this same harness (does k rise, does realized coverage follow, do
needle/described calls stay lean) — the C2 lesson applies (teaching
text must earn its keep behaviorally; the last drafted teaching text
failed and one inline example got copied verbatim), so nothing ships
without that eval passing.

## The description A/B (2026-07-29): the lever works, with a cost knob

Two candidate texts for the `doc_match_counts` sentence, A/B'd on the
behavior harness (`eval_spread_fanout_behavior.py --arm new|new2`;
single-sentence docstring change, everything else identical; all cells
cached):

| arm | spread coverage | spread complete | spread k (med) | needle k (mean) |
|---|---|---|---|---|
| old (shipped) | 56% | 6/25 | 2 | 1.1 |
| v1: "re-ask EVERY document listed" | **69%** | **11/25** | 5 | 2.7 (max 11) |
| v2: classify-first, then every-doc | 56% | 5/25 | 1 | 0.9 |

- **v1 passes the primary gate**: +13 points coverage, complete answers
  nearly doubled — the first intervention in the whole investigation to
  move its target. Cost: ~1.6 extra local searches (~0.8s + the
  caller's tool-call round-trips) on single-answer queries, with no
  quality harm (needle 100% both arms).
- **v2 shows the seesaw**: leading with classification restores needle
  discipline perfectly and erases the spread gain entirely (callers
  classify spread questions as single-doc and stop).
- Iteration stopped at two texts by pre-commitment: prose tuning is a
  hill-climb with per-step cost and overfitting risk (n=25, in-sample).

**Ship decision (open)**: v1's trade — 13 points of multi-document
answer coverage against ~1.6 wasted searches on single-answer queries.
The corpus feature's differentiator is multi-document questions, which
argues for v1; the cost lands on every single-answer corpus query. A
maintainer call, not a measurement. If v1 ships: description change on
the corpus feature (convenient with v1.23.0), locked by
tests/test_tool_descriptions.py, and the n=25 in-sample caveat noted.

## Multi-turn sessions (2026-07-29): the floor is the field value

The single-turn numbers carried a hedge: live agents reading results
turn-by-turn might do better. Measured with 25 real agentic sessions
(`scripts/eval_multiturn_fanout.py`: `claude -p` with pdf-mcp mounted as
a real MCP server over the warmed cache, raw questions, transcripts
graded by deterministic re-execution, `pdf_read_pages` counted):

- tool calls median 4 (max 9); documents followed up **median 1**
- per-document re-phrasing: **still zero** (median 1 distinct query
  per session) — iteration does not produce reformulation either
- part coverage **61%** vs the 56% single-turn floor; complete 5/25

The hedge is resolved: live multi-turn behavior is statistically the
single-turn floor. Nothing about seeing results makes agents fan out
wider or re-query. This removes the last argument for waiting on
"natural" caller behavior and strengthens the v1-instruction case:
behavior only moved when the description told it to (k median 2 → 5).

## Cross-corpus replication (2026-07-29): structure holds, plus one new limit

16 independently-authored spread queries over the 24-filing 10-K corpus
(44 verified labels, 19 filings; `spread_queries.json`,
`spike_fspread_replication.py`, results in
`financial_reports/spread_replication_results.json`):

| metric | 10-K | arXiv |
|---|---|---|
| docs identified (`doc_match_counts`) | 98% | 93% |
| flat top-10 coverage | 59% | 75% |
| fan-out coverage @5 → all-named | 39% → 73% | 65% → 87% |
| hop-2 ceiling | **75%** | 94% |
| selection@5 shipped vs decayed-vote | 59% = 59% | 73% > 66% |

Every structural finding replicates: near-total document
identification, a large flat-response gap, a width dial that roughly
doubles coverage (+34 points here, larger than arXiv's +22), and no
ordering variant beating the shipped order. New limit: on 100+-page
filings, hop-2 with the question query misses the gold page 25% of the
time even on the right document — page-level retrieval depth, a known
financial-corpus characteristic, independent of fan-out. The width
dial's ceiling is corpus-dependent (73% vs 87%) but its slope is not.

## Caveats

- n=25 queries / 62 parts; aggregate claims only.
- Hop-2 reused the corpus-phrased query per document; real callers may
  re-phrase per doc (floor, noted above).
- Whether real callers fan out at all is unmeasured (caller-behavior
  eval, billed, if server-side work lands).
