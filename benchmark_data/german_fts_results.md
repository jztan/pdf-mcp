# German FTS mirror (`[fts] language = "de"`) — before/after

Recall@10 and MRR of `cache.search_fts` on a small synthetic German corpus,
comparing the shipped default (`porter unicode61` — an English stemmer) against
the new opt-in German-stemmed mirror index (`pdf_search_fts_de`, Snowball
German via the pure-Python `snowballstemmer` package).

- **Before** — every document goes through `pdf_search_fts` regardless of
  language. Porter's English suffix rules do nothing useful for German
  inflection, and don't touch the umlaut/ß ↔ ASCII-transliteration spelling
  variants either (`Kündigung`/`Kuendigung`/`kundigung`, `Straße`/`Strasse`).
- **After** — with `[fts] language = "de"` set, queries route to
  `pdf_search_fts_de` instead, both sides (index and query) run through
  `_german_normalize` (lowercase, tokenize, Snowball-stem).

Corpus: 14 short synthetic pages written directly into
`scripts/benchmark_german_fts.py` (no external document), covering
inflection, both spelling conventions, numbers/statute citations, a
multi-word query, the OR fallback, and topic distractors so recall isn't
trivially 1.0.
Ground truth is hand-authored (small enough to eyeball). "Recall" here is
binary hit-rate@10 (1.0 if *any* relevant page is in the top 10, else
0.0), not the fraction of relevant pages retrieved — several queries have
more than one relevant page (e.g. `kündigen`'s `[0, 1, 2]`), and a 1.00
recall there means at least one was found, not all three (see the
Snowball-stemmer-gaps caveat below for why `kündigen` in fact only
recovers 2 of its 3 relevant pages).
Reproduce: `python scripts/benchmark_german_fts.py`.

## Aggregate (14 queries)

| metric | before (porter) | after (de) | delta |
| --- | --- | --- | --- |
| **mean recall@10** | 0.643 | **0.929** | **+0.286** |
| **MRR** | 0.643 | **0.929** | **+0.286** |
| queries with a hit | 9 / 14 | **13 / 14** | +4 |

## By query

| query | relevant pages | before recall | after recall |
| --- | --- | ---: | ---: |
| kündigen | [0, 1, 2] | 0.00 | 1.00 |
| Kündigung | [0, 1, 2] | 1.00 | 1.00 |
| gekündigt | [0, 1, 2] | 0.00 | 0.00 |
| Kuendigung | [0, 1, 2] | 0.00 | 1.00 |
| Straße | [3] | 1.00 | 1.00 |
| Strasse | [3] | 0.00 | 1.00 |
| Fussball | [4] | 0.00 | 1.00 |
| Urlaubsanspruch | [5] | 1.00 | 1.00 |
| Arbeitsgericht | [6] | 1.00 | 1.00 |
| 626 | [8] | 1.00 | 1.00 |
| § 626 BGB | [8] | 1.00 | 1.00 |
| 2023 | [8] | 1.00 | 1.00 |
| befristeter Arbeitsvertrag | [10] | 1.00 | 1.00 |
| § 622 BGB | [13] | 1.00 | 1.00 |

## Findings

- **Inflection and spelling variants that porter always missed now hit.**
  `kündigen` (infinitive query against pages 0 and 2, which use
  `Kündigung`/`Kündigungen` — sharing its `kundig` stem; NOT page 1's
  `kündigte`, which stems to `kundigt` and stays a stemmer gap, see below),
  both ASCII-transliteration spellings (`Kuendigung`, `Strasse`), and the
  standalone-word case (`Fussball`) all go from 0.00 to 1.00 recall.
- **Numbers and statute citations survive stemming — and stay discriminative.**
  `626`, `§ 626 BGB`, and `2023` all hit their citation page under "de" mode
  without also matching a distractor page that only mentions "BGB" —
  regression coverage for a digit-dropping tokenizer bug found in review
  (`_GERMAN_TOKEN_RE` used to treat digits as separators, so `"§ 626 BGB"`
  degraded to just `"bgb"` and matched every BGB-mentioning page).
- **Multi-word queries try AND first, then fall back to OR — same as the
  default keyword path.** `befristeter Arbeitsvertrag` still matches only
  the page containing both stems (the returned page list is exactly
  `[10]`), not the distractor page sharing just one of the two words: AND
  is tried first and is precise when it hits. `§ 622 BGB` is answered only
  by the OR retry (`_fts5_or_fallback_de`) — no synthetic page contains
  both stems ("622" and "bgb"), so the AND form matches nothing, and only
  the fallback finds the true citation page, ranked above a distractor
  that merely says "BGB" without this number (verified directly: the
  citation page comes back first). Regression coverage for the maintainer
  reporting exactly this query against a real BGB PDF returning nothing
  under "de" mode.
- **Exact-form queries are unaffected either way.** `Kündigung`,
  `Straße`, `Urlaubsanspruch`, `Arbeitsgericht`, and the numeric queries
  already worked under porter (the query happens to literally match a page
  word) and still work under "de" — the option adds coverage, it doesn't
  regress the cases that already worked.

## Honest caveats

- **The German Snowball stemmer itself has gaps.** `gekündigt` (past
  participle, `ge-` prefix) stems to `gekundigt`, and `kündigte` (3rd
  person past) stems to `kundigt` — neither shares the `kundig` stem that
  `kündigen`/`Kündigung`/`Kündigungen` do — verified directly against the
  `snowballstemmer` package, not an integration bug. `get_fts_page_counts`
  demonstrates this precisely: `_cache.get_fts_page_counts(path,
  "kündigen")` on a page containing all four forms counts only 2 (the
  `kundig`-stem occurrences), not 4 — see
  `test_get_fts_page_counts_counts_stem_matches` in
  `tests/test_cache_german_fts.py`. This is a limitation of the stemming
  algorithm itself, not of how it's
  wired into `cache.py`; a query using the exact inflected form the page
  uses always works regardless.
- **No compound-word splitting.** `Kündigungsschutzklage` and
  `Kündigungsschutz` stem to different tokens — real German legal/technical
  vocabulary is compound-heavy, and this is explicitly out of scope for this
  change (would need a dictionary or trained-model dependency).
- **Small, synthetic, hand-authored corpus.** Thirteen queries over twelve
  sentences is enough to demonstrate the mechanism honestly, not a
  statistically powered benchmark. It intentionally avoids depending on a
  large or private German corpus so it ships in the repo and runs in CI
  without a download.

## Query latency (real 100+ page document)

Median per-query `search_fts` time on `googl-fy2023.pdf` (111 pages, a real
10-K), 3 queries × 5 repeats. The document is
English — irrelevant for a timing measurement, since the "de" search path
re-tokenizes and stems whatever text is on the page regardless of its actual
language. Reproduce: `python scripts/benchmark_german_fts.py --latency`.

| path | median ms/query |
| --- | ---: |
| porter (default) | ~10 |
| **de, before this fix** (re-stem every page on every query) | **~980** |
| **de, after this fix** (copy pre-stemmed rows from `pdf_search_fts_de`) | **~39** |

Environment: CPython 3.12.2, SQLite 3.45.1, x86_64 (Linux); fastembed 0.8.0,
numpy 2.4.4, onnxruntime 1.24.4, pdf-mcp 3.2.0, pypdfium2 5.13.0.

**Before this fix**, `de` mode re-stemmed the entire document (`page_text`)
into a fresh temp FTS table on every single query — found in review as a
~1s/query cost on a 121-page 10-K, reproduced here as ~980 ms/query on a
111-page one (varies run to run; a few individual runs landed anywhere from
~890 to ~1015 ms). **After the fix**, the query path copies already-stemmed
rows straight from the shared `pdf_search_fts_de` mirror (maintained
incrementally by every writer, synced on cache open, and self-healed per
query if it's ever found to be missing or partial for the document being
searched — see cache.py's `_sync_de_tables` and `_build_temp_page_fts`),
cutting median query time to ~39 ms — about 25x faster, and the same order
of magnitude as the porter default (the remaining gap is per-result excerpt
construction, which re-tokenizes only the matched page, not the whole
document). The OR fallback added in review round 2 (see "By query" above)
does not change these numbers: it only runs when the AND form already
matched nothing, and retries against the same connection-local temp index
built for the AND attempt — no extra stemming, one extra `MATCH` against
an index already in memory.

**Not measured here: one-time startup cost.** The maintainer reported the
open-time sync (`_sync_de_tables`, not the per-query path this section
measures) taking about 28 s on a 4,000-page cache the first time `[fts]
language = "de"` is turned on, blocking server startup for that one run;
later starts only re-sync documents whose page count changed since. See
[docs/configuration.md](../docs/configuration.md).
