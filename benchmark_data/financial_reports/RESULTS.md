# Financial-Report Retrieval Benchmark: Results

A persistent benchmark over real public-company filings, built to measure
`pdf_search` and `pdf_corpus_search` on a distribution the arXiv corpus does
not cover: very large, table-dense documents whose fiscal years are
near-duplicates of one another.

24 documents · 3,545 pages · 8 filers × 3 years · 66 retrieval queries ·
106 answerability questions.

---

## 1. Results

### Can a caller answer the question?

100 single-document questions, hybrid mode, majority-of-3 judge. Both arms
graded in the same run.

| arm | answerable in full | partial | not answerable | wrong attribution |
|---|---|---|---|---|
| search only | 65/100 | 14 | 21 | 10 |
| **search, then read the top 2 pages** | **86/100** | 7 | 7 | 8 |

**Cite 86%, and name the flow.** This server documents search-to-locate,
then `pdf_read_pages` to answer. 24 questions improved and 0 worsened —
one-directional and far outside the ±4 judge resolution. The search-only
column measures excerpt quality, not what a caller can answer.

### Retrieval quality

Deterministic; no judge, no noise floor. Cite the **doc-level** columns —
page-level is floored by label sparsity (§2).

| mode | doc-NDCG@10 | doc-hit@3 | page-NDCG@10 | single-doc NDCG@10 | s/query |
|---|---|---|---|---|---|
| keyword | 0.633 | 0.758 | 0.209 | 0.419 | 0.21 |
| semantic | 0.759 | 0.818 | 0.172 | 0.367 | 0.13 |
| **hybrid** | **0.776** | **0.818** | **0.298** | **0.488** | 0.23 |

Per class, doc-NDCG@10:

| mode | needle | route | trap | concept |
|---|---|---|---|---|
| keyword | 0.627 | 0.864 | 0.610 | 0.186 |
| semantic | 0.767 | 0.937 | 0.639 | 0.488 |
| hybrid | 0.807 | 0.927 | 0.689 | 0.468 |

Hybrid wins overall, reproducing the shipped RRF result on a genuinely
different document distribution. It is the only arm strong on both axes:
keyword collapses on paraphrase queries (concept 0.186), semantic gives up
ground on needles and traps.

**Year discrimination works** — route (which filer-year answers this?) is
the strongest class at 0.927, which is what this corpus was built to test.

**Fusion slightly dilutes pure semantic on concept** (0.468 vs 0.488): the
keyword arm contributes noise where the query shares no vocabulary with the
target, and RRF still gives it rank mass. Small, repeatable, worth watching
if paraphrase queries become a priority.

Warm (text + embeddings, 24 docs / 3,545 pages): 173s.

### What the benchmark found

Four defects, all fixed and covered by tests:

| defect | evidence |
|---|---|
| **FTS5 AND cliff** | 17 of 45 page-labeled queries (38%) returned *nothing*; keyword concept class scored exactly 0.000 |
| `pdf_corpus_search` snippet default | disagreed with `pdf_search`, which defaulted to paragraph |
| hybrid `doc_match_counts` | returned `{}`, so callers had no signal to decompose on |
| paragraph-picker tie-break | ties fell to document order, favouring prose over the block with the numbers |

The AND cliff alone justifies the corpus. FTS5 AND-joins query terms, so
**every** word had to appear on one page: "Apple Greater China net sales
decline in 2024" returned nothing because the filing says *decreased*.
Rephrasing to "Greater China net sales" returned the gold page at rank 1.

It went unmeasured for a year because the arXiv corpus's queries are
3-token noun-phrases lifted from the papers, so every token is present by
construction and the AND-join cannot fail. Financial filings force the
other style — a fact in a 10-K has no distinctive name, so it must be
described. Median query length is 6 tokens here versus 3 there.

The fix (OR retry when a 3+ word query matches nothing) is **scoped to
keyword mode**: an earlier version applied it in hybrid too and *lowered*
hybrid doc-NDCG to 0.749, because the semantic arm already covers what
keyword misses and loose matches only diluted fusion.

---

## 2. How to read these numbers

Four calibrations govern everything above. They were measured, not assumed.

### The judge disagrees with itself on 13% of verdicts

Judged metrics come from `claude -p`. Judging the same 100 payloads twice
under identical configuration moved **13 verdicts**, and the headline
landed on **74, 71, 67, 65** across four passes.

| pair | verdicts moved |
|---|---|
| old judge context vs new | 11/100 |
| **identical config, twice** | **13/100** |

- Treat a judged figure as **±4 points**. A single run is one draw.
- **Differences below ~7 points are not findings.** Mode gaps survive
  (hybrid 69% vs semantic 60% vs keyword 54% at n=68 — spreads of 9 and 15).
  Per-type breakdowns on 8–13 questions do not.
- **Do not model the floor from ballot data.** Estimated from recorded
  triples it ranges 7%–35% depending on estimator, because 3 ballots cannot
  pin a distribution. An earlier draft quoted the 7% figure as if it were
  the floor, which made an 11% comparison look like a real effect.
- The noise is **symmetric**, so mode-vs-mode comparisons on the same
  question set stay meaningful. It is the small specific claims that break.

Re-measure with `scripts/measure_judge_noise_floor.py` whenever the judge
model, its context flags, or the ballot rule changes.

### Grading the search payload alone understates by ~20 points

See §1. Any section reporting a search-only figure is measuring excerpts.

### Page-level retrieval scores are floored by labelling, not failure

Verified rather than assumed:

- every zero-scoring needle has doc-NDCG 1.000 and doc-hit@3 1 — the right
  document is retrieved and ranked first
- searching the gold document directly returns the labeled page at **rank 1**
  (checked on `needle-01` in `googl-fy2024`, `needle-21` in the 372-page
  `jpm-fy2023`)

Retrieval finds the right document and the right page within it. What fails
is fitting that page into a corpus-wide top-10 drawn from 3,545 pages of
recurring phrasing, with one labeled page per needle.

### Not every question needs a judge

`scripts/diagnose_excerpt_fidelity.py` asks whether the excerpt for a
verifiably-correct page carries the answer — deterministic, free, immune to
the noise floor. On the 100 questions: **75 ok, 20 excerpt-miss, 5
recall-miss**. Retrieval located the answering page in **95 of 100**. Prefer
it to a judged run whenever the question is about excerpts.

---

## 3. Corpus

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
serves `.docx`, so its rows are SEC-hosted ARS filings; Amazon and JPMorgan
publish a combined annual report embedding the complete Form 10-K.
JPMorgan's are the hardest documents here: 328–372 pages of heavily designed
multi-column layout with the 10-K starting around page 66–86.

PDFs are fetched, not committed (`scripts/fetch_financial_corpus.py`), each
pinned by SHA256 so a rotted URL can be replaced by any mirror serving
identical bytes. sec.gov requires a declaring User-Agent and rate-limits by
IP, so the fetcher serializes downloads and backs off on 403/429.

---

## 4. Queries and ground truth

**66 retrieval queries** in four classes. Every page label's `evidence`
string is a verbatim substring of that page's extracted text, checked by
`--validate` against the same extraction path the tool uses.

| class | n | what it measures |
|---|---|---|
| needle | 25 | the answer sits on one identifiable page |
| route | 21 | which filer-year answers this? (doc-level, no page) |
| trap | 10 | terms that are boilerplate across filings, substantive in one |
| concept | 10 | paraphrase sharing no distinctive keyword with the target |

The route class exists because this corpus's defining difficulty is
near-duplicate fiscal years: the gold year scores gain 2, the same filer's
adjacent years gain 1, so a system that finds "an Apple 10-K" but not "the
FY2024 one" is visibly penalised.

**106 answerability questions** (100 single-document, 6 multi-document),
typed figure / causal / table / risk-synthesis / definition / attribution.

Reference facts are extracted from page text by regex and asserted to
contain the figure their regex matched, then verified verbatim against the
source pages. Candidates with no verifiable fact are dropped, not guessed.

Four authoring defects were caught rather than shipped: splitting sentences
on "." truncated figures out of their own facts; widening the window
backwards let an unrelated sentence become the fact (a European Commission
match once yielded a sentence about dividends); topic-only risk anchors
matched the shareholder letter instead of Item 1A; and a double-escaped
character class silently dropped 23 specs at once.

---

## 5. Where answers are lost

Retrieval finds the answering page 95 times in 100. The losses are
downstream of that.

**The excerpt quotes the wrong paragraph of the right page.** Asked *"Why
did Apple's Greater China net sales fall in 2024?"*, retrieval returned
`aapl-fy2024 p25` at rank 1 — the correct page — and the excerpt shown was
the segment net-sales *table* from that page. The rank metric scores a
perfect hit; the caller cannot answer.

**Comparisons come back one-sided.** "Compare AWS growth with Microsoft
Cloud growth" returned evidence for exactly one company (doc coverage 0.50,
balance 0.00), with nothing in the response saying the other side is
missing.

**A minority are genuine page misses** — 5 of 100.

### Two different attribution failures

The dangerous ones, and they are not the same problem:

| setting | what goes wrong | evidence |
|---|---|---|
| across documents | wrong **fiscal year** wins the rank | asked about FY2025 buybacks, `aapl-fy2024 p47` ranks first and the FY2025 figure never appears |
| within one document | wrong **scope** — a segment's figure read as the entity's | all 6 confirmed cases in the single-doc arm |

The cross-document one is a ranking problem. The within-document one is
not: the right page was returned in 95 of 100 questions, and 4 of the 6
confirmed cases had it at **rank 1**.

Better excerpts can make the cross-document failure *worse*. Under the old
snippet default the wrong-year page produced a vague window about the fair
value of Notes; under paragraph mode it produces a crisp, quotable "During
2024, the Company repurchased 499 million shares … for $95.0 billion". The
ranking error is identical; the excerpt just made the wrong answer more
convincing.

### Wrong attribution survives reading the page

The one failure reading does not fix: 10 cases search-only, 8 after reading
— a difference inside the noise floor, and the composition is worse than
the total suggests.

- **6 persist** through reading the full page.
- **2 are new**, caused by reading: a full page carries more confusable
  figures than an excerpt, so more context is a mild risk here.

Every confirmed case is segment-vs-consolidated scope confusion, not the
fiscal-year confusion the label suggests: a segment's $10 million provision
against the firmwide $10.7 billion; Office Commercial against Intelligent
Cloud; one theatre segment's attendance against the total.

Documented as a known limitation with two rejected fixes recorded. "The
answer is missing" is recoverable by reading on — 24 of 24 such questions
were. "A wrong figure looks like the answer" is not.

### By question type

Hybrid, search-only, n=100. **Read for shape, not ranking** — at a 13%
floor, a type with 8–13 questions carries 8–15 points of judge-only
movement.

| type | n | full |
|---|---|---|
| definition | 8 | 88% |
| causal | 25 | 76% |
| figure | 35 | 74% |
| table | 18 | 72% |
| risk-synthesis | 13 | 62% |

---

## 6. Method

### The judge

Verdicts are the majority of 3 independent `claude -p` calls. A single
ballot is not enough: across 204 recorded triples, 16% of questions split —
6 returning both "full" and "no" on identical evidence — and a lone ballot
disagrees with the majority 5.7% of the time.

Ballots are spent one at a time and stop once the rest cannot change either
verdict. This is lossless by construction: two agreeing ballots are already
a majority of three. Replayed over those triples it costs **2.11 calls per
question**; the n=100 run spent 2.08.

A cheaper rule — one ballot, escalate only when it is not "full" — was
measured at 1.76 calls and **rejected**: it grades 14 "partial" and 4 "no"
questions as "full", biasing the headline upward in one direction only.

Judge context is stripped to what it can use (`--setting-sources ''`,
empty MCP config): **20,704 → 7,170 fresh input tokens per call**. Verified
not to change verdicts — 11/100 moved, fewer than the 13/100 two identical
runs move. Ballots are cached by prompt hash and replayed, so an
interrupted run resumes for free.

### Why the numbers moved as the sample grew

Ground truth was authored on 4 filers (35 queries) first, then expanded to
all 8 (66 queries), per the repo's quality loop.

| metric (hybrid) | stage A (35 q) | full (66 q) | delta |
|---|---|---|---|
| doc-NDCG@10 | 0.814 | 0.776 | −0.038 |
| doc-hit@3 | 0.886 | 0.818 | −0.068 |
| page-NDCG@10 | 0.325 | 0.298 | −0.027 |
| single-doc NDCG@10 | 0.496 | 0.478 | −0.018 |

Every metric moved down — the expected direction, and the reason the
quality loop mandates expansion. The smaller sample overstated quality by
4–7% relative, with losses concentrated in the combined annual reports.

**Four small-sample readings that reversed.** Each would have justified a
wrong decision:

1. On 9 questions **keyword led** (7/9 vs hybrid 6/9). At 25 it was last by
   five, at 49 by eight, at 68 by ten.
2. At 49 questions hybrid and semantic **tied** (34 vs 35). At 68 hybrid
   leads by six.
3. On 3 risk-synthesis questions hybrid looked **diluted**, suggesting
   fusion hurt synthesis. At 9 it scores 67% against semantic's 33% — the
   opposite conclusion.
4. At 17 questions **causal was the weak type** at 59% in both modes —
   specific enough to have justified redesigning the search-to-read
   contract. At 25 it is 76%, near the top.

Expansion alone is not sufficient: it addresses sampling, not judge
variance. Two of the four are partly attributable to the 13% floor, and
after the fact the causes cannot be separated. A claim is safe only when
the gap exceeds the measured floor.

### Superseded: the corpus answerability arm (n=15)

An earlier run put 15 analyst questions through `pdf_corpus_search` and
reported 7/15 → 9/15 across the excerpt-default fix, concluding that
"retrieval scoring 0.78 leaves fewer than half of real questions
answerable". **Do not cite it.** n=15 against a 13% floor is ~2 questions
of judge-only movement — exactly the size of the reported improvement; it
predates majority-of-3, so those are single ballots; and it grades the
search payload alone.

The excerpt-default fix it was meant to evaluate stands on a different
ground: `pdf_corpus_search` and `pdf_search` disagreed with each other.
Re-running the corpus arm under the current judge is open work.

---

## 7. Known limitations of this dataset

- **Concept labels are inherently ambiguous.** Eight filers all discuss
  supplier concentration, antitrust exposure and currency hedging, so
  pinning one "correct" document for a paraphrase is partly arbitrary.
  `concept-04` and `concept-05` miss the labeled document entirely, and the
  retrieved documents are not obviously wrong answers.
- **One labeled page per needle.** Sibling pages that legitimately answer
  the query earn no partial credit.
- **Fiscal-year windows differ per filer** (NVIDIA FY2024-26 vs Meta
  FY2022-24) because filing calendars differ. Route queries always name the
  filer's own fiscal-year label.
- **Microsoft page 1 carries no extractable text** (image cover on the ARS
  filings) — normal for glossy reports, worth knowing when labeling.
- **Judged metrics carry the 13% floor; retrieval metrics do not.**

---

## 8. Reproducing

Free and deterministic — run these first:

```bash
uv run python scripts/fetch_financial_corpus.py                # 24 PDFs, SHA256-checked
uv run python scripts/benchmark_corpus_modes.py \
    --data-dir benchmark_data/financial_reports --validate     # label check
uv run python scripts/benchmark_corpus_modes.py \
    --data-dir benchmark_data/financial_reports --single-doc-arm
uv run python scripts/diagnose_excerpt_fidelity.py             # excerpt carries the answer?
```

Billed — each spends `claude -p` calls (~2.1 ballots per question, cached
and resumable; a killed run restarts for free):

```bash
uv run python scripts/eval_single_doc_answerability.py auto --decomposed
uv run python scripts/measure_judge_noise_floor.py             # re-measure the 13% floor
uv run python scripts/eval_financial_answerability.py          # corpus arm (superseded)
```
