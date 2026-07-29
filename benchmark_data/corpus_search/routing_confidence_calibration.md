# Routing-Confidence Calibration (C3 spike, 2026-07-28)

> **VERDICT: REJECTED. The field was built, then reverted unshipped.**
> Everything below calibrates the signal on *raw benchmark query
> strings*. On the queries a real caller actually emits it scores 50%
> precision and 33% recall — see "Why this was rejected" at the end. The
> calibration numbers in the middle of this document are real but were
> measured on a population the tool does not receive. Keep this file as
> the record of how the signal was built and why it failed; do not quote
> its AUCs as evidence for a future version.

Offline calibration for a `routing_confidence` signal in
`pdf_corpus_search` hybrid mode, computed from what the hybrid path
already has at fusion time. Harness: `scripts/spike_routing_confidence.py`
(free, deterministic). Outcome variable: gold doc within the first 3
distinct docs of the fused ranking (hit@3), TOP_K=10.

The raw per-query signal dumps this harness writes
(`routing_confidence_signals.json` in the dataset directory) were not
retained: the signal is rejected, its AUCs are not reusable (see the
verdict above), and any successor must recalibrate on a different query
population anyway. Re-running the harness regenerates them for free.

## Structural finding

**The keyword arm is empty for every described query** (hybrid runs
strict-AND with no OR fallback, and question-phrased queries lexically
match nothing). Consequences:

- Fusion-agreement signals degenerate to 0 for the whole described class
  (AUC 0.500 within it); they cannot rank described queries against each
  other.
- But `keyword arm empty` is itself a clean detector of "this query
  carries no lexical anchor": the empty stratum is exactly the 25
  described queries plus 6 trap queries. This is the natural trigger for
  the C2 rewrite advice.
- Within the empty stratum, semantic-strength signals discriminate well:
  NQC (std-dev of top-10 cosines) AUC 0.833, top-cosine AUC 0.817, above
  the 0.73-0.77 the literature led us to expect.
- Only 3 of 25 terms-of-art rewrites produce keyword hits at all; the C2
  routing gain flows almost entirely through the semantic arm. Strict-AND
  over 100 docs is rarely survivable even for good rewrites.

## AUC (signal predicts hit@3, n=89 baseline queries)

| signal | all | described | non-described |
|---|---|---|---|
| doc_overlap3 (top-3 doc agreement between arms) | 0.831 | 0.500 | **0.927** |
| doc_jaccard5 | 0.824 | 0.500 | 0.899 |
| sem_nqc (std of top-10 cosines) | **0.851** | **0.833** | 0.766 |
| sem_top1 | 0.817 | 0.817 | 0.815 |
| kw_cov_max | 0.705 | 0.500 | 0.395 |
| kw_ndocs | 0.676 | 0.500 | 0.085 |

## Cross-corpus validation (10-K set) killed the NQC-only rule

The in-sample rule (stratum A = `sem_nqc < 0.008` alone) caught only 1 of
6 failures on the 66-query 10-K benchmark: the failing concept queries
there sit at NQC 0.008-0.016, values that read as "differentiated" on the
arXiv corpus. Scale-invariant variants (coefficient of variation,
relative top1-top10 gap) do NOT rescue transfer (AUC 0.62-0.68 in the
10-K empty-keyword stratum). The QPP-generalization warning was correct.

What does transfer: **sem_top1** (absolute best cosine), AUC 0.810 arXiv /
0.820 10-K in-stratum. Absolute cosine thresholds are defensible here
because the embedding model is pinned per install, the same precedent as
`_SEMANTIC_CONFIDENCE_THRESHOLD` (0.5).

## The shipped rule (validated on both corpora)

```
if keyword arm empty:                 # no lexical anchor
    low = sem_top1 < 0.755  or  sem_nqc < 0.008
else:
    low = doc_overlap3 == 0           # arms disagree on all of top-3
```

| corpus | failures caught | precision | flag rate |
|---|---|---|---|
| arXiv (n=89, fitted) | 8/9 | 67% | 13% |
| 10-K (n=66, held out) | 3/6 | 60% | 8% |

Stratum B notes: failures with a non-empty keyword arm are rare in both
corpora (2 of 58 arXiv, 1 of 51 10-K); the overlap rule catches both
arXiv failures at 5% FP and misses the single 10-K one (concept-07,
overlap 1/3). Kept because it is nearly free and errs quiet.

## Direction check on the rewritten queries

On the baseline (pre-rewrite) described class the shipped rule flags
6/25, all six true failures (precision 100%, recall 6/7): "rewrite once"
is never advised in vain. After terms-of-art rewriting (which lifts
described hit@3 from 72% to 92%), flags drop to 4/25; those four are
false positives costing at most one wasted retry each, and the 2
residual failures are not flagged. The residual failures are C1/C7's
target, not a confidence-detectable state.

## Why this was rejected (2026-07-28)

The C2 caller eval (`c2_rewrite/CALLER_EVAL.md`) established that callers
do not send raw benchmark strings; they send 2-3 content words. Re-running
the *implemented* flag through the real tool on the 39 caller-emitted
queries from that eval, scored against gold doc-hit@1:

| population | precision | recall | flag rate |
|---|---|---|---|
| raw strings, arXiv (calibration) | 67% | 8/9 | 13% |
| raw strings, 10-K (held out) | 60% | 3/6 | 8% |
| **caller-emitted queries** | **50%** (2/4) | **2/6 (33%)** | 10% |

It flagged `'Thomson problem'` — a needle query that routes perfectly —
while missing four real failures (`'model size scaling data'`,
`'training data generalization'`, `'normalization convergence'`,
`'cluster routing'`). A caller obeying it would retry queries that
worked and receive no warning on the ones that did not.

**Root cause: population mismatch, the same error as the trap recorded in
`what-we-tried.md` §6.** Raw questions never match lexically, so the
entire calibration set fell in stratum A (empty keyword arm). Caller
queries are short content terms that *do* match, so they split across
both strata — and stratum B was supported by 3 failures total across both
corpora. The end-to-end verification did not catch this because it, too,
replayed benchmark strings.

Compounding it: the action the flag recommends (re-query with terms of
art) is itself unvalidated, and the nearest evidence — the C2 teaching
eval — found that advice neutral at best.

**If anyone revisits this**, the requirements are: calibrate on
caller-emitted queries from the outset, collect enough of them that the
failure count supports a threshold (39 queries with 6 failures does not),
and validate the recommended action separately from the detector.

## Caveats

- Thresholds were fitted on the arXiv set and validated once on the 10-K
  set (held out from fitting). They are named constants
  (`_ROUTING_TOP1_THRESHOLD`, `_ROUTING_NQC_THRESHOLD` in server.py);
  re-fit when either benchmark changes materially or the default
  embedding model changes (absolute cosine scales are model-specific).
- Stratum B rests on 3 failures across both corpora. Do not quote its
  rates as findings.
- The signal predicts routing, not answerability; EXCERPT MISS is
  invisible to it.
- The flag is advisory: its action ("re-query once with terms of art")
  costs one retry, so 60-67% precision is acceptable; a hard gate would
  not be.
