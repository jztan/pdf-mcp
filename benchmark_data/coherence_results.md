# Coherence eval results

Extras config: `{'column_aware': True, 'vertical_aware': True, 'semantic': True}`

| Page | Verdict | vs baseline | Rationale |
|---|---|---|---|
| arxiv-0705.4297-p4-ltr-1col | coherent | same | Single-column math paper reads in correct order; lemmas, proofs, and theorems flow sequentially with only minor missing-space ligature artifacts. |
| arxiv-0706.0028-p4-ltr-1col | coherent | same | Single-column academic prose reads in correct order; theorems, definitions, and formulas flow naturally with no interleaving or glyph scrambling. |
| arxiv-0707.0311-p4-ltr-1col | coherent | same | Mathematical prose (lemma, proof, problems) reads in correct top-to-bottom order with intact sentences and logical flow; symbol spacing quirks don't break readability. |
| arxiv-0709.4466-p4-ltr-2col | coherent | same | Figure axis labels and caption lead, then conclusion prose and references flow in correct top-to-bottom single-column reading order. |
| arxiv-0710.2740-p4-ltr-1col | coherent | same | Single-column academic prose reads in correct order; only the matrix block and a few inline-math fragments are slightly jumbled, which is expected and doesn't break readability. |
| arxiv-1307.7059-p2-ltr-2col | coherent | same | Single-column prose reads in correct top-to-bottom order with intact sentences and continuous argument flow; only minor intra-word spacing artifacts. |
| arxiv-1808.03354-p2-ltr-2col | coherent | same | Body prose reads in correct order throughout; only inline math/notation fragments around equations are garbled, which is expected and localized. |
| chukobungaku-104-52-p7-academic | scrambled | same | Glyph-by-glyph soup of punctuation and mojibake with no readable vertical-RTL prose; reading order is broken. |
| ibk-72-102-academic-2col | coherent | improved | Dense vertical-RTL Japanese Buddhist-studies prose reads in correct order with citations and quotations intact; page number trails naturally at the end. |
| ibk-72-102-p4-academic-2col | coherent | improved | Continuous vertical-RTL prose reads in correct order; heading line folded mid-text but content is well-ordered and usable. |
| iwaki-p1-pure-vert | coherent | improved | Vertical-RTL prose reads in correct order with intact sentences; only a minor split of 'バトン/ン' at a column break. |
| iwaki-p3-magazine-vert | coherent | improved | Magazine interview text reads in correct order; furigana/interview lines duplicated and one feature block runs as a wall, but content is sequential and usable. |
| iwaki-p6-mixed-orient | coherent | improved | Mixed-direction magazine spread reads in sensible order: title block, tagline, then the long body essay flow as complete readable Japanese prose. |
| nihonbungaku-64-11-p4-academic | coherent | improved | Vertical-RTL academic prose reads in correct order with intact section headers, enumerated constraints, and footnote markers in place. |
| sodegaura-p3-pure-vert | partial | improved | Body prose blocks read coherently in vertical-rtl order, but the top header zone is fragmented into stray single-character/duplicated label lines (mojibake-like glyph splitting). |
| sodegaura-p4-mixed-orient | scrambled | same | Two coherent interview passages bookend a middle section where multi-column support-center listings (phone numbers, addresses, district names, area lists) are interleaved glyph-by-region into unreadable soup. |
| transformer-p4-ltr-2col | coherent | same | Text flows in correct reading order with intact prose, equations, and footnote at the bottom; figure labels at top are minor. |
| yamato-p10-pure-vert | partial | improved | Vertical-RTL article blocks read coherently in order, but each block is interrupted by mojibake (encoding-broken headings) and stray leading number runs. |
| yamato-p4-mixed-orient | partial | improved | Reading order is broadly correct (header, chart labels, table, then body prose) but the chart/table region scrambles numeric labels and a few inline runs invert characters (デベシル, 10デシベル０), plus a short mojibake stretch. |
| yamato-p9-pure-vert | scrambled | same | Vertical-RTL reorder failed: award-title fragments are split and interleaved with long mojibake runs (˔͖ͭΈ໺Ӻ…) between readable judge comments, so columns are broken up rather than flowing in order. |

## Known-bad / not-yet-fixed

chukobungaku-104-52-p7-academic, sodegaura-p3-pure-vert, sodegaura-p4-mixed-orient, yamato-p10-pure-vert, yamato-p4-mixed-orient, yamato-p9-pure-vert
