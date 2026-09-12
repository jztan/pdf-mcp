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

Corpus: 8 short synthetic pages written directly into
`scripts/benchmark_german_fts.py` (no external document), covering
inflection, both spelling conventions, and topic distractors so recall isn't
trivially 1.0. Ground truth is hand-authored (small enough to eyeball).
Reproduce: `python scripts/benchmark_german_fts.py`.

## Aggregate (9 queries)

| metric | before (porter) | after (de) | delta |
| --- | --- | --- | --- |
| **mean recall@10** | 0.444 | **0.889** | **+0.444** |
| **MRR** | 0.444 | **0.889** | **+0.444** |
| queries with a hit | 4 / 9 | **8 / 9** | +4 |

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

## Findings

- **Inflection and spelling variants that porter always missed now hit.**
  `kündigen` (infinitive query against a page using `Kündigung`/`kündigte`/
  `Kündigungen`), both ASCII-transliteration spellings (`Kuendigung`,
  `Strasse`), and the standalone-word case (`Fussball`) all go from 0.00 to
  1.00 recall.
- **Exact-form queries are unaffected either way.** `Kündigung`,
  `Straße`, `Urlaubsanspruch`, `Arbeitsgericht` already worked under porter
  (the query happens to literally match a page word) and still work under
  "de" — the option adds coverage, it doesn't regress the cases that already
  worked.

## Honest caveats

- **The German Snowball stemmer itself has gaps.** `gekündigt` (past
  participle, `ge-` prefix) stems to `gekundigt`, not the `kundig` stem
  shared by `kündigen`/`Kündigung`/`Kündigungen`/`kündigte` — verified
  directly against the `snowballstemmer` package, not an integration bug.
  This is a limitation of the stemming algorithm itself, not of how it's
  wired into `cache.py`; a query using the exact inflected form the page
  uses always works regardless.
- **No compound-word splitting.** `Kündigungsschutzklage` and
  `Kündigungsschutz` stem to different tokens — real German legal/technical
  vocabulary is compound-heavy, and this is explicitly out of scope for this
  change (would need a dictionary or trained-model dependency).
- **Small, synthetic, hand-authored corpus.** Nine queries over eight
  sentences is enough to demonstrate the mechanism honestly, not a
  statistically powered benchmark. It intentionally avoids depending on a
  large or private German corpus so it ships in the repo and runs in CI
  without a download.
