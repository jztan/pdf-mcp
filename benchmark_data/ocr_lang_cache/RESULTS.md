# ocr_lang cache key: measurements for issue #27

Three measurements taken 2026-08-17, before designing the fix for
[#27](https://github.com/jztan/pdf-mcp/issues/27) (`ocr_lang` is stored and
compared but is not part of `page_text`'s primary key, so a page holds one row
and alternating language strings evict each other).

They answer three separate questions: what the bug costs when it happens, how
often it is reachable, and what the fix's migration costs.

| script | question | billed? |
|---|---|---|
| `scripts/measure_ocr_lang_thrash.py` | what does the thrash cost? | no |
| `scripts/probe_ocr_lang_variance.py` | do callers actually vary the string? | **yes** |

The variance probe was run twice: once before the fix
(`variance_results.json`) and once against the reworded tool description
(`variance_results_after_docs.json`).
| `scripts/measure_page_text_migration.py` | what does the migration cost? | no |

## 1. Cost of the thrash

10-page bilingual synthetic scan (Cyrillic and Latin paragraphs alternating),
6 tool calls per sequence, cold isolated cache each time.

| sequence | wall clock | page-misses | page-hits |
|---|---|---|---|
| baseline (`rus+eng` every call) | 2.83s | 10 | 50 |
| case-thrash (`rus+eng` / `RUS+ENG`) | 11.47s | 60 | 0 |
| order-thrash (`rus+eng` / `eng+rus`) | 11.68s | 60 | 0 |

4x wall clock, and the cache contributes **nothing**: every page of every call
re-OCRs. The zero-hit result is structural and does not depend on the sample.

The absolute seconds are a **floor**, not a typical cost. This synthetic page
OCRs quickly; a real 300dpi scan is slower per page, which makes the penalty
larger. Parallel OCR (gate 2, cap 8) also compresses the wall clock on
multi-page calls, so per-call latency understates per-page waste.

`strip().lower()` on the key fixes case-thrash only. Order-thrash needs the
wider `(file_path, page_num, ocr_lang)` primary key, because @deepdmk's test rig
established that the two orderings produce genuinely different text and
therefore cannot be collapsed.

## 2. Do callers actually vary the string?

The justification for the wider key is that this server's main caller is a
model, and a model regenerates its tool arguments rather than reusing a fixed
string. Measured directly: 20 independent `claude -p` sessions
(`claude-opus-4-8`), one bilingual scan, one intent, varying only how the user's
sentence orders the two languages.

| arm (user's phrasing) | `ocr_lang` emitted |
|---|---|
| "Russian and English" | `rus+eng` 10/10 |
| "English and Russian" | `eng+rus` 8/10, `rus+eng` 2/10 |

Two findings, the second stronger than the first:

1. **Phrasing determines the cache key.** The model mirrors the language order
   in the user's request. Two people describing the same scan differently get
   different cache keys with nobody doing anything wrong.
2. **The model is not self-consistent within one phrasing.** The `en_first` arm
   split 8/2 across identical prompts in independent sessions. The thrash is
   reachable without any phrasing variation at all.

Limits: one model, one language pair, n=10 per arm. The 8/2 split has a wide
interval and should be read as "varies within one phrasing", not as a rate.
Trials are single-call, so this measures the cross-session case (two
conversations, or one agent resuming after compaction). A model that OCRs the
same page twice inside one session can see its earlier call and would likely
copy it.

Judge/caller config is recorded in `variance_results.json`. Per CLAUDE.md the
run used `--setting-sources ''` and `--strict-mcp-config` with only pdf-mcp
loaded, and no `--system-prompt`.

## 2b. Does the tool description change what callers emit?

The fix ships a reworded `ocr_lang` parameter description saying order is
significant and asking callers to keep the string stable. Whether documentation
changes model behaviour is testable with the same 20-session protocol, so it was
tested rather than assumed (`variance_results_after_docs.json`).

| arm (user's phrasing) | before the doc change | after |
|---|---|---|
| "Russian and English" | `rus+eng` 10/10 | `rus+eng` 10/10 |
| "English and Russian" | `eng+rus` 8/10, `rus+eng` 2/10 | `eng+rus` 10/10 |
| distinct strings overall | 2 | 2 |

Within-phrasing variance disappeared: the 8/2 split became 10/10. Cross-phrasing
mirroring did not move. The model reads "order is significant", takes it
seriously, and concludes the significant order is the one the user used, which
is a reasonable reading of the sentence.

**Treat this as weak evidence.** Going 8/2 to 10/10 is two ballots moving on
n=10, no noise floor has been measured for this probe, and the before condition
was not re-run, so any drift between the two runs is confounded with the doc
change. What it does rule out is the failure mode worth worrying about: telling
a model that an arbitrary choice matters could have made it deliberate about the
choice and produced *more* variance. That did not happen.

The practical consequence is that cross-phrasing variance, the kind that
actually creates a second cache row, survives. A caller who says "Russian and
English" today and "English and Russian" next month still pays two OCR passes,
and closing that gap would require normalizing order, which is the correctness
bug this investigation ruled out. The 20-page-miss figure in the after-the-fix
table below is therefore the real-world number, not a way station.

## 3. Migration cost

Widening a primary key in SQLite means create-new, copy, drop, rename against
every existing user's `cache.db`. Synthetic cache, 20% OCR rows, plus a
`page_embeddings` row per page.

| rows | migrate | VACUUM | before -> unvacuumed -> after |
|---|---|---|---|
| 1,000 | 0.004s | 0.009s | 4.3MB -> 6.4MB -> 4.3MB |
| 5,000 | 0.019s | 0.048s | 21.3MB -> 31.8MB -> 21.2MB |
| 20,000 | 0.078s | 0.287s | 85.0MB -> 127.1MB -> 84.8MB |
| 50,000 | 0.178s | 0.728s | 212.6MB -> 317.9MB -> 211.9MB |

Under a second end to end at 50,000 cached pages, which is larger than the
100-doc `CORPUS_MAX_FILES` ceiling normally produces. Not a performance risk.

**Drop-and-rename inflates the file by ~50% and it stays inflated.** The middle
column of sizes above is what a user would be left with if the migration skipped
the `VACUUM`. `VACUUM` reclaims it fully for well under a second, so it is part
of the migration rather than an optional extra. This would have shipped
unnoticed if only the migration itself had been timed.

The script asserts row count, the `''` sentinel backfill, and text integrity, so
a silent data-loss bug fails loudly instead of printing a fast time.

## After the fix

Same harness, same corpus, re-run once the wider primary key and the
language-scoped reads landed (`thrash_results_after.json`):

| sequence | wall clock | page-misses | page-hits |
|---|---|---|---|
| baseline | 2.77s | 10 | 50 |
| case-thrash | 2.71s | 10 | 50 |
| order-thrash | 4.53s | 20 | 40 |

Case variants now share a cache row, so `rus+eng` / `RUS+ENG` costs exactly
what a single spelling costs. Distinct orderings each pay for one OCR pass and
are free thereafter, which is the intended behaviour rather than a shortfall:
they are genuinely different requests to Tesseract.

`order-thrash` landing on 20 rather than 10 is the load-bearing check. 10 would
mean the two orderings had been collapsed into one row, which would be the
correctness bug this whole investigation ruled out.

## Verdict

The wider primary key is justified: the cost is real and structural, the
trigger is ordinary caller behaviour rather than misuse, and the migration is
cheap. The design must include the `VACUUM`.

Documentation is not a substitute for it. The reworded parameter description
removed within-phrasing variance in a 20-session probe but left cross-phrasing
mirroring intact, and cross-phrasing variance is what creates a second cached
row. Two distinct `ocr_lang` strings for one document remain reachable from
ordinary phrasing differences, which is exactly what the cache now handles
correctly instead of thrashing on.

## Reproducing

Measurements 1 and 2 need a tessdata directory holding `eng` and `rus`
(the dev machine ships `eng` only):

```bash
mkdir -p /tmp/td && cp /opt/homebrew/share/tessdata/eng.traineddata /tmp/td/
curl -sLo /tmp/td/rus.traineddata \
  https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/rus.traineddata

TESSDATA_PREFIX=/tmp/td python scripts/measure_ocr_lang_thrash.py --pages 10 --calls 6
python scripts/measure_page_text_migration.py
TESSDATA_PREFIX=/tmp/td python scripts/probe_ocr_lang_variance.py --trials 10 \
  --out benchmark_data/ocr_lang_cache/variance_results.json   # billed, 20 sessions
```

@deepdmk's own order-invariance rig (which established that language order
changes Tesseract's output, killing the cheaper "sort the key" fix) is kept
locally at `docs_internal/ocr-order-testrig/` pending a decision on whether it
belongs in the repo. It needs nine language packs plus Chrome, so it cannot run
in CI.
