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

Hybrid mode, majority-of-3 judge, both arms graded in the same run so the
comparison is paired.

**One filing — the caller already knows which document** (n=100):

| arm | answerable in full | partial | not answerable | wrong attribution |
|---|---|---|---|---|
| search only | 65/100 | 14 | 21 | 10 |
| **search, then read the top 2 pages** | **86/100 (86%)** | 7 | 7 | 8 |

24 improved, 0 worsened.

**24-document corpus — the document must be found first** (n=106, includes
the 6 multi-document questions):

| arm | answerable in full | partial | not answerable | wrong attribution |
|---|---|---|---|---|
| search only | 65/106 (61%) | 14 | 27 | 17 |
| **search, then read the top 2 pages** | **76/106 (72%)** | 12 | 18 | 19 |

18 improved, 5 worsened (sign test p = 0.005).

**Cite 86% for the single-document case and 72% for the corpus, and name
the flow.** This server documents search-to-locate, then `pdf_read_pages`
to answer; the search-only rows measure excerpt quality, not what a caller
can answer.

**The two settings differ in a way worth stating.**

| | one filing | 24-doc corpus |
|---|---|---|
| search only | 65% | 61% |
| search + read | **86%** | **72%** |
| questions reading made **worse** | 0 | 5 |
| wrong attribution | 10% | 16% |

Reading the located pages is close to free when the filing is already
known. Across a corpus it is a weaker and *riskier* move: the top pages can
come from the wrong filing, so reading imports a confusable year's figures
instead of resolving anything. Wrong attribution rises rather than falls
(17 → 19), and sits 6 points above the single-document rate.

Multi-document questions (n=6) barely move — 1 → 2 full, 4 partial in both
arms. Reading two pages cannot answer a question spanning two filings;
that is what the follow-up arm (`--arms followup`, scoped re-searches of
documents that matched but won no slot) exists for, and it was not run
here.

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
| `pdf_corpus_search` snippet default | **23% → 61% answerable** (search-only, n=106, paired, p<0.0001); also disagreed with `pdf_search` |
| hybrid `doc_match_counts` | returned `{}`, so callers had no signal to decompose on |
| paragraph-picker tie-break | ties fell to document order, favouring prose over the block with the numbers |

The excerpt-default fix is the branch's largest measured improvement.
Re-judging the same 106 questions with `excerpt_style="snippet"` — byte-for-
byte the pre-branch behavior, still a supported parameter — scored **24/106
(23%) answerable in full against 65/106 (61%)** with paragraph excerpts:
51 questions improved, 6 worsened, and wrong attribution fell 26 → 17.
Before this branch, a caller grading corpus search payloads alone got
snippet windows that answered fewer than a quarter of realistic questions.

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

### Grading the search payload alone understates the product

By ~21 points on a single filing (65 → 86) and ~11 across the corpus
(61 → 72). Any section reporting a search-only figure is measuring excerpt
quality, not answerability.

The gap is not uniform, and reading is not uniformly safe: on one filing it
worsened nothing, across the corpus it worsened 5 questions and raised
wrong attribution. Quote the arm and the setting together.

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
the noise floor. Prefer it to a judged run whenever the question is about
excerpts rather than answerability.

**It reports two fidelity measures and does not choose between them.** They
answer different questions, and the choice changes the number by 26 points,
so both are always printed:

- **best-ranked page** — does the excerpt for the highest-ranked answering
  page carry the answer? One page per question, so the two settings are
  comparable.
- **any gold page** — does *any* returned answering page's excerpt carry
  it? Closer to what the caller's payload actually offers, but **not
  comparable across settings**: a single-document search gives all 10 slots
  to the answering filing (367 gold pages over 100 questions) while a
  corpus search shares 10 across 24 documents (172), so it gets more
  attempts at the same question.

| | one filing | | 24-doc corpus | |
|---|---|---|---|---|
| | best-ranked | any gold | best-ranked | any gold |
| `ok` | 51 | 76 | 46 | 58 |
| `EXCERPT MISS` | 44 | 19 | 41 | 29 |
| `PAGE MISS` | 5 | 5 | 12 | 12 |
| `DOC MISS` | 0 | 0 | 1 | 1 |
| **recall** | **95%** | **95%** | **87%** | **87%** |
| **fidelity** | **54%** | **80%** | **53%** | **67%** |

Recall is identical under both — the disagreement is entirely about
excerpts.

Four things follow.

**Document routing is not the corpus problem.** `DOC MISS` is 1 in 100 —
consistent with the route class scoring 0.927. The corpus penalty is that
the answering page must win a top-10 slot against 3,545 pages instead of
~100, so `PAGE MISS` goes 5 → 12.

**The corpus is no worse at excerpt selection, but only the comparable
measure shows that.** Best-ranked: 54% vs 53%. Any-gold-page: 80% vs 67%,
which reads as a 13-point corpus regression and is an artifact of the slot
count above. A direct check settled it: of 150 (page, question) pairs
returned by both tools, 139 excerpts are byte-identical and the 11 that
differ are not systematically worse — on one, the corpus picked the better
block.

**Snippet-side losses dominate retrieval-side losses under either
measure** — 44 vs 5 or 19 vs 5 on one filing; 41 vs 13 or 29 vs 13 on the
corpus. The ratio is 9:1 or 4:1 single-document, 3:1 or 2:1 corpus. The
conclusion does not depend on which measure you pick, only its size does.

**Which to cite:** best-ranked for comparing settings or tracking a change,
any-gold-page for describing what a caller's payload contains. Reporting
one alone invites the reader to treat it as *the* fidelity, which it is
not.

> **Do not multiply these terms.** `recall × fidelity` does not predict
> answerability under either measure: 0.95 × 0.54 = 0.51 and 0.95 × 0.80 =
> 0.76, against a measured 0.65. The first implies a "reasoning" factor
> above 1.0, the second below it, and neither is a real quantity — the
> payload returns ten excerpts and the answer can come from a page this
> diagnostic never flagged as gold. The stages are not independent and the
> payload is not a single snippet. That the two measures bracket the
> measured value from opposite sides is the clearest sign the
> multiplicative form is fitting, not explaining.

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

Retrieval finds the answering page 95 times in 100 on a single filing, 87
across the corpus, and picks the wrong *document* once in 100. The losses
are downstream of retrieval: 44 excerpt misses against 5 page misses on one
filing, 41 against 13 on the corpus — or 19 against 5 and 29 against 13
under the other fidelity measure; the direction holds either way (§2).

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

The one failure reading does not fix — in either setting.

| setting | search only | after reading |
|---|---|---|
| one filing | 10/100 | 8/100 |
| 24-doc corpus | 17/106 | **19/106** |

On one filing the difference is inside the noise floor, and the composition
is worse than the total suggests: **6 persist** through reading the full
page, and **2 are new**, caused by it — a full page carries more confusable
figures than an excerpt does.

Across the corpus it gets worse rather than better, and the base rate is 6
points higher. The mechanism is different in each setting:

- **within one filing**, every confirmed case is segment-vs-consolidated
  scope confusion — a segment's $10 million provision against the firmwide
  $10.7 billion; Office Commercial against Intelligent Cloud; one theatre
  segment's attendance against the total
- **across the corpus**, the top pages can belong to the *wrong filing*, so
  reading imports a confusable fiscal year rather than resolving anything

So "read more context" is a fix for missing answers, not for wrong ones,
and across a corpus it actively feeds the wrong-answer failure. The
within-document case is documented as a known limitation with two rejected
fixes recorded; the cross-document case is a ranking problem (§5).

"The answer is missing" is recoverable by reading on — 24 of 24 such
questions were on one filing. "A wrong figure looks like the answer" is
not.

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

### Retired: the first corpus answerability arm (n=15)

An earlier run put 15 analyst questions through `pdf_corpus_search` and
reported 7/15 → 9/15 across the excerpt-default fix, concluding that
"retrieval scoring 0.78 leaves fewer than half of real questions
answerable". That claim rested on n=15 against a 13% floor — about 2
questions of judge-only movement, exactly the size of the improvement it
reported — judged with single ballots before majority-of-3 landed, and
graded on the search payload alone.

**Superseded by the n=106 corpus run in §1**, which uses the current judge,
majority-of-3, and both contracts. The direction survives — a corpus that
ranks documents well (doc-NDCG 0.776) still leaves 39% of questions
unanswerable from search alone — but the magnitude was never measurable at
n=15, and "fewer than half" was wrong: it is 61% search-only, 72% after
reading.

The snippet-vs-paragraph comparison it attempted (7/15 → 9/15) has also
been re-measured properly: on all 106 questions, paired, the excerpt
default is worth **23% → 61%** (see §1). The n=15 arm had the right
direction and understated the effect by an order of magnitude.

The excerpt-default fix it was meant to evaluate stands on a different
ground anyway: `pdf_corpus_search` and `pdf_search` disagreed with each
other.

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
- **The corpus follow-up arm is unmeasured.** The n=106 run graded
  search-only and search+read. `--arms followup` (scoped re-searches of
  documents that matched but won no slot) exists and is the mechanism
  multi-document questions actually need, but has not been judged under the
  current setup. The 6 multi-document questions sit mostly at "partial" in
  both measured arms.
- **Mode comparisons were measured search-only.** Whether hybrid's lead over
  semantic and keyword survives the read flow is untested; reading the right
  page plausibly narrows it.

---

## 8. Reproducing

Free and deterministic — run these first:

```bash
uv run python scripts/fetch_financial_corpus.py                # 24 PDFs, SHA256-checked
uv run python scripts/benchmark_corpus_modes.py \
    --data-dir benchmark_data/financial_reports --validate     # label check
uv run python scripts/benchmark_corpus_modes.py \
    --data-dir benchmark_data/financial_reports --single-doc-arm
uv run python scripts/diagnose_excerpt_fidelity.py             # one filing
uv run python scripts/diagnose_excerpt_fidelity.py --corpus    # all 24 documents
```

Billed — each spends `claude -p` calls (~2.1 ballots per question, cached
and resumable; a killed run restarts for free):

```bash
uv run python scripts/eval_single_doc_answerability.py auto --decomposed
uv run python scripts/measure_judge_noise_floor.py             # re-measure the 13% floor
uv run python scripts/eval_financial_answerability.py          # corpus arm (superseded)
```
