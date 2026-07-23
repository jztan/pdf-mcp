# Coherence eval results

Extras config: `{'column_aware': True, 'vertical_aware': True, 'semantic': True}`

| Page | Verdict | vs baseline | Rationale |
|---|---|---|---|
| arxiv-0705.4297-p4-ltr-1col | coherent | same | Mathematical prose reads in correct sequential order (lemmas, proofs, theorems flow logically); only minor typographic artifacts from union/subscript symbols, no column interleaving. |
| arxiv-0706.0028-p4-ltr-1col | coherent | same | Mathematical prose flows in correct logical order (theorem, definitions, equations) with no interleaving or displacement; only cosmetic ligature artifacts. |
| arxiv-0707.0311-p4-ltr-1col | coherent | same | Mathematical prose flows in correct order (lemma → proof → problems 3–4) with only minor ligature/spacing artifacts, no interleaving or scrambling. |
| arxiv-0709.4466-p4-ltr-2col | coherent | same | Body prose, conclusions, acknowledgment, and references all read in correct order; only figure axis-label fragments (10^-k ticks, stray 'BER') appear as expected chart noise, not an ordering fault. |
| arxiv-0710.2740-p4-ltr-1col | partial | same | Body prose reads in correct order throughout, but the transition matrix and equations (5)-(7) have locally interleaved fragments (e.g. 'p11(1 −αx p12(1 −αx' with subscripts split onto later lines, 'Q =' displaced after the matrix). |
| arxiv-1307.7059-p2-ltr-2col | coherent | same | Continuous well-ordered academic prose with an intact section break (III. MOTIVATION); sentences flow logically with no interleaving or glyph soup. |
| arxiv-1808.03354-p2-ltr-2col | coherent | same | Prose flows in correct order across columns and the page break (sentence continues cleanly past the stray page number '2'); only inline math/equation fragments are jumbled, which is typical extraction noise, not reading-order failure. |
| ibk-72-102-academic-2col | partial | same | Prose reads in correct vertical-rtl order throughout, but a running header/title block (法然門下における善導『観経疏』「一一願言」をめぐる議論と伝…承) is interleaved mid-sentence, splitting 往生し得ない across it — a single localized insertion, not systemic scrambling. |
| ibk-72-102-p4-academic-2col | partial | same | Body prose reads in correct order, but a running header (article title '法然門下における…議論と伝承（中村）') is interleaved mid-sentence, splitting 解/釈 and 伝/承 across the page break. |
| iwaki-p1-pure-vert | partial | same | Body prose reads coherently in order, but the heading めぐみをつなぐバトン is split with ン stranded on its own line (localized fragment break); page footers append harmlessly. |
| iwaki-p3-magazine-vert | partial | same | Main body prose reads coherently in order, but the interview block has every line duplicated (each sentence fragment emitted twice), a localized extraction artifact rather than column interleaving. |
| iwaki-p6-mixed-orient | partial | same | Main body reads as continuous well-ordered prose, but numerals ('15') are split out of their sentences (東日本大震災から年の節目), and headline/caption fragments and page footers are clumped out of place at the top. |
| nihonbungaku-64-11-p3-academic | coherent | same | Japanese prose reads as continuous, well-ordered argument about 古文 education with complete sentences and logical flow; only stray page number '13' at top. |
| nihonbungaku-64-11-p4-academic | coherent | same | Prose flows in correct order throughout; only minor inline footnote-marker artifacts (⑹⑺⑻⑼ splitting words like ところが) which don't disrupt readability. |
| sodegaura-p3-pure-vert | partial | same | Body prose reads in correct order across all sections, but header digits are scrambled into glyph-by-glyph soup (1 F / A 6 2 X...), leaving 「月日は」 and ☎（）（） stripped of their numbers, plus duplicated title lines. |
| sodegaura-p4-mixed-orient | partial | same | Main interview prose reads in order but is split around an interleaved block; the resident-voices section has two testimonials line-interleaved (話をしな…がら食事 split by another sentence) and one reversed label (線鴨川葉千), while the support-center listings remain usable. |
| transformer-p4-ltr-2col | coherent | same | Body prose flows in correct order with only minor equation/superscript line-break artifacts typical of math extraction; sections 3.2.1–3.2.2 read continuously. |
| yamato-p10-pure-vert | partial | same | Body prose of each article reads in order, but a heading is split and interleaved mid-sentence (「人権作文・ポスタ」…「ーの優秀作品を表彰」), stray number tokens are dumped at start/end, and decorative glyphs are mojibake. |
| yamato-p4-mixed-orient | partial | same | Body prose reads in order, but a chart-legend fragment is interleaved mid-sentence (測...定対象 split by うち１００デベシル以上騒音測定回数), digits garbled (１０デシベル０), and two headings are mojibake; chart/table regions are number soup. |
| yamato-p9-pure-vert | partial | same | Body paragraphs read coherently, but the large headline (第◯回大和市街づくり賞が決定〜表彰式とパネル展も開催します) is split into fragments interleaved between blocks, and several sub-headings are mojibake (˔͖ͭΈӺớΓỜ). |

## Known-bad / not-yet-fixed

arxiv-0710.2740-p4-ltr-1col, ibk-72-102-academic-2col, ibk-72-102-p4-academic-2col, iwaki-p1-pure-vert, iwaki-p3-magazine-vert, iwaki-p6-mixed-orient, sodegaura-p3-pure-vert, sodegaura-p4-mixed-orient, yamato-p10-pure-vert, yamato-p4-mixed-orient, yamato-p9-pure-vert
