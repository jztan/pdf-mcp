# Coherence eval results

Extras config: `{'column_aware': True, 'vertical_aware': True, 'semantic': True}`

| Page | Verdict | vs baseline | Rationale |
|---|---|---|---|
| arxiv-0705.4297-p4-ltr-1col | coherent | same | Dense math prose and theorem/proof blocks read top-to-bottom in correct order; only minor glyph-spacing artifacts (e.g. missing spaces around symbols), not order breakage. |
| arxiv-0706.0028-p4-ltr-1col | coherent | same | Reads as continuous, correctly ordered mathematical prose with intact theorem/definition flow and no interleaving or glyph soup. |
| arxiv-0707.0311-p4-ltr-1col | coherent | same | Mathematical prose with lemma, proof, and problem statements flows in correct top-to-bottom reading order; minor ligature/spacing artifacts don't disrupt order. |
| arxiv-0709.4466-p4-ltr-2col | coherent | same | Figure axis labels and caption lead, then conclusion prose and references read top-to-bottom in correct order with no column interleaving. |
| arxiv-0710.2740-p4-ltr-1col | coherent | same | Prose reads in correct order; only the displayed matrix and equation fragments are inherently messy math typesetting, not reading-order errors. |
| arxiv-1307.7059-p2-ltr-2col | coherent | same | Single-column LTR prose reads in correct order with intact sentences and proper paragraph flow; only minor inline spacing/ligature artifacts. |
| arxiv-1808.03354-p2-ltr-2col | coherent | same | Body prose flows in correct order; only inline math/operator fragments are garbled, not the reading order itself. |
| ibk-72-102-academic-2col | coherent | same | Vertical-rtl Japanese Buddhist text reads as continuous well-ordered prose with intact quotations, citations, and page footer. |
| ibk-72-102-p4-academic-2col | coherent | same | Continuous vertical-rtl Japanese scholarly prose reads in correct order; only a trailing isolated '･' and page marker, no interleaving. |
| iwaki-p1-pure-vert | coherent | same | Vertical-rtl prose reads in correct order; only the page-footer folio lines trail after the body, which is expected. |
| iwaki-p3-magazine-vert | coherent | same | Interview block and feature article both read in correct order as complete prose; the Interview paragraphs are duplicated line-for-line but each is individually well-ordered, not scrambled. |
| iwaki-p6-mixed-orient | coherent | same | Mixed vertical/horizontal Japanese magazine text reads in sensible order; the closing block is a clean continuous paragraph with no glyph soup or column interleaving. |
| nihonbungaku-64-11-p3-academic | coherent | same | Continuous vertical-rtl Japanese prose on 古文/古典 education flows logically across both paragraphs with no interleaving or glyph soup. |
| nihonbungaku-64-11-p4-academic | coherent | same | Vertical-rtl Japanese academic prose reads in correct order with intact section headers, lists, and footnote markers; flows naturally with no interleaving or glyph soup. |
| sodegaura-p3-pure-vert | coherent | same | Body paragraphs read in correct order as complete, well-formed vertical-rtl Japanese prose; only the masthead/sidebar fragments at the top are scrambled, but the substantive content is fully usable. |
| sodegaura-p4-mixed-orient | partial | improved | Interview and directory prose are mostly readable, but a sidebar 'voices' box has two sentences interleaved line-by-line and several headers/captions are duplicated — localized order breaks, not full soup. |
| transformer-p4-ltr-2col | coherent | same | Body text, equation, and footnote read in correct order; figure labels above the caption are expected layout artifacts, not scrambling. |
| yamato-p10-pure-vert | coherent | same | Vertical-rtl Japanese municipal newsletter reads in correct order with intact award lists, dates, and contact info; only scattered mojibake tokens, no order breakage. |
| yamato-p4-mixed-orient | partial | same | Reading order largely follows the page (intro, charts, incident table, then body prose), but the body article runs headings into prose without breaks and has localized glyph reversals/transpositions (e.g. デベシル, 10デシベル０, mojibake furigana ԋủϰ). |
| yamato-p9-pure-vert | coherent | improved | Reads as well-ordered Japanese prose about a city award; localized mojibake glyphs from vertical-script decorations are noise but order is intact and usable. |

## Known-bad / not-yet-fixed

sodegaura-p4-mixed-orient, yamato-p4-mixed-orient
