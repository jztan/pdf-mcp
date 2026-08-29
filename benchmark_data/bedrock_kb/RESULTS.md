# Bedrock KB anchor benchmark

Generated 2026-08-29. Token budget 2000 per query per arm. Bedrock is an anchor, not a subject; any result is acceptable. Never average across classes.

## described

| arm | n | span recall | fidelity gap | doc-NDCG@10 | doc-hit@3 | realized k |
|---|---|---|---|---|---|---|
| P | 13 | 0.231 | 0.077 | 0.780 | 0.692 | 13.539 |
| B0 | 13 | 0.692 | 0.000 | 0.886 | 1.000 | 6.615 |
| B1 | 13 | 0.769 | 0.000 | 1.000 | 1.000 | 1.462 |

- P minus B0, span recall: -0.462 [-0.769, -0.077] (excludes zero, n=13)
- P minus B1, span recall: -0.538 [-0.769, -0.231] (excludes zero, n=13)

## needle

| arm | n | span recall | fidelity gap | doc-NDCG@10 | doc-hit@3 | realized k |
|---|---|---|---|---|---|---|
| P | 11 | 0.545 | 0.091 | 1.000 | 1.000 | 18.000 |
| B0 | 11 | 0.909 | 0.000 | 1.000 | 1.000 | 9.546 |
| B1 | 11 | 0.909 | 0.000 | 1.000 | 1.000 | 2.364 |

- P minus B0, span recall: -0.364 [-0.727, +0.000] (includes zero, n=11)
- P minus B1, span recall: -0.364 [-0.727, +0.000] (includes zero, n=11)

## spread

| arm | n | span recall | fidelity gap | doc-NDCG@10 | doc-hit@3 | realized k |
|---|---|---|---|---|---|---|
| P | 21 | 0.762 | 0.095 | 0.767 | 0.905 | 14.333 |
| B0 | 21 | 0.714 | 0.048 | 0.755 | 0.905 | 7.000 |
| B1 | 21 | 0.809 | 0.048 | 0.765 | 0.905 | 1.571 |

- P minus B0, span recall: +0.048 [-0.286, +0.381] (includes zero, n=21)
- P minus B1, span recall: -0.048 [-0.333, +0.238] (includes zero, n=21)

## trap

| arm | n | span recall | fidelity gap | doc-NDCG@10 | doc-hit@3 | realized k |
|---|---|---|---|---|---|---|
| P | 23 | 0.565 | 0.174 | 1.000 | 1.000 | 15.870 |
| B0 | 23 | 0.913 | 0.217 | 1.000 | 1.000 | 6.783 |
| B1 | 23 | 0.957 | 0.174 | 1.000 | 1.000 | 1.609 |

- P minus B0, span recall: -0.348 [-0.565, -0.130] (excludes zero, n=23)
- P minus B1, span recall: -0.391 [-0.609, -0.174] (excludes zero, n=23)

## Interpretation

Reading the tables above as "Bedrock retrieves better than pdf-mcp" would
be wrong. For every non-flagged query where arm P scored `missing`
(30 cases), `scripts/diagnose_arm_p_excerpt_gap.py` checked whether P's
kept units already included the graded (doc, page) pair, and if so,
re-extracted that page from the source PDF and re-checked the evidence
span with the harness's own `contain()` rule, independent of what P's
selected excerpt text said. Result: in 22 of those 30 cases P had already
returned the graded page, and in all 22 the span was present on that page
(0 cases where the span was absent from a page P returned). The remaining
8 cases are genuine document-routing misses, where P did not return the
graded page at all.

P's document-level metrics corroborate this: doc-hit@3 and doc-NDCG@10
are both 1.000 on `needle` and `trap`, so on those two classes P is
finding the right document essentially every time. What it loses is the
span inside a page it already retrieved, because `pdf_corpus_search`
returns a selected paragraph excerpt rather than the whole page, and the
picker sometimes chose a different block from the same page than the one
the label graded.

Realized k corroborates it again from the other direction. P keeps 13 to
18 short excerpts inside the 2,000-token budget; B0 keeps 6 to 9 chunks;
B1, with Cohere rerank, keeps only 1.5 to 2.4 chunks of about 1,000
tokens each. B1 has the fewest units in every class and the highest span
recall in every class. That is the signature of one large contiguous
chunk of raw page text being more likely to contain a specific verbatim
substring than several disjoint, individually-selected excerpts at the
same token budget, not a signature of better retrieval.

| | doc-hit@3 (needle/trap) | mean realized k | span recall by class (described/needle/spread/trap) |
|---|---|---|---|
| P | 1.000 / 1.000 | 13 to 18 | 0.231 / 0.545 / 0.762 / 0.565 |
| B0 | 1.000 / 1.000 | 7 to 10 | 0.692 / 0.909 / 0.714 / 0.913 |
| B1 | 1.000 / 1.000 | 1.5 to 2.4 | 0.769 / 0.909 / 0.809 / 0.957 |

(Per-class figures repeated from the tables above for side-by-side
comparison; not averaged.)

Span containment measures whether a verbatim substring survives into the
returned context, so at a fixed token budget it structurally rewards an
arm that returns fewer, larger, contiguous spans of raw page text over
one that returns more, smaller, selected excerpts. That is a property of
the metric, not a defect in it: it was chosen because it is unit-agnostic
across arms with very different chunking, and this asymmetry is the cost
of that choice. The actionable read for pdf-mcp is the excerpt picker
inside `pdf_corpus_search`, not the retrieval or ranking stack: this run
indicts excerpt selection, and does not indict document routing.

## Flagged for manual page-image review

Evidence span found by no arm; excluded from every mean above.

- described-02
- described-03
- described-04
- described-06
- described-08
- described-09
- described-10
- described-15
- described-16
- described-19
- described-21
- described-25
- needle-10
- needle-12
- needle-13
- spread-05
- spread-06
- spread-08
- spread-14
- trap-03
- trap-12

## Flagged review

All 21 flagged ids were checked against the current pdf-mcp extraction of
their graded page (`extract_text_from_page` on the labeled page, the same
containment function the harness uses), and two were spot-checked visually
with `pdf_render_pages` (described-02, needle-10). In every case the
evidence string is genuinely present on the graded page: verdict is **label
ok** for all 21, a real miss by every arm, not a labeling defect.

The flagged rate is concentrated in `described` (12 of 25, 48 percent):
this matches the standing finding in `docs_internal/corpus-vs-single-doc-performance.md`
and `benchmark_data/corpus_search/modes_results.md` that paraphrase-style
`described` queries are the weakest class for every retrieval mode. The
remaining 9 flagged ids split across `needle` (3, including two Japanese
queries on scanned government/academic PDFs and one CJK vertical-script
academic PDF), `spread` (4, cross-document conceptual queries with two
valid documents each) and `trap` (2). None showed a pattern of the
extraction itself being wrong: every checked page contains the cited
evidence, word for word or after whitespace normalization only.

Individual verdicts (all label ok, genuine miss for every arm):

| id | doc | page | match |
|---|---|---|---|
| described-02 | 1608.03983 | 1 | normalized |
| described-03 | 1611.03530 | 1 | normalized |
| described-04 | 1712.00409 | 1 | normalized |
| described-06 | 1905.11946 | 1 | normalized |
| described-08 | 2201.02177 | 1 | normalized |
| described-09 | 2203.15556 | 1 | exact |
| described-10 | 2205.14135 | 1 | normalized |
| described-15 | 2010.14701 | 1 | normalized |
| described-16 | 2005.14165 | 1 | normalized |
| described-19 | 1501.05624 | 1 | normalized |
| described-21 | 1612.09007 | 1 | exact |
| described-25 | 1307.7059 | 1 | normalized |
| needle-10 | ibk_72-102 | 1, 2 | exact (visually confirmed) |
| needle-12 | iwaki_koho_2025-12 | 2 | exact |
| needle-13 | iwaki_koho_2025-12 | 1 | exact |
| spread-05 | 0707.3690, 0802.0539 | 1, 2 | exact |
| spread-06 | 0707.4042, 0802.0539 | 1, 4 | exact |
| spread-08 | 0707.4042, 0710.2265 | 1, 4 | exact |
| spread-14 | 2607.09566, 2607.09556, 2607.10297 | 1, 2, 2 | exact / normalized / exact |
| trap-03 | 0707.1301 | 1 | exact |
| trap-12 | 0811.0781 | 1 | exact |

Every "normalized" case is a line-wrap artifact: the evidence string is a
single space where the extracted text has a newline from PDF line
wrapping (`normalize()` in the harness collapses both to one space before
comparing, so this is already handled correctly and is not itself a
defect).

## Provenance

- pdf-mcp commit: e19f19f
- `_EXTRACTION_VERSION`: 8
- Corpus: `benchmark_data/corpus_search` manifest, 100 docs, 2,238 pages,
  235 MB of PDFs, warmed and re-confirmed in this session (2026-08-29);
  `pdf_mcp.cache.db` mtime updated during arm P's run, confirming a live
  cache read, not a stale one
- Bedrock: us-east-1, Titan Text Embeddings v2, S3 Vectors, Cohere Rerank
  3.5 (B1 only)
- Arm P run in-session, not lifted from `modes_results.md`
- Arm ids and index stamps: see `config.arm_ids` and `config.index_stamps`
  in `results.json`
  - B0 label maps to arm id `B0-default-v1`, stack
    `pdfmcp-anchor-b0-default-v1`, ingested 2026-08-29T15:20:39Z
  - B1 label maps to arm id `B1-fixed1000-v1`, stack
    `pdfmcp-anchor-b1-fixed1000-v1`, ingested 2026-08-29T15:25:26Z
  - both stamped against manifest sha256 `7ff4f98...af1219`, matching the
    manifest used for this run (drift guard in `main()` passed for both
    arms before any query ran)
- Pilot at 1000/2000/4000 tokens on the first 20 queries (needle and
  spread classes only, since queries are grouped by class and the first
  20 land there): the sign of P minus B0 did not flip for either class
  across the three budgets. `needle` stayed negative (minus 0.273, minus
  0.364, minus 0.364). `spread` stayed non-negative (plus 0.250, 0.000,
  0.000), the move to 0.000 driven by a change in which queries were
  flagged at the larger budget (n=4 at 1000/2000 tok, n=6 at 4000 tok),
  not by a reversal. Full run proceeded at budget 2000 only, as planned.

## Observed AWS cost

- Ingest (both arms, prior session): not billed in this session; the
  design doc's modeled ingest cost is about $0.03 to $0.05 total (Titan
  embeddings plus S3 Vectors PUT/storage), pending confirmation in Cost
  Explorer
- Query runs (this session, 89 queries times 3 arms plus the 20-query
  pilot times 3 budgets times 2 arms): Titan/S3 Vectors retrieval queries
  are priced at $2.50 per million, and Cohere Rerank 3.5 (B1 only) at
  $2.00 per 1,000 queries, so the modeled cost for roughly 209 Bedrock
  calls (89 full-run queries on B0 and B1, plus 60 pilot queries on B0
  and B1) is a few cents, dominated by the rerank line; pending
  confirmation in Cost Explorer (costs typically post with a delay of
  several hours)
- Stack is retained per the design (keep the indexes); idle cost is
  about $0.02/month for both stacks combined (S3 Vectors storage plus S3
  source bucket storage for the 100 PDFs), per the cost table in
  `benchmark_data/bedrock_kb/README.md`
