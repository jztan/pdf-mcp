# Coherence eval results

Extras config: `{'column_aware': True, 'vertical_aware': False, 'semantic': True}`

| Page | Verdict | vs baseline | Rationale |
|---|---|---|---|
| ibk-72-102-academic-2col | scrambled | same | Each per-character block is internally readable, but consecutive blocks jump between interleaved main-text and footnote/column threads (e.g. block ending 'である。' followed by 'て、先行研究…'), so top-to-bottom reading order is broken across the page. |
| iwaki-p3-magazine-vert | scrambled | same | Vertical-rtl columns are interleaved—horizontal Interview-box prose (食に関わる...) duplicated and spliced between glyph-by-glyph vertical column fragments, breaking reading order. |
| sodegaura-p4-mixed-orient | scrambled | same | Vertical-text columns are interleaved glyph-by-glyph across the page, producing single-character soup; only a few isolated blocks read coherently. |
| transformer-p4-ltr-2col | coherent | same | Body prose flows in correct reading order; only the two figure-label lines and stacked equation/footnote fragments are minor artifacts. |
| yamato-p10-pure-vert | scrambled | same | Multi-column vertical-rtl newspaper page with interleaved column blocks that don't form continuous prose, plus heavy mojibake from decorative headings — articles can't be read in order. |

## Known-bad / not-yet-fixed

ibk-72-102-academic-2col, iwaki-p3-magazine-vert, sodegaura-p4-mixed-orient, yamato-p10-pure-vert
