# pdf_corpus_search mode benchmark (production tool, real embeddings)

Corpus: 100 docs. Queries: 89 (graded ground truth, stage-2). top_k=10. The tool itself is called per query on a warmed isolated cache, so numbers measure the agent-facing contract end to end.

| mode | overall NDCG@10 | described | needle | spread | trap | doc-hit@3 | s/query |
|---|---|---|---|---|---|---|---|
| keyword (keyword) | 0.492 | 0.250 | 0.968 | 0.361 | 0.596 | 0.888 | 0.46 |
| semantic (semantic) | 0.542 | 0.450 | 0.861 | 0.215 | 0.785 | 0.876 | 0.25 |
| auto (hybrid) | 0.602 | 0.460 | 0.996 | 0.350 | 0.776 | 0.944 | 0.47 |

## Doc-level NDCG@10 (ranked docs deduped, gain = doc's best label)

Separates "wrong doc" from "right doc, unlabeled page": sparse page labels grade only a few (doc, page) pairs while a gold doc matches the query on many pages, so page-level NDCG floors on label sparsity. Doc-level is the honest ceiling-side read wherever gold docs match on many more pages than are labeled.

| mode | overall | described | needle | spread | trap |
|---|---|---|---|---|---|
| keyword | 0.838 | 0.728 | 1.000 | 0.735 | 0.960 |
| semantic | 0.835 | 0.853 | 0.974 | 0.576 | 1.000 |
| auto | 0.882 | 0.853 | 1.000 | 0.727 | 1.000 |

## CJK subset (5 needle queries on Japanese docs; embedding model is English bge-small, so the semantic arm is expected to be weak there)

| mode | CJK NDCG@10 (n=5) | non-CJK NDCG@10 |
|---|---|---|
| keyword | 0.985 | 0.462 |
| semantic | 0.684 | 0.534 |
| auto | 0.990 | 0.579 |

Sanity cross-check: keyword overall should land near the stage-2 arm-B result (~0.547). Interpretation is appended by hand after the run.
