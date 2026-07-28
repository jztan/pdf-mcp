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

## Caveats

- n=25 queries / 62 parts; aggregate claims only.
- Hop-2 reused the corpus-phrased query per document; real callers may
  re-phrase per doc (floor, noted above).
- Whether real callers fan out at all is unmeasured (caller-behavior
  eval, billed, if server-side work lands).
