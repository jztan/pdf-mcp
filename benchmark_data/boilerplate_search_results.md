# Search-impact benchmark: does stripping boilerplate change ranking?

Keyword (BM25 / FTS5) ranking with boilerplate left in vs stripped, via the real `pdf_search` path on a fresh index each run (`scripts/benchmark_search_impact.py`). The only variable is boilerplate removal. Semantic leg is offline here (model download); BM25 is the leg where boilerplate-on-every-page would plausibly distort ranking.

## 1. Realistic labeled queries (MRR, rank stability)

attention/gpt3 boilerplate is page-numbers only, so query terms never collide. `changed` = top-10 differs after stripping.

| pdf | query | relevant | RR base | RR strip | changed |
| --- | --- | --- | --- | --- | --- |
| attention | dropout rate | [8] | 1.00 | 1.00 | no |
| attention | why self-attention is better than recurrent layers | [7] | 0.00 | 0.00 | no |
| attention | WMT-2014 generalization capability | [8] | 0.00 | 0.00 | no |
| attention | Scaled Dot-Product Attention | [4] | 1.00 | 1.00 | no |
| attention | the parallelization advantage over sequential recurrence | [6] | 0.00 | 0.00 | no |
| gpt3 | few-shot performance | [12, 14, 16] | 1.00 | 1.00 | no |
| gpt3 | bias fairness | [34, 36, 39] | 1.00 | 1.00 | no |

**MRR: 0.571 (base) vs 0.571 (stripped); 0/7 queries changed top-10.**

## 2. GDPR distortion: control vs header-colliding queries

Label-free. `jaccard` = top-10 overlap (1.00 = identical results). `dropped` = pages in the baseline top-10 removed after stripping (header-driven hits). `control` = ordinary lookups; `collision` = terms overlapping the running header.

| kind | query | jaccard | dropped / base |
| --- | --- | --- | --- |
| control | right to erasure | 1.00 | 0/10 |
| control | data protection officer | 1.00 | 0/10 |
| control | lawfulness of processing | 1.00 | 0/10 |
| control | consent of the data subject | 1.00 | 0/10 |
| collision | official journal european union | 0.45 | 5/10 |
| collision | european union official journal | 0.45 | 5/10 |
| collision | journal of the union | 0.33 | 6/10 |

**control mean jaccard 1.00, collision mean jaccard 0.41.**

## Takeaway

- Realistic queries: BM25's IDF already down-weights text that appears on every page, so leaving boilerplate in the index does not move ranking. No benefit there.
- The benefit is real but narrow: it shows up only when query terms overlap the boilerplate (the collision rows), where the header makes otherwise-irrelevant pages match and stripping removes that noise. Documents with word-bearing running headers (legal/standards/journals) are where this earns its keep.
