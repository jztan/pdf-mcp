# Coherence eval results

Extras config: `{'column_aware': True, 'vertical_aware': False, 'semantic': True}`

| Page | Verdict | vs baseline | Rationale |
|---|---|---|---|
| ibk-72-102-academic-2col | scrambled | same | Vertical-rtl column blocks are interleaved out of order — adjacent fragments don't continue (e.g. 'が、善導の「一一願言」である。' then 'て、先行研究…'), mixing main text and footnotes so sentences don't follow in reading order. |
| iwaki-p3-magazine-vert | scrambled | same | Much of the main article is split glyph-by-glyph (one character per line) and interview blocks are duplicated and interleaved with the vertical columns — not usable as ordered prose. |
| sodegaura-p4-mixed-orient | scrambled | same | Vertical Japanese columns extracted glyph-by-glyph (one character per line) and interleaved across columns, so the prose cannot be read in order despite a few intact horizontal blocks. |
| transformer-p4-ltr-2col | coherent | same | Figure caption, body prose, equation, and footnote all read in correct top-to-bottom order with intact sentences. |
| yamato-p10-pure-vert | scrambled | same | Multi-column vertical text is interleaved across unrelated articles with character-by-character fragmentation and pervasive glyph-soup noise (ୈ, ճେ࿨ࢢத); not usable as prose. |

## Known-bad / not-yet-fixed

ibk-72-102-academic-2col, iwaki-p3-magazine-vert, sodegaura-p4-mixed-orient, yamato-p10-pure-vert
