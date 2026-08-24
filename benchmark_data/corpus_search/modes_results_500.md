# pdf_corpus_search mode benchmark (production tool, real embeddings)

Corpus: 500 docs. Queries: 89 (graded ground truth, stage-2). top_k=10. The tool itself is called per query on a warmed isolated cache, so numbers measure the agent-facing contract end to end.

| mode | overall NDCG@10 | described | needle | spread | trap | doc-hit@3 | s/query |
|---|---|---|---|---|---|---|---|
| keyword (keyword) | 0.351 | 0.064 | 0.944 | 0.145 | 0.513 | 0.573 | 2.08 |
| semantic (semantic) | 0.442 | 0.157 | 0.751 | 0.220 | 0.777 | 0.753 | 0.89 |
| auto (hybrid) | 0.480 | 0.157 | 0.961 | 0.260 | 0.754 | 0.753 | 2.15 |

## Doc-level NDCG@10 (ranked docs deduped, gain = doc's best label)

Separates "wrong doc" from "right doc, unlabeled page": sparse page labels grade only a few (doc, page) pairs while a gold doc matches the query on many pages, so page-level NDCG floors on label sparsity. Doc-level is the honest ceiling-side read wherever gold docs match on many more pages than are labeled.

| mode | overall | described | needle | spread | trap |
|---|---|---|---|---|---|
| keyword | 0.556 | 0.169 | 1.000 | 0.378 | 0.873 |
| semantic | 0.735 | 0.610 | 0.938 | 0.497 | 0.985 |
| auto | 0.748 | 0.610 | 1.000 | 0.508 | 0.985 |

## CJK subset (5 needle queries on Japanese docs; embedding model is English bge-small, so the semantic arm is expected to be weak there)

| mode | CJK NDCG@10 (n=5) | non-CJK NDCG@10 |
|---|---|---|
| keyword | 0.916 | 0.318 |
| semantic | 0.524 | 0.437 |
| auto | 0.890 | 0.456 |

Sanity cross-check: compare keyword overall against the stage-2 arm-B reference (~0.547) only on the 64 non-`described` queries. The 89-query overall includes `described`, where keyword scores ~0.13, so it lands well below the reference on query mix alone. Interpretation is appended by hand after the run.

## Stage 0 verdict (500-doc rung: 100 gold + 400 arXiv distractors)

**Decision: HOLD the cap raise.** The described-query routing gate fails under
5x distractor pressure. Per the scale-1k design's pre-agreed fallback, do NOT
raise `CORPUS_MAX_FILES`; open the doc-level-embeddings routing investigation.

The 900-doc (1,000-total) rung was deliberately NOT run: the rung was staged
at 500 specifically so a described-gate failure stops the work before the full
download. It did.

### Ship gates (hybrid/auto is the production default; baseline = committed 100-doc run)

| gate | 100-doc baseline | 500-doc rung | verdict |
|---|---|---|---|
| needle doc-NDCG ~1.0 (>=0.98) | 1.000 | 1.000 | PASS |
| trap doc-NDCG ~1.0 (>=0.98) | 1.000 | 0.985 | PASS |
| **described doc-hit@3 >= 0.60** | **0.720** | **0.480** | **FAIL** |
| hybrid latency <= 5s/query | 0.41s | 2.15s | PASS |
| permutation invariance | unit 9/9 pass | arm-A control flat | PASS |

Cite per-class numbers, never the aggregate (measurement-trap rule). All
metrics are deterministic; no LLM judge ran, so no noise floor applies.

### What moved, per class (doc-hit@3, hybrid)

| class | 100 | 500 | delta |
|---|---|---|---|
| needle | 1.000 | 1.000 | 0 |
| trap | 1.000 | 1.000 | 0 |
| spread | 0.920 | 0.640 | -0.280 |
| described | 0.720 | 0.480 | -0.240 |

needle and trap (literal-anchor queries) are immune to distractor pressure.
The collapse is entirely in `described` (paraphrase) and `spread` (scattered
gold), the classes that lean on semantic routing, which dilutes as distractors
crowd the embedding space. The keyword arm degrades further on its own
(described doc-hit@3 0.52 -> 0.20, trap doc-NDCG 0.960 -> 0.873); hybrid's
semantic arm cushions it, which is why hybrid trap doc-NDCG holds at 0.985.

### Latency

Hybrid 0.41s -> 2.15s/query (keyword 0.40 -> 2.08, semantic 0.18 -> 0.89),
roughly linear in corpus size and far under the 5s gate. The 0.41s baseline
is this run's fresh 100-doc parity re-run; the committed `modes_results.md`
records 0.47s from the Aug-23 baseline. Both pass the gate; the small delta
is run-to-run timing, not a regression. Latency was never the
constraint; retrieval quality is. This is consistent with the design's
"described is the binding constraint, not corpus size."

### Permutation invariance

Production cross-document ranking invariance is unit-tested at the fusion level
(`tests/test_corpus.py::test_ranking_is_invariant_under_document_renaming` and
`::test_equal_scores_still_break_deterministically`): renaming never reorders
results except at exact score ties, which break deterministically by
`(doc_path, page)`. Those pass unchanged (9/9). The rename-based recheck
(`recheck_tiebreak_permutation.py --distractor-manifest ...`, 6 seeds, 500 docs)
holds its arm-A control flat, confirming the harness and corpus are uncorrupted
with distractors folded in.

Caveat worth recording: that recheck tests the stage-2 SPIKE arm B (raw RRF,
pure `(doc_path, page)` tie-break), whose "OUTSIDE the permuted range" result
for spread/trap is pre-existing and is exactly the filename sensitivity the
production `term-coverage x IDF` tie-break fix removed. At 500 docs that spike
sensitivity grows (arm B real 0.512 vs permuted mean 0.415 overall), because
distractor filenames correlate with anti-relevance (gold = low 2007-era arXiv
IDs, sorted first; distractors = newer high IDs). The production tool neutralizes
this via term-coverage; where it cannot (exact ties), gold sorting first would
if anything flatter the measured described score, so the true described
doc-hit@3 is <= 0.48. That makes the FAIL more decisive, not less.

### Corpus

Gold: the existing 100-doc READoc-arXiv set, 89 graded queries, untouched.
Distractors: 400 arXiv PDFs (newest submissions across cs.LG/cs.CL/cs.CV/
math.PR/physics.data-an), deduped against gold by base id and title, 0 overlap,
recorded in `distractor_manifest.json` (ids + sha256; PDFs not committed).
