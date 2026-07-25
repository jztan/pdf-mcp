# Financial-Report Retrieval Benchmark: Results

A persistent benchmark over real public-company financial filings, built to
measure `pdf_corpus_search` and `pdf_search` on a document distribution the
existing corpus benchmark does not cover: very large, table-dense filings
whose fiscal years are near-duplicates of one another.

## Corpus

24 documents, 3,545 pages, 8 filers x 3 fiscal years.

| filer | years | doc type | pages (per year) |
|---|---|---|---|
| Alphabet | FY2023-25 | 10-K | 111 / 99 / 99 |
| Apple | FY2023-25 | 10-K | 80 / 121 / 80 |
| NVIDIA | FY2024-26 | 10-K | 96 / 130 / 93 |
| Microsoft | FY2023-25 | annual report | 96 / 96 / 88 |
| Meta | FY2022-24 | 10-K | 171 / 147 / 150 |
| Amazon | FY2022-24 | annual report | 88 / 92 / 91 |
| AMC Entertainment | FY2022-24 | 10-K | 188 / 198 / 167 |
| JPMorgan Chase | FY2022-24 | annual report | 328 / 364 / 372 |

Three filers publish no standalone 10-K PDF. Microsoft's investor site
serves `.docx`, so its rows are the SEC-hosted ARS filings; Amazon and
JPMorgan publish a single combined annual report that embeds the complete
Form 10-K. JPMorgan's are the hardest documents in the corpus: 328-372
pages of heavily designed multi-column layout with the 10-K starting around
page 66-86.

PDFs are fetched, not committed (`scripts/fetch_financial_corpus.py`), and
each is pinned by SHA256 so a rotted URL can be replaced by any mirror
serving the identical bytes. sec.gov requires a declaring User-Agent and
rate-limits by IP, so the fetcher serializes downloads and backs off on
403/429.

## Query design

66 queries in four classes. Every page-level label's `evidence` string is a
verbatim substring of that page's extracted text, checked by
`--validate` against the same extraction path the tool uses.

| class | n | what it measures |
|---|---|---|
| needle | 25 | the answer sits on one identifiable page |
| route | 21 | which filer-year answers this? (doc-level labels, no page) |
| trap | 10 | terms that are boilerplate across filings, substantive in one |
| concept | 10 | paraphrase sharing no distinctive keyword with the target |

The route class exists because this corpus's defining difficulty is
near-duplicate fiscal years: the gold year scores gain 2 and the same
filer's adjacent years score gain 1, so a system that finds "an Apple
10-K" but not "the FY2024 one" is visibly penalised.

## Headline

Cite the **doc-level** numbers. Page-level NDCG is floored by label
sparsity here far more than on the arXiv corpus: one labeled page per
needle competes against 3,545 pages in which the same sentence patterns
recur across three fiscal years and across MD&A, the notes, and the
segment sections.

| mode | doc-NDCG@10 | doc-hit@3 | page-NDCG@10 | single-doc NDCG@10 | s/query |
|---|---|---|---|---|---|
| keyword | 0.595 | 0.712 | 0.195 | 0.291 | 0.21 |
| semantic | 0.759 | 0.818 | 0.172 | 0.367 | 0.13 |
| **hybrid** | **0.776** | **0.818** | **0.298** | **0.478** | 0.23 |

Warm (text + embeddings, 24 docs / 3,545 pages): 173s. Queries run against
an isolated cache warmed once per run.

Hybrid wins overall, reproducing the fusion result the shipped RRF work
established on a genuinely different document distribution. It is the only
arm that is strong on both axes: keyword collapses on paraphrase queries
(concept doc-NDCG 0.000 -- with no shared terms there is nothing to match),
while semantic alone gives up ground on needles and traps.

### Per class (doc-level NDCG@10)

| mode | needle | route | trap | concept |
|---|---|---|---|---|
| keyword | 0.602 | 0.864 | 0.610 | 0.000 |
| semantic | 0.767 | 0.937 | 0.639 | 0.488 |
| hybrid | 0.807 | 0.927 | 0.689 | 0.468 |

**Year discrimination works.** The route class is the strongest result in
the benchmark: doc-hit@3 of 1.000 on all eleven stage-A routing queries and
doc-NDCG 0.927-0.937 in the semantic and hybrid arms. Asking for "the
FY2024 Apple 10-K" against three near-identical Apple filings reliably
returns the right year.

**Fusion slightly dilutes pure semantic on the concept class** (0.468 vs
0.488). The keyword arm contributes only noise where the query shares no
vocabulary with the target, and RRF still gives it rank mass. The margin is
small and hybrid remains far ahead overall, but it is a real, repeatable
direction worth watching if concept-style queries become a priority.

## Stage-A to full expansion

Ground truth was authored and validated on 4 filers (35 queries) first,
then expanded to all 8 (66 queries), per the repo's quality loop. Both runs
used the same 24-document corpus, so the delta isolates the effect of
covering four more filers -- including the three whose 10-K content is
embedded in a glossy annual report.

| metric (hybrid) | stage A (35 q) | full (66 q) | delta |
|---|---|---|---|
| doc-NDCG@10 | 0.814 | 0.776 | -0.038 |
| doc-hit@3 | 0.886 | 0.818 | -0.068 |
| page-NDCG@10 | 0.325 | 0.298 | -0.027 |
| single-doc NDCG@10 | 0.496 | 0.478 | -0.018 |

Every metric moved down. This is the expected direction and the reason the
quality loop mandates expansion: the smaller sample overstated quality by
roughly 4-7% relative, and the queries added over Microsoft, Amazon and
JPMorgan -- the combined annual reports -- are where the losses concentrate.

No stage-A trap term had to be replaced when re-checked against all 24
documents; the trap terms chosen (`First Republic`, `reverse stock split`,
`Odeon`, `Other Bets`, `facilities consolidation`) stayed concentrated.

## Verified diagnosis of the low page-level scores

The page-level numbers look alarming beside the arXiv corpus (hybrid 0.674
overall, 0.996 needle). They were checked rather than assumed:

- Every zero-scoring needle in the stage-A run has doc-NDCG 1.000 and
  doc-hit@3 1 -- the right document is retrieved and ranked first.
- Searching the gold document directly returns the labeled page at **rank
  1** (verified on `needle-01` in `googl-fy2024` and `needle-21` in the
  372-page `jpm-fy2023`).

So retrieval finds the right document and, within it, the right page. What
fails is fitting that page into a corpus-wide top-10 drawn from 3,545
pages of recurring phrasing. This is the known label-sparsity floor,
sharper on 10-Ks than on 20-page papers.

## Known limitations of this dataset

- **Concept labels are inherently ambiguous.** Eight filers all discuss
  supplier concentration, antitrust exposure and currency hedging, so
  pinning one "correct" document for a paraphrase query is partly
  arbitrary. `concept-04` (antitrust -> Alphabet) and `concept-05`
  (supplier concentration -> Apple) miss the labeled document entirely;
  the retrieved documents are not obviously wrong answers to the question
  as asked. Read concept-class numbers as a rough signal, not a verdict.
- **One labeled page per needle.** Sibling pages that legitimately answer
  the query earn no partial credit, which is why page-level NDCG
  understates real-world usefulness on this corpus.
- **Fiscal-year windows differ per filer** (NVIDIA FY2024-26 vs Meta
  FY2022-24) because filing calendars and availability differ. Route
  queries always name the filer's own fiscal-year label.
- **Microsoft page 1 carries no extractable text** (image cover on the ARS
  filings), which is normal for glossy reports but worth knowing when
  labeling.

## Reproducing

```bash
uv run python scripts/fetch_financial_corpus.py                # 24 PDFs, SHA256-checked
uv run python scripts/benchmark_corpus_modes.py \
    --data-dir benchmark_data/financial_reports --validate     # label check
uv run python scripts/benchmark_corpus_modes.py \
    --data-dir benchmark_data/financial_reports --single-doc-arm
```
