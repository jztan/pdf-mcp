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
| keyword | 0.633 | 0.758 | 0.209 | 0.419 | 0.21 |
| semantic | 0.759 | 0.818 | 0.172 | 0.367 | 0.13 |
| **hybrid** | **0.776** | **0.818** | **0.298** | **0.488** | 0.23 |

These are post-fix numbers. This corpus surfaced a real defect in
keyword search (see "The AND cliff" below); the keyword and single-doc
columns are the repaired values, and hybrid is unchanged by the fix.

Warm (text + embeddings, 24 docs / 3,545 pages): 173s. Queries run against
an isolated cache warmed once per run.

Hybrid wins overall, reproducing the fusion result the shipped RRF work
established on a genuinely different document distribution. It is the only
arm that is strong on both axes: keyword is weakest on paraphrase
queries (concept doc-NDCG 0.186, against semantic's 0.488), because
those queries are written to share no distinctive vocabulary with the
target text, while semantic alone gives up ground on needles and traps.
Before the AND-cliff fix below, keyword scored exactly 0.000 on the
concept class -- those queries returned nothing at all rather than
returning something poor.

### Per class (doc-level NDCG@10)

| mode | needle | route | trap | concept |
|---|---|---|---|---|
| keyword | 0.627 | 0.864 | 0.610 | 0.186 |
| semantic | 0.767 | 0.937 | 0.639 | 0.488 |
| hybrid | 0.807 | 0.927 | 0.689 | 0.468 |

**Year discrimination works.** The route class is the strongest result in
the benchmark: doc-hit@3 of 1.000 on all eleven stage-A routing queries and
doc-NDCG 0.927-0.937 in the semantic and hybrid arms. Asking for "the
FY2024 Apple 10-K" against three near-identical Apple filings reliably
returns the right year.

## The AND cliff: a real defect this corpus surfaced

The first run of this benchmark scored keyword far lower still
(doc-NDCG 0.595, single-doc 0.291). Diagnosis showed why: FTS5 queries
are AND-joined, so **every** word of a query had to appear on the same
page. A question-shaped query like "Apple Greater China net sales
decline in 2024" returned *nothing* because the filing says "decreased",
not "decline" — 17 of 45 page-labeled queries (38%) returned zero
results. Rephrasing to "Greater China net sales" returned the gold page
at rank 1 immediately.

This was a recall cliff, not a ranking weakness, and not specific to
financial documents. It went unmeasured until now because the arXiv
corpus's queries are 3-token technical noun-phrases lifted from the
papers themselves, so every token is present by construction and the
AND-join cannot fail. Financial filings force the other style: a fact in
a 10-K has no distinctive name, so it must be described, and
near-duplicate fiscal years force qualifiers. Median query length is 6
tokens here versus 3 there.

The fix (an OR retry when a 3+ word query matches nothing, scoped to
keyword-only search) is in the changelog. Scoping matters: an earlier
version applied it in hybrid mode too and *lowered* hybrid doc-NDCG to
0.749, because the semantic arm already covers what keyword misses and
the loose matches only diluted fusion.

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

## Answerability: what the rank metrics miss

Rank metrics ask "is the gold page in the top 10". A caller asks "can I
answer my question from what came back". Those diverge badly on 10-Ks.
`scripts/eval_financial_answerability.py` puts 15 realistic analyst
questions through `pdf_corpus_search` and grades the payload the caller
actually receives, against hand-verified reference facts.

| metric | before excerpt fix | after |
|---|---|---|
| answerable in full | 7 / 15 (47%) | **9 / 15 (60%)** |
| partial | 1 / 15 (7%) | 3 / 15 (20%) |
| not answerable | 7 / 15 (47%) | **3 / 15 (20%)** |
| wrong attribution | 3 / 15 (20%) | 3 / 15 (20%) |
| mean doc coverage | 0.93 | 0.93 |

The "after" column reflects `pdf_corpus_search` defaulting to
`excerpt_style="paragraph"` (see the changelog). Note what did **not**
move: wrong attribution held at 20%, and its character changed for the
worse -- see below.

Set against hybrid's doc-NDCG of 0.776 and doc-hit@3 of 0.818, this is the
headline finding of the whole corpus: **retrieval that scores 0.78 leaves
fewer than half of real questions answerable.**

### Root cause 1: the excerpt quotes the wrong paragraph of the right page

The dominant failure, and the dangerous one. Asked *"Why did Apple's
Greater China net sales fall in 2024?"*, retrieval returned
`aapl-fy2024 p25` at **rank 1** -- the correct page, which carries the
sentence "Greater China net sales decreased during 2024 compared to 2023
due primarily to lower net sales of iPhone and iPad". The excerpt shown to
the caller was the *segment net-sales table* from the same page. The
retrieval benchmark scores this a perfect hit (it is `needle-04`); the
caller cannot answer the question.

Worse, on multi-segment MD&A pages the excerpt lands on a *confusable
neighbour*: asked about Google Cloud's operating income, the top excerpt
reads "Google Services operating income increased $25.4 billion" -- a
different segment's figure, in the position where the answer belongs. A
caller trusting the excerpt reports the wrong number. That is the
`wrong_attribution` failure, and it fires on 20% of questions.

### Root cause 2: comparisons come back one-sided

"Compare AWS growth with Microsoft Cloud growth" and "Compare Apple's and
Alphabet's R&D" both returned evidence for exactly one of the two
companies (doc coverage 0.50, balance 0.00). Nothing in the response says
the other side is missing. Multi-year trend questions show the same skew
more mildly (the thinnest year gets 10-20% of the hits).

### Root cause 3: genuine page misses

A minority: the $479M Alphabet severance charge (p39) and AMC's
$1,276.1M goodwill impairment (p58) were simply not in the top 10.

### What the excerpt fix did and did not solve

Fixing the excerpt default converted six questions from unanswerable to
answerable, because the payload finally quoted the sentence carrying the
answer rather than a window over a nearby table.

It did **not** reduce wrong attribution, and it sharpened one case. Asked
how much stock Apple repurchased in fiscal 2025, retrieval ranks
`aapl-fy2024 p47` first -- the *prior* year's filing -- and the FY2025
figure never appears. Under the old snippet default that page produced a
vague window about the fair value of Notes; under paragraph mode it
produces a crisp, quotable "During 2024, the Company repurchased 499
million shares of its common stock for $95.0 billion". The ranking error
is identical in both runs; better excerpts simply made the wrong answer
more convincing.

That is the real remaining defect and it is a **ranking** problem, not an
excerpt one: on a corpus of near-duplicate fiscal years, the wrong year
outranks the right one and nothing in the response signals it. The order
of work these findings imply is therefore: excerpt quality (done),
then per-document/per-year balance and year disambiguation, and only
then general ranking.

## Single-PDF performance, and what mode to use

The corpus numbers above answer "search 24 filings". The commoner case is
that the caller already knows which filing and searches only that one, so
document selection is off the table and what remains is within-document
retrieval and excerpt quality (`scripts/eval_single_doc_answerability.py`,
judge = majority of 3).

The default mode is measured on all 100 single-document questions; the two
alternatives were measured on the first 68 and not re-run, because their
ordering held across every expansion (n=9 → 25 → 49 → 68) and re-running
them costs more than the answer is worth:

| mode | n | answerable in full | partial | not answerable | wrong attribution |
|---|---|---|---|---|---|
| **hybrid (auto, default)** | **100** | **67-74/100 (see below)** | 9 | 17 | 9-13 |
| hybrid, first 68 only | 68 | 47/68 (69%) | 7 | 14 | 9 |
| semantic | 68 | 41/68 (60%) | 9 | 18 | 6 |
| keyword | 68 | 37/68 (54%) | 7 | 24 | 3 |

**Roughly 70% of realistic questions are fully answerable from a single
10-K** in the default mode, against 53% single-call on the 24-document
corpus. Same finding from two directions: this tool is strongest once the
caller has narrowed to a document.

The hybrid row is a **range, not a point**, and that is the honest form.
The same 100 payloads judged three times scored 74, 71, and 67 -- see
"How much of this is judge noise" below. The 69% at n=68 is inside that
range, so the apparent 69% → 74% improvement is not one; the 68 are also a
subset of the 100, so it compares question mixes, not versions.

Wrong attribution (9-13 of 100) is the failure mode worth working on: the
top excerpts lead a reader to the wrong company, segment, or fiscal year.
An earlier draft of this document called it "the stable signal" because it
read 13% at both n=68 and n=100. Re-judging showed it moves between 9 and
13 on identical payloads, so its stability across the expansion was luck.

### By question type

Hybrid at n=100; the semantic column is the n=68 run, shown only where the
two overlap enough to compare:

| type | n | hybrid | semantic (n=68) |
|---|---|---|---|
| definition | 8 | 88% | -- |
| causal | 25 | 76% | 59% |
| figure | 35 | 74% | 67% |
| table | 18 | 72% | 67% |
| risk-synthesis | 13 | 62% | 33% |

**Read this table for shape, not for ranking.** At a 13% per-verdict noise
floor, a type with 8-13 questions carries roughly ±1-2 questions of
judge-only movement, which is 8-15 percentage points. Nothing here except
the spread between the extremes is separable from noise, and even that is
marginal.

**The causal weakness reported at n=68 did not survive.** That run put
causal at 59% in both hybrid and semantic on 17 questions, which read as a
mode-independent deficiency pointing at the search-then-read contract
rather than at ranking. At 25 questions causal is 76%. Part of that is the
larger sample and part is judge variance; the two cannot be separated after
the fact. Either way the original finding was not solid enough to act on.

### Four small-sample readings that reversed

Every one of these would have justified a wrong decision if acted on:

1. On 9 questions **keyword led** (7/9 vs hybrid 6/9). At 25 it was last
   by five, at 49 last by eight, at 68 last by ten.
2. At 49 questions hybrid and semantic **tied** (34 vs 35). At 68, after
   adding causal and risk questions, hybrid leads by six.
3. On 3 risk-synthesis questions hybrid looked **diluted** (returning
   "partial" where a single arm returned "full"), suggesting fusion hurt
   synthesis. At 9 questions hybrid scores 67% against semantic's 33% --
   twice as good, the opposite conclusion.
4. At 17 questions **causal was the weak type** at 59%, equal in both
   modes -- a finding specific enough to have justified redesigning the
   search-to-read contract around it. At 25 it is 76%, near the top.

The pattern is consistent: a one-or-two-question gap at these sample sizes
is judge noise. Only differences that survive an expansion are real, and
the more specific and actionable a small-sample finding looks, the more
carefully it needs retesting before anything is built on it.

Note that expansion alone is not sufficient -- it addresses sampling, not
judge variance. Two of the four reversals above are partly attributable to
the 13% noise floor rather than to the larger sample, and after the fact
the two causes cannot be separated. A claim is only safe when the gap
exceeds the measured floor.

### How much of this is judge noise

Every number above is a difference between judged runs, which means none of
them can be read without knowing how much the judge disagrees with itself.
That cannot be derived from one run's ballots -- it has to be measured, by
judging identical stored payloads twice under identical configuration
(`scripts/measure_judge_noise_floor.py`; retrieval is deterministic, so the
judge is the only thing that varies).

**13% of verdicts move between two identical runs.** The same 100 payloads
scored 74, 71, and 67 "answerable in full" across three passes.

| pair | verdicts moved | headline |
|---|---|---|
| old judge context vs new | 11/100 | 74 → 71 |
| **identical config, twice** | **13/100** | **71 → 67** |

Consequences, applied throughout this document:

- **The headline is 67-74, not 74.** Treat ~±4 points as the resolution of
  this metric. A single run's number is one draw from that range.
- **Differences below ~7 points are not findings.** The mode gaps survive
  comfortably (hybrid 74 vs semantic 60 vs keyword 54 at n=68, spreads of
  14 and 20 points). Per-type breakdowns on 8-13 questions do not.
- **Modelling the noise floor from ballot data does not work.** Estimated
  from the recorded triples it comes out anywhere from 7% to 35% depending
  on the estimator, because 3 ballots cannot pin a distribution -- a
  question whose ballots happened to agree looks deterministic. An earlier
  draft of this section quoted the 7% figure as if it were the floor. It
  was the most optimistic end of a wide range, and it made an 11% flag
  comparison look like a possible effect when the true floor is higher
  still.

The one methodological consolation: this noise is *symmetric* and affects
all arms equally, so mode-vs-mode comparisons run on the same question set
remain meaningful at the magnitudes seen here. It is the small, specific
claims -- one type, one expansion, one metric moving a few questions --
that this floor invalidates.

### Judge cost, and why the ballots are not all spent

Each verdict is the majority of 3 independent `claude -p` calls. A single
ballot is not enough: across the 204 recorded ballot triples, 16% of
questions split, 6 of them returning both "full" and "no" on identical
evidence, and a lone ballot disagrees with the majority 5.7% of the time --
noise the same size as the mode gaps being measured.

Ballots are spent one at a time and stop as soon as the rest cannot change
either verdict, which is lossless by construction: two agreeing ballots are
already a majority of three. Replayed over those triples the rule costs
2.11 calls per question and changes no verdict; the n=100 run spent 2.08.

A cheaper rule -- one ballot, escalate only when it is not "full" -- was
measured and rejected at 1.76 calls: it grades 14 "partial" and 4 "no"
questions as "full", biasing the headline upward by ~1.5 points in one
direction only.

### Ground-truth provenance

Reference facts are extracted from page text by regex and asserted to
contain the figure their regex matched, then verified verbatim against the
source pages; three older hand-authored facts are close paraphrases.
Candidates with no verifiable fact are dropped rather than guessed at.

Four authoring defects were caught rather than shipped: splitting
sentences on "." truncated figures out of their own facts; widening the
window backwards let an unrelated sentence become the fact (a European
Commission match once yielded a sentence about dividends); topic-only risk
anchors matched the shareholder letter and the business description rather
than Item 1A; and a double-escaped character class silently dropped 23
specs at once. Where evidence did not support the question as written, the
question was reworded to what the text states.

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
