# Encoder swap spike: Titan v2 vs bge-small, within-document gold-page rank

Date: 2026-08-30 (late). Throwaway measurement, no product change.

Question: the 41 described queries no arm answers all route to the right
document; does a stronger encoder rank the gold page higher INSIDE that
document? Method: `spike.py` embeds every sub-page unit of each gold
document (the same `page_embedding_units` texts the index stores) with
`amazon.titan-embed-text-v2:0` (1024-d, normalized) and the query, scores
pages by max unit cosine, and ranks the gold page; the bge-small column is
the shipped index. Cost about $0.03 (3,969 Titan calls).

| encoder | n | median rank | rank 1 | top 3 | top 5 | beyond 10 |
|---|---|---|---|---|---|---|
| bge-small (shipped) | 41 | 6 | 2 | 9 | 17 | 12 |
| Titan v2 | 41 | 6 | 6 | 14 | 19 | 12 |

Titan better on 21, same on 7, worse on 13.
Median margin from the winning page to the gold page: bge 0.039, Titan 0.096 (Titan is more confident on the same wrong pages).

Verdict: a roughly 30x encoder does not move the median and leaves the
same 12 queries beyond rank 10. The encoder path is closed for described
(what-we-tried.md section 8 item 3, re-confirmed). Per-query rows in
`rows.json`: (id, doc, gold page, page count, bge rank, bge margin, Titan
rank, Titan margin). The Titan vectors are kept locally at
`docs_internal/spikes/titan_vectors_2026-08-30.jsonl` (not committed).
