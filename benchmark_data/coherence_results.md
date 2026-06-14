# Coherence eval results

Extras config: `{'column_aware': True, 'vertical_aware': False, 'semantic': True}`

| Page | Verdict | vs baseline | Rationale |
|---|---|---|---|
| arxiv-0705.4297-p4-ltr-1col | coherent | same | Math paper reads top-to-bottom in correct order; lemmas, proofs, and theorems flow logically with only minor ligature/spacing artifacts (ﬁ, ω∗) that don't break reading order. |
| arxiv-0706.0028-p4-ltr-1col | coherent | same | Sequential paragraphs, theorems, and definitions read in correct top-to-bottom order with no column interleaving or glyph soup; ligature artifacts (ﬀ, ﬂ) are minor. |
| arxiv-0707.0311-p4-ltr-1col | coherent | same | Single-column math text flows logically through lemma, proof cases, and problem statements with correct order; ligature/spacing artifacts only. |
| arxiv-0709.4466-p4-ltr-2col | coherent | same | Figure axis/legend/caption then body prose, conclusions, acknowledgment, and ordered references all flow in correct ltr reading order with no column interleaving or glyph soup. |
| arxiv-0710.2740-p4-ltr-1col | coherent | same | Single-column technical prose reads in correct order; matrix rows and equations are inherent LaTeX/PDF artifacts, not reading-order scrambling. |
| arxiv-1307.7059-p2-ltr-2col | coherent | new | Single-column prose reads in correct order through the literature review and Motivation section; only minor intra-word spacing artifacts, no order breaks. |
| arxiv-1808.03354-p2-ltr-2col | coherent | new | Single-column prose reads in correct order; only localized inline-math/notation fragments are garbled, not the reading flow itself. |
| chukobungaku-104-52-p7-academic | scrambled | same | Glyph-by-glyph soup with mojibake and isolated fragments scattered across disconnected lines; no recoverable vertical-rtl reading order. |
| ibk-72-102-academic-2col | scrambled | same | Individual lines are clean vertical-rtl Japanese, but the blocks are interleaved out of order (main text, footnotes, and columns mixed), so consecutive blocks don't form continuous prose. |
| ibk-72-102-p4-academic-2col | scrambled | same | Vertical columns are out of reading order — sentence continuations are scattered far apart (e.g. '…第四節参' connects to '照）' many lines later; '引き立' to 'て役として' much further down), so columns are interleaved rather than read right-to-left in sequence. |
| iwaki-p1-pure-vert | scrambled | same | Vertical-rtl columns are interleaved out of order — the headline lines, a fragmented sub-block, and body-text columns appear shuffled rather than in coherent right-to-left reading sequence. |
| iwaki-p3-magazine-vert | scrambled | same | Vertical-rtl columns emitted out of order (article body reads reversed top-to-bottom), interview blocks duplicated, and long stretches split glyph-by-glyph onto separate lines — not usable as prose. |
| iwaki-p6-mixed-orient | scrambled | same | Vertical-text columns are interleaved into glyph-by-glyph soup in the central block (な い 安 ま … / め 思 全 れ …), breaking the body into unreadable order despite a coherent intro and footer. |
| nihonbungaku-64-11-p4-academic | scrambled | same | Column/block order is broken — headings are scattered through body text and adjacent lines don't connect (e.g. 'る教室において' precedes its antecedent '生徒たちにとって'), so it can't be read as prose. |
| sodegaura-p3-pure-vert | scrambled | same | Glyph-by-glyph soup with single characters on their own lines and interleaved FAX/header fragments; no coherent vertical-rtl prose recoverable. |
| sodegaura-p4-mixed-orient | scrambled | same | Large stretches of vertical Japanese are extracted glyph-by-glyph (one char per line) and interleaved across columns, breaking reading order despite a few coherent horizontal blocks. |
| transformer-p4-ltr-2col | coherent | same | Figure caption then body prose in correct order; equations and footnote read sequentially with no column interleaving or glyph soup. |
| yamato-p10-pure-vert | scrambled | same | Multiple vertical columns and separate articles are interleaved line-by-line with glyph-soup noise runs; fragments are readable but reading order is broken, not usable as prose. |
| yamato-p4-mixed-orient | scrambled | same | Two infographic/table pages interleaved with a vertically-set Japanese article whose columns are read across instead of down, producing glyph-soup like '測 １ 来 こ １ / 定 ０ する...' |
| yamato-p9-pure-vert | scrambled | same | Vertical-rtl columns are interleaved out of order (sentence fragments like '定しました。' precede their openings) and the text is shot through with glyph-soup mojibake lines (˔͖ͭΈ໺, ߨධ：), so it doesn't read as ordered prose. |

## Known-bad / not-yet-fixed

chukobungaku-104-52-p7-academic, ibk-72-102-academic-2col, ibk-72-102-p4-academic-2col, iwaki-p1-pure-vert, iwaki-p3-magazine-vert, iwaki-p6-mixed-orient, nihonbungaku-64-11-p4-academic, sodegaura-p3-pure-vert, sodegaura-p4-mixed-orient, yamato-p10-pure-vert, yamato-p4-mixed-orient, yamato-p9-pure-vert
