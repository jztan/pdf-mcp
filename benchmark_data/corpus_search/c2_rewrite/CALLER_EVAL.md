# C2 Caller Eval: does teaching the description change caller queries?

**Verdict: NO-GO for the drafted C2 text. Do not ship it.**
Run 2026-07-28, `scripts/eval_c2_query_teaching.py`, caller model
`claude-opus-4-8`, 78 calls, 0 errors.

## Design

FR2b-style behavioral eval with **deterministic grading, no LLM judge**
(so no noise floor applies). Two arms differ ONLY in the `query`
parameter docstring shown to a simulated caller:

- **old** — the currently shipped text (keyword brevity advice)
- **new** — the drafted C2 teaching text (terms of art, verbatim
  distinctive phrases, don't-guess-names, with an inline example)

For each of 25 described + 14 needle benchmark questions, `claude -p`
sees only the tool signature + docstring + the user's question and
replies with the exact `query` string it would pass. Every emitted string
is then run through the real `pdf_corpus_search` (hybrid, warmed cache)
and scored against gold labels. `original` = the raw question text, the
no-caller reference.

## Results

| class | arm | doc-hit@1 | doc-hit@3 |
|---|---|---|---|
| described | original (raw question) | 14/25 (56%) | 18/25 (72%) |
| described | **old (shipped text)** | **19/25 (76%)** | **22/25 (88%)** |
| described | new (C2 draft) | 18/25 (72%) | 20/25 (80%) |
| needle | original | 14/14 | 14/14 |
| needle | **old (shipped text)** | **14/14** | **14/14** |
| needle | new (C2 draft) | 10/14 | 10/14 |

## Two findings, both important

**1. The currently shipped description already elicits the rewriting.**
A caller reading today's text turns raw questions into queries that route
at 76% @1 / 88% @3, versus 56% / 72% for the raw question. That is at or
above the hand-rewritten ceiling measured in `RESULTS.md` (72% / 88%).
**The gap C2 was designed to close is already closed by caller behavior
we did not have to teach.** The measured 56% baseline in
`corpus-vs-single-doc-performance.md` reflects benchmark queries fed
verbatim, which is not how an agent actually calls the tool. Section 3 of
that document should be read as a lower bound on real-world routing, not
an estimate of it.

**2. The drafted C2 text actively backfires, via example-copying.**
5 of 39 new-arm emissions were the docstring's inline example
(`"batch normalization convergence training steps"`) copied verbatim as
the query for an unrelated question, including 4 of the 14 needles
(Noetherian splitting families, volatility clustering, airline boarding,
cluster web interface). The old arm produced that string zero times. This
is few-shot anchoring: a concrete example inside a parameter docstring is
salient enough that the model emits it instead of following the
instruction it illustrates.

Excluding the contaminated emissions, the new text is merely *neutral*,
not better: described 18/24 @1 vs old 19/25, needle 10/10 vs 14/14. So
even a de-exampled rewrite of the C2 text has no measured upside over
what ships today.

## Consequences

- **Do not ship the C2 teaching text.** No upside when clean, a
  needle-breaking failure mode when not.
- **Do not put concrete query examples in a parameter docstring.** If any
  future description needs an example, it must be eval'd for copying, and
  the needle class is the control that catches it.
- The C3 `routing_low_confidence` field is unaffected: it is a response
  field, was validated separately and deterministically, and its Returns
  bullet contains no example.
- Re-baselining the described-class numbers against *caller-emitted*
  queries (not raw benchmark strings) is now the honest measurement for
  any future routing work, including the C1/C7 residual spike.

## Part 2: recall and fidelity on caller-emitted queries (2026-07-28)

Follow-up closing the gap Part 1 left open: the fidelity harness re-run
with the 25 described questions replaced by the old-arm (shipped
description) caller emissions. Free and deterministic
(`excerpt_fidelity_caller_{corpus,twohop3}_auto.json`).

| arm | raw strings | hand-rewrite ceiling | caller-emitted |
|---|---|---|---|
| corpus single-hop recall | 48% | 80% | **80%** |
| two-hop k=3 recall | 64% | 88% | **84%** |
| fidelity, best-ranked page | 42% | 60% | 60% |
| single-doc reference (raw strings) | 88% | — | — |

Failure decomposition, single-hop, best-ranked measure: **8 EXCERPT
MISS, 4 PAGE MISS, 1 DOC MISS.**

Findings:

1. **The corpus recall penalty for a real caller is ~8 points, not 40**
   (80% vs the 88% raw-string single-doc reference; note the reference
   was not re-measured with caller phrasing, so the true gap could be
   somewhat smaller or larger).
2. **Caller queries hit the hand-rewrite ceiling exactly.** There is no
   headroom left in query formulation.
3. **Routing is no longer the recall bottleneck**: 1 DOC MISS out of 25.
   The residual case for a doc-level routing prior (C1/C7 in
   `corpus-routing-research.md`) is 3 route-misses at doc-hit@3, a
   marginal target.
4. **Excerpt selection is now the dominant defect by a wide margin**:
   8 of 25 questions have the answer on a returned page but not in the
   excerpt, twice the combined page+doc misses. This matches the
   cross-arm 42-50% fidelity finding and is where quality work should go
   next.

## Caveats

- One caller model (`claude-opus-4-8`), n=39 questions, single emission
  per question per arm (no repeats, so per-question emission variance is
  unmeasured). The headline differences (5 example-copies, 4 broken
  needles) are structural failures, not marginal deltas.
- Emissions are cached in `caller_eval_cache.jsonl`; delete it to force a
  fresh sample.
- Part 1's grading measures routing only; Part 2 adds recall/fidelity
  but inherits the n=25 aggregate-claim-only limit.
- The single-doc 88% reference remains raw-string-based; a caller-phrased
  single-doc arm would need another emission run with a one-document
  framing.
