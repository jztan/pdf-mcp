# pdf_corpus_search mode benchmark (production tool, real embeddings)

Corpus: 100 docs. Queries: 64 (graded ground truth, stage-2). top_k=10. The tool itself is called per query on a warmed isolated cache, so numbers measure the agent-facing contract end to end.

| mode | overall NDCG@10 | needle | spread | trap | doc-hit@3 | s/query |
|---|---|---|---|---|---|---|
| keyword (keyword) | 0.486 | 0.688 | 0.381 | 0.476 | 0.797 | 0.38 |
| semantic (semantic) | 0.579 | 0.861 | 0.215 | 0.785 | 0.875 | 0.19 |
| auto (hybrid) | 0.661 | 0.938 | 0.392 | 0.776 | 1.000 | 0.45 |

## CJK subset (5 needle queries on Japanese docs; embedding model is English bge-small, so the semantic arm is expected to be weak there)

| mode | CJK NDCG@10 (n=5) | non-CJK NDCG@10 |
|---|---|---|
| keyword | 0.200 | 0.510 |
| semantic | 0.684 | 0.570 |
| auto | 0.826 | 0.647 |

Sanity cross-check: keyword overall should land near the stage-2 arm-B result (~0.547). Interpretation is appended by hand after the run.
## Interpretation (this run, 2026-07-25)

Hybrid is the strongest mode by a wide margin (overall 0.661, doc-hit@3 =
1.000: a gold document in the top 3 for every query) and every mode runs
in under half a second per query on a warmed 100-doc corpus. Semantic is
near-immune to lexical traps (0.785 vs keyword's 0.476); keyword anchors
literal precision; fusion captures both.

KNOWN ISSUE CAUGHT BY THIS RUN: the keyword CJK subset scores 0.2 because
`_cjk_excerpt`'s contiguity post-filter joins ALL query tokens into one
literal substring, so every multi-term CJK keyword query drops all of its
(correctly found) FTS hits. This also affects single-doc `pdf_search`
keyword mode and shipped with v1.19.1 (whose benchmark used single-term
queries only). Non-CJK keyword classes match the stage-2 spike exactly
(spread 0.381, trap 0.476). A fix is in progress; these numbers are the
pre-fix record.
