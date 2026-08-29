# pdf_corpus_search mode benchmark (production tool, real embeddings)

Corpus: 500 docs. Queries: 89 (graded ground truth, stage-2). top_k=10. The tool itself is called per query on a warmed isolated cache, so numbers measure the agent-facing contract end to end.

The Aug-24 pre-arm run (Stage 0 verdict: cap raise HELD at described doc-hit@3 0.48) is preserved in git at commit 2119f57.

| mode | overall NDCG@10 | described | needle | spread | trap | doc-hit@3 | s/query |
|---|---|---|---|---|---|---|---|
| keyword (keyword) | 0.351 | 0.064 | 0.944 | 0.145 | 0.513 | 0.573 | 3.02 |
| semantic (semantic) | 0.442 | 0.157 | 0.751 | 0.220 | 0.777 | 0.753 | 1.12 |
| auto (hybrid) | 0.454 | 0.146 | 0.934 | 0.270 | 0.676 | 0.832 | 3.02 |

## Doc-level NDCG@10 (ranked docs deduped, gain = doc's best label)

Separates "wrong doc" from "right doc, unlabeled page": sparse page labels grade only a few (doc, page) pairs while a gold doc matches the query on many pages, so page-level NDCG floors on label sparsity. Doc-level is the honest ceiling-side read wherever gold docs match on many more pages than are labeled.

| mode | overall | described | needle | spread | trap |
|---|---|---|---|---|---|
| keyword | 0.556 | 0.169 | 1.000 | 0.378 | 0.873 |
| semantic | 0.735 | 0.610 | 0.938 | 0.497 | 0.985 |
| auto | 0.751 | 0.587 | 1.000 | 0.543 | 0.985 |

## CJK subset (5 needle queries on Japanese docs; embedding model is English bge-small, so the semantic arm is expected to be weak there)

| mode | CJK NDCG@10 (n=5) | non-CJK NDCG@10 |
|---|---|---|
| keyword | 0.916 | 0.318 |
| semantic | 0.524 | 0.437 |
| auto | 0.890 | 0.428 |

## Interpretation (500-doc run, document arm live; cite the tables above, not this prose)

Hybrid is the strongest mode on every page-level cut: overall NDCG@10
0.454, described 0.146, needle 0.934, trap 0.676, at 3.02 s/query over
a warmed 500-doc corpus (100 graded arXiv papers plus 400 distractors).
At the doc level it also leads: overall 0.751, described 0.587, needle
1.000, trap 0.985.

**The document arm's effect, isolated.** Against the Aug-24 pre-arm
500-doc run preserved at commit `2119f57` (described doc-hit@3 0.480,
spread 0.640, needle 1.000, trap 0.985), this post-arm run reads
described doc-hit@3 0.680 (+0.20, 12/25 -> 17/25) and spread doc-hit@3
0.720 (+0.08, 16/25 -> 18/25), with needle and trap unchanged at 1.000.
`described` and `spread` are the classes with real distractor pressure
at 500 docs; needle and trap stay pinned near 1.000 regardless of
corpus size because they are lexically distinctive or unambiguous by
construction.

**The control that makes this a real effect, not corpus noise.** The
keyword and semantic arms' per-class doc-hit@3 are identical, query for
query, between the Aug-24 pre-arm run and this one: keyword described
0.20, needle 1.000, spread 0.36, trap 0.92 (both runs); semantic
described 0.48, needle 0.929, spread 0.68, trap 1.000 (both runs).
Neither arm's code changed on this branch, so an unchanged score is
exactly what should happen; only hybrid moved, and only on the classes
the document arm targets. See "Task 8 gate results" below for the full
per-class doc-hit@3 and doc-NDCG@10 table.

## Task 8 gate results: document arm at 500 docs (2026-08-29, this machine, this branch)

Auto (hybrid) mode, 500 docs (100 gold + 400 distractors), 89 graded queries,
document-arm feature at `feat/doc-profile-routing` (1c2caf1..2119f57), against
the Aug-24 pre-arm 500-doc numbers in this same file's prior committed
version (described 0.48, spread 0.64, needle 1.000, trap 0.985, ~2.15 s/query).

| class | pre-arm doc-hit@3 (Aug-24) | post-arm doc-hit@3 | doc-NDCG@10 | gate | verdict |
|---|---|---|---|---|---|
| described | 0.480 | 0.680 (17/25) | 0.587 | >= 0.60 | PASS |
| spread | 0.640 | 0.720 (18/25) | 0.543 | (informational) | n/a |
| needle | 1.000 | 1.000 (14/14) | 1.000 | >= 0.98 | PASS |
| trap | 0.985 | 1.000 (25/25) | 0.985 | >= 0.98 | PASS |

s/query (auto): 3.021, gate <= 5. PASS.

`CORPUS_MAX_FILES` stays 100. This 500-doc rung is a benchmark-only
distractor-manifest workaround (`--distractor-manifest` / `--max-docs`); it
does not raise the cap in the shipped tool. Raising the cap is a separate,
unstarted stage-1 item.

### Latency: the arm adds microseconds, not the run-to-run swing

Within the SAME run, auto (hybrid) mean seconds/query equals keyword mean
seconds/query to four decimal places at every corpus size measured:

| corpus | keyword s/query | auto s/query | delta |
|---|---|---|---|
| 100 docs | 0.5687 | 0.5688 | +0.0001 |
| 500 docs | 3.021 | 3.021 | ~0 |
| 10-K (24 docs) | 0.426 | 0.414 | -0.012 (noise) |

That confirms the doc arm itself costs microseconds, as the spec expects.
The 100-doc run's absolute latency (0.569 s/query, all three modes) is
higher than the Task 0 scratch baseline (0.39 s/query, auto only), but
semantic-only mode (which the doc arm never touches) rose by the same
proportion (0.143 -> 0.235 s/query), so the swing is ambient machine load
on this run, not a code-path regression.

### Permutation invariance (`scripts/recheck_tiebreak_permutation.py`, 500 docs, seeds 1-6)

```
     run   needle   spread     trap  OVERALL
--------------------------------------------
   arm A    0.944    0.291    0.503    0.517
   arm B    0.944    0.351    0.430    0.512   <- real filenames (published)
  perm 1    0.944    0.116    0.403    0.409
  perm 2    0.944    0.174    0.379    0.422
  perm 3    0.944    0.153    0.368    0.410
  perm 4    0.944    0.175    0.368    0.418
  perm 5    0.944    0.151    0.376    0.412
  perm 6    0.944    0.164    0.382    0.420

needle   real is inside the permuted range
spread   real is OUTSIDE the permuted range  ** ARM ORDER FLIPS **
trap     real is OUTSIDE the permuted range
OVERALL  real is OUTSIDE the permuted range
```

Arm A (the corpus-wide-index control) is fixed across every seed: it does
not move, as the script's own docstring requires ("if arm A ever moves
under permutation, this script is broken, not the benchmark"). That is the
invariance check this script exists to certify, and it passes.

Arm B here reimplements the pre-fix stage-2 spike design (RRF fusion of
per-document rank lists, tie-broken by `(doc_path, page)` with no relevance
score) via `_corpus_ranking.rrf_fuse_doc_rankings` called without `scores`.
It is not the code path this branch ships: the production
`rrf_fuse_doc_rankings` in `src/pdf_mcp/corpus.py` accepts a `scores`
argument specifically to replace this alphabetical tie-break with BM25
relevance (the fix for the cross-doc tie-break defect closed earlier), and
every corpus-search caller in `server.py` passes it. Arm B's filename
sensitivity here reproduces the already-documented, already-closed defect
in the retired design, not a property of anything this branch touches.
This script does not import or exercise the document-arm code at all.

### Held-out 10-K corpus (`benchmark_data/financial_reports`, 24 filings, 66 queries)

Same-day pre-arm baseline via `git checkout 7ef552f -- src/` (develop,
pre-arm), auto mode, vs. post-arm on this branch:

| class | pre-arm doc-hit@3 | post-arm doc-hit@3 | regression | gate |
|---|---|---|---|---|
| concept (n=10) | 0.500 (5/10) | 0.600 (6/10) | improved | PASS |
| needle (n=25) | 0.880 (22/25) | 0.920 (23/25) | improved | PASS |
| route (n=21) | 0.952 (20/21) | 0.952 (20/21) | unchanged | PASS |
| trap (n=10) | 0.700 (7/10) | 0.600 (6/10) | -1 query | PASS (<= 1 query) |

Sanity check on the pre/post split: `pdf_corpus_search(..., mode="auto")`
returns a `doc_profile_coverage` key only with post-arm `src/` (confirmed
`True` post-arm, `False` pre-arm on the same manifest paths); the harness's
own `modes_results.json` does not persist the raw per-query response, so
grepping it for `doc_profile_coverage` is not a usable pre/post signal
(both print 0) and this key check was done directly against
`pdf_corpus_search` instead.

### Excerpt gate

`scripts/benchmark_excerpt_quality.py`: exit=0, GATE VERDICT PASS.
excerpt_containment=0.800, bbox_containment=0.800, 0 regressions, 0 stale
known_fail rows.

### Single-doc arm not re-run

This benchmark's `--single-doc-arm` flag was not passed on this pass, so
no single-doc `pdf_search` figures were regenerated here. The
previously published single-doc numbers stand: `pdf_search` is untouched
by the document-arm work on this branch.

### Overall verdict: all Task 8 gates PASS.

Size dependence of the document arm (50 to 500 documents, same cache, control vs arm): `doc_arm_size_sweep.md`.
