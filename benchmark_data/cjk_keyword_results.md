# CJK keyword-search fix — before/after

Recall@50 of `pdf_search(..., mode="keyword")` on unspaced Japanese text, before
and after the char-split FTS5 fix (`feature/cjk-fts5-keyword-fix`, 2026-06-21).

- **Before** — the `porter unicode61` index queried with `_escape_fts5_query`
  (whole-token match). An unspaced CJK run becomes a single token, so a term only
  matched when it happened to sit as a whitespace-/newline-delimited token.
- **After** — CJK-containing queries route to a parallel char-split index
  (`pdf_search_fts_cjk`, `tokenize='unicode61'`, one codepoint per token) queried
  with phrase semantics; English/Latin queries stay on the untouched porter index.

Ground truth is not hand-authored: for each query, the relevant pages are those
whose extracted text (same `extract_text_from_page` the cache indexes) literally
contains the query substring. Corpus: local `docs_internal/sample_pdfs/vertical-jp`
(yamato + sodegaura municipal bulletins). 13 queries, 88 relevant pages total.

Reproduce: `python scripts/benchmark_cjk_keyword.py` (after-only gate);
the before/after delta below was measured by additionally querying the porter
table directly under the same resolved file path.

## Aggregate (13 queries, 88 relevant pages)

| metric | before (porter) | after (char-split) | delta |
| --- | --- | --- | --- |
| **mean recall@50** | 0.252 | **1.000** | **+0.748** |
| relevant pages found | 23 / 88 | **88 / 88** | +65 |
| queries with ≥1 hit | 9 / 13 | **13 / 13** | +4 |
| `厚木基地` (headline) | 0 / 3 | **3 / 3** | the 0→3 case |

**Mean CJK keyword recall went from 25% → 100% (+0.75 absolute, ~4×), recovering
65 of 88 previously-unreachable relevant pages.**

## By query

| query | pdf | relevant | before recall | after recall |
| --- | --- | ---: | ---: | ---: |
| 厚木基地 | yamato | 3 | 0.00 | 1.00 |
| 終活 | yamato | 3 | 0.33 | 1.00 |
| 令和6年度 | yamato | 6 | 0.33 | 1.00 |
| 10人 | yamato | 5 | 0.40 | 1.00 |
| 3月 | yamato | 5 | 0.40 | 1.00 |
| 健康 | yamato | 14 | 0.14 | 1.00 |
| 情報 | yamato | 7 | 0.14 | 1.00 |
| 子ども | yamato | 3 | 0.00 | 1.00 |
| 図書館 | yamato | 1 | 0.00 | 1.00 |
| 令和 | sodegaura | 12 | 0.00 | 1.00 |
| 募集 | sodegaura | 8 | 0.62 | 1.00 |
| イベント | sodegaura | 8 | 0.75 | 1.00 |
| 申込 | sodegaura | 13 | 0.15 | 1.00 |

## Findings

- **Categorical recall fix.** Every query reaches full recall after; the
  headline `厚木基地` (always embedded as `厚木基地をめぐる…`) goes 0 → 3 pages.
- **Before was partial, not zero.** The old path matched terms that appeared as
  standalone tokens (`イベント` 0.75, `募集` 0.62, `終活` 0.33 as a heading) but
  missed embedded occurrences — hence 25% mean recall, the honest baseline.
- **English path unchanged.** Latin queries never touch the CJK index; the RRF v2
  English-regression gate confirms NDCG@10 stays 0.642 / 0.656 / 0.777.

## Honest caveats

- **after = 1.00 is a structural ceiling, not a tuned result.** The index splits
  identically to the query, so any page with the literal contiguous substring is
  guaranteed to match. The meaningful half of the delta is the *before = 0.25* —
  the measure of what the old tokenizer missed.
- **Recall, not precision/ranking.** This measures the breakage that was fixed
  (reachability). It is exact-substring retrieval — no stemming/segmentation, and
  it can over-match on substring overlap (e.g. `京都` inside `東京都`). BM25 over
  single-char tokens is weak ordering when a term hits many pages.
- **Japanese only.** Chinese/Korean work by the same script-agnostic mechanism
  but have no graded corpus yet — untested for quality.
