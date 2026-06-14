# Coherence eval results

Extras config: `{'column_aware': True, 'vertical_aware': False, 'semantic': True}`

| Page | Verdict | vs baseline | Rationale |
|---|---|---|---|
| ibk-72-102-academic-2col | scrambled | new | Body-text columns and footnote/citation blocks are interleaved out of order — sentence threads break and resume in non-adjacent blocks (e.g. the 證空/隆寛 passage continues several blocks earlier), so it can't be read as sequential prose. |
| iwaki-p3-magazine-vert | scrambled | new | Vertical-rtl body text is split into per-glyph lines and interleaved with duplicated horizontal paragraph blocks, breaking column reading order entirely. |
| sodegaura-p4-mixed-orient | scrambled | new | Vertical-text columns are interleaved into glyph-by-glyph soup (e.g. 'い も 変  護 り か ま 活  ら') across most of the page; only a few isolated blocks read cleanly. |
| transformer-p4-ltr-2col | coherent | new | Figure captions, section headings, equations, and prose all read in correct top-to-bottom order with the footnote properly trailing at the bottom. |
| yamato-p10-pure-vert | scrambled | new | Vertical-rtl blocks are individually readable but page-level order interleaves unrelated articles/columns out of sequence, with glyph-soup mojibake fragments throughout. |

## Known-bad / not-yet-fixed

ibk-72-102-academic-2col, iwaki-p3-magazine-vert, sodegaura-p4-mixed-orient, yamato-p10-pure-vert
