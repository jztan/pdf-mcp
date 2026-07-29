# C2 Ceiling Experiment: Caller-Side Terms-of-Art Rewriting (2026-07-28)

Measures the ceiling of caller-side query rewriting on the described-query
class, before any server change. Step 1 of the plan in
`docs_internal/corpus-routing-research.md` §5. Free, deterministic, no LLM
judge. Measured on `develop` @ `e3a67a9` with the shipped tools; server code
untouched.

## Protocol

The 25 described queries were rewritten into terms-of-art form by an LLM
(Claude), **from the question text only: blind to gold labels, answer spans,
and corpus contents** (rewrites and the blindness statement in
`c2_rewrites.json`). This simulates a Claude-class calling agent following
the draft C2 tool-description teaching ("query with the terms of art the
answering page would use; 3–8 content-bearing terms"). Example:

> "does normalizing layer inputs converge in fewer training steps at equal
> accuracy" → "batch normalization internal covariate shift training steps
> convergence accuracy"

Variant dataset = the production `queries.json` / `fidelity_questions.json`
with ONLY the 25 described texts replaced; the other 64 queries are
byte-identical and act as a control. Runs: `benchmark_corpus_modes.py` and
`diagnose_excerpt_fidelity.py --corpus` / `--two-hop --route-k 3` with
`--data-dir` pointed at the variant.

## Results

Ranking (modes harness, hybrid, n=25 described):

| metric | baseline | rewritten |
|---|---|---|
| doc-hit@1 | 56% | **72%** |
| doc-hit@3 | 72% | **88%** |
| doc-NDCG@10 | 0.698 | **0.853** |
| page-NDCG@10 | 0.241 | **0.459** |

**Control: zero drift** on all 64 unchanged needle/trap/spread queries
(per-query page- and doc-NDCG identical to the 2026-07-28 baseline run).

Answerability (fidelity harness, hybrid, n=25 described):

| arm | recall | fidelity best-ranked | DOC MISS |
|---|---|---|---|
| single doc (baseline reference) | 88% | 50% | — |
| corpus single-hop, baseline | 48% | 42% | 4 |
| corpus single-hop, **rewritten** | **80%** | **60%** | 1 |
| two-hop k=3, baseline | 64% | 44% | — |
| two-hop k=3, **rewritten** | **88%** | **64%** | 2 |

Routing within the fidelity harness: doc-hit@1 72%, @3 92%, @5 96%
(counting method differs slightly from the modes harness; each arm is
compared within its own harness only).

## Reading

1. **Rewriting closes most of the corpus penalty in one hop** (48% → 80%
   against the single-doc 88%), and **two-hop at k=3 reaches exact parity
   with single-document search** (88%). The two-hop ceiling moved because
   routing moved (doc-hit@3 72% → 92%).
2. **The improvement lands exactly on the baseline failures**: 9 of 25
   queries improved doc-NDCG, including 4 of the 6 hard failures
   (baseline ≤0.356); 15 unchanged; 1 regressed.
3. **The one regression is the predicted failure mode.** described-02
   ("the two small image benchmarks") was rewritten with guessed dataset
   names ("MNIST CIFAR-10") and dropped 1.000 → 0.631: injected specifics
   that miss, the same mechanism as the FiQA −9% result in the rewriting
   literature (arXiv 2603.13301). The C2 teaching text must say: add terms
   of art you are confident of; do not invent concrete names.
4. **Remaining failures are mostly excerpt selection, not retrieval**:
   8 of the non-ok outcomes in the single-hop arm are EXCERPT MISS (answer
   on a returned page, missing from the excerpt), the known cross-arm
   defect.

## Caveats

- **This is a ceiling, not a shipped result.** The rewriter was an LLM
  deliberately playing the taught caller; whether real callers rewrite this
  way after reading the tool description requires the FR2b-style `claude -p`
  eval (plan §5 step 5) before any claim about shipped behavior.
- n=25 supports the aggregate claim only. The @1/@3 moves are 4 queries
  each, i.e. double the 2-query "suggestive" threshold, but per-type
  breakdowns are not defensible at this size.
- The rewriter's general ML knowledge is part of the treatment by design
  (the intended caller is a frontier LLM). A caller without domain knowledge
  would land somewhere between baseline and this ceiling.
- Ranking-harness and fidelity-harness routing numbers differ by counting
  method (88% vs 92% @3); compare within one harness only.

## Files

- `c2_rewrites.json` — the 25 rewrites + blindness protocol
- `modes_results.{json,md}` — full 89-query run on the variant dataset
- `excerpt_fidelity_corpus_auto.json`, `excerpt_fidelity_twohop3_auto.json`
  — fidelity arms on the variant dataset
