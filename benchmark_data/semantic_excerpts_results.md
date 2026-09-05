# Pure-semantic excerpt quality

Generated 2026-09-05T09:36:57+00:00 at `c3d74a5` by `scripts/benchmark_semantic_excerpts.py`. Both arms run `mode="semantic"`; corpus scores span recall of the gold evidence within 2,000 tokens (top_k=25), single-doc scores answer containment in the graded page's excerpt (max_results=5).

## corpus (metric: span_recall)

| style | all | described | needle | spread | trap | retrieval | s/query |
|---|---|---|---|---|---|---|---|
| snippet | 0.288 (n=184) | 0.108 (n=83) | 0.387 (n=31) | 0.378 (n=45) | 0.600 (n=25) | doc-NDCG@10 0.765 | 1.87 |
| paragraph | 0.391 (n=184) | 0.241 (n=83) | 0.548 (n=31) | 0.378 (n=45) | 0.720 (n=25) | doc-NDCG@10 0.765 | 1.88 |

## single (metric: contains)

| style | all | prose | structured | table | retrieval | s/query |
|---|---|---|---|---|---|---|
| snippet | 0.537 (n=82) | 0.809 (n=21) | 0.667 (n=9) | 0.404 (n=52) | graded page hit 74 | 0.18 |
| paragraph | 0.744 (n=82) | 0.905 (n=21) | 0.889 (n=9) | 0.654 (n=52) | graded page hit 74 | 0.20 |

## Gate: PASS

Current minus baseline, paired bootstrap 95% CI. A class fails only when its CI lies entirely below zero.

| arm | style | class | n | baseline | current | current minus baseline |
|---|---|---|---|---|---|---|
| corpus | snippet | all | 184 | 0.288 | 0.288 | +0.000 [+0.000, +0.000] (includes zero) |
| corpus | snippet | described | 83 | 0.108 | 0.108 | +0.000 [+0.000, +0.000] (includes zero) |
| corpus | snippet | needle | 31 | 0.387 | 0.387 | +0.000 [+0.000, +0.000] (includes zero) |
| corpus | snippet | spread | 45 | 0.378 | 0.378 | +0.000 [+0.000, +0.000] (includes zero) |
| corpus | snippet | trap | 25 | 0.600 | 0.600 | +0.000 [+0.000, +0.000] (includes zero) |
| corpus | paragraph | all | 184 | 0.391 | 0.391 | +0.000 [+0.000, +0.000] (includes zero) |
| corpus | paragraph | described | 83 | 0.241 | 0.241 | +0.000 [+0.000, +0.000] (includes zero) |
| corpus | paragraph | needle | 31 | 0.548 | 0.548 | +0.000 [+0.000, +0.000] (includes zero) |
| corpus | paragraph | spread | 45 | 0.378 | 0.378 | +0.000 [+0.000, +0.000] (includes zero) |
| corpus | paragraph | trap | 25 | 0.720 | 0.720 | +0.000 [+0.000, +0.000] (includes zero) |
| single | snippet | all | 82 | 0.537 | 0.537 | +0.000 [+0.000, +0.000] (includes zero) |
| single | snippet | prose | 21 | 0.809 | 0.809 | +0.000 [+0.000, +0.000] (includes zero) |
| single | snippet | structured | 9 | 0.667 | 0.667 | +0.000 [+0.000, +0.000] (includes zero) |
| single | snippet | table | 52 | 0.404 | 0.404 | +0.000 [+0.000, +0.000] (includes zero) |
| single | paragraph | all | 82 | 0.744 | 0.744 | +0.000 [+0.000, +0.000] (includes zero) |
| single | paragraph | prose | 21 | 0.905 | 0.905 | +0.000 [+0.000, +0.000] (includes zero) |
| single | paragraph | structured | 9 | 0.889 | 0.889 | +0.000 [+0.000, +0.000] (includes zero) |
| single | paragraph | table | 52 | 0.654 | 0.654 | +0.000 [+0.000, +0.000] (includes zero) |

Queries with a different excerpt: corpus/snippet: 5/184, corpus/paragraph: 0/184, single/snippet: 1/82, single/paragraph: 0/82
