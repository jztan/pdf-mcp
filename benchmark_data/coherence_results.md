# Coherence eval results

Extras config: `{'column_aware': True, 'vertical_aware': True, 'semantic': True}`

| Page | Verdict | vs baseline | Rationale |
|---|---|---|---|
| arxiv-0705.4297-p4-ltr-1col | coherent | same | Lemmas, theorems, and proofs flow in correct sequential order with intact prose; only minor inline-math artifacts (e.g. union symbol 'S' split from its subscript), no column interleaving. |
| arxiv-0706.0028-p4-ltr-1col | coherent | same | Continuous mathematical prose with theorems and definitions flowing in correct logical order; line breaks and ligatures intact, no interleaving or scrambling. |
| arxiv-0707.0311-p4-ltr-1col | coherent | same | Mathematical prose flows in correct logical order (lemma, proof cases, sequential problem statements) with no interleaving or glyph soup; only cosmetic ligature artifacts. |
| arxiv-0709.4466-p4-ltr-2col | coherent | same | Body prose, conclusions, acknowledgment, and references all read in correct order; the leading axis-tick fragments and trailing 'BER' are expected figure-label noise, not ordering breaks in the prose. |
| arxiv-0710.2740-p4-ltr-1col | coherent | same | Prose flows in correct order throughout; only the matrix/equation regions are fragmented, which is inherent formula-extraction noise, not reading-order breakage. |
| arxiv-1307.7059-p2-ltr-2col | coherent | same | Continuous, correctly ordered prose across sections (related work → III. Motivation); only a minor word-per-line whitespace artifact in one paragraph, with no order breakage. |
| arxiv-1808.03354-p2-ltr-2col | coherent | same | Prose flows in correct order across sections and page break (stray page number '2' and typical math-formula fragmentation are minor noise, not order breakage). |
| ibk-72-102-academic-2col | partial | same | Body prose is well-ordered vertical-rtl scholarship, but the running header/title (「法然門下における善導『観経疏』「一一願言」をめぐる議論と伝…承（中村）」) is interleaved mid-sentence, splitting 「往生し…得ない」. |
| ibk-72-102-p4-academic-2col | partial | same | Body prose reads coherently in order, but the running header/title (法然門下における…をめぐる議論と伝…承（中村）) is interleaved mid-sentence, splitting 解釈 into 解…釈している — a localized order defect. |
| iwaki-p1-pure-vert | partial | same | Body prose reads in correct order, but the title めぐみをつなぐバトン is split with ン stranded on its own line (localized order break); page footers harmless. |
| iwaki-p3-magazine-vert | partial | same | Main article body reads as coherent vertical-rtl prose, but the interview block has every line duplicated (each sentence emitted twice), a localized extraction artifact rather than page-wide scrambling. |
| iwaki-p6-mixed-orient | partial | same | Body prose reads in order, but numerals from vertical text are displaced ('東日本大震災から年の節目' missing the '15' stranded on its own lines), a heading fuses into the body start, and page-footer lines interleave mid-text. |
| nihonbungaku-64-11-p3-academic | coherent | same | Continuous, well-ordered Japanese prose (aside from a leading page number '13'); sentences flow logically across both paragraphs with no column interleaving or glyph soup. |
| nihonbungaku-64-11-p4-academic | coherent | same | Japanese academic prose reads in correct continuous order (sections 3-1, 3-2, list a-e follow logically); only trivial inline footnote markers (⑹⑺⑻) split a couple of words, not an order problem. |
| sodegaura-p3-pure-vert | partial | same | Body prose (介護の日 feature, interview, 補助金 notice) reads in correct order, but a localized glyph-by-glyph digit/letter soup block (phone/contact numbers) appears mid-header and the closing 介護保険課☎（）（） has its digits stripped out. |
| sodegaura-p4-mixed-orient | partial | same | Main interview and directory prose are readable, but the resident-voices speech bubbles interleave line-by-line, the interview and intro paragraphs are split across intervening blocks, and one map label (線鴨川葉千) is reversed. |
| transformer-p4-ltr-2col | coherent | same | Body prose reads in correct order with intact sections and sentences; only minor artifacts are figure-label lines at top and math notation linebreaks, normal for extraction. |
| yamato-p10-pure-vert | partial | same | Body prose flows in order per article, but headline fragments interleave mid-sentence (人権作文・ポスタ…ーの優秀作品を表彰, ご意見…を), inset numerals (18/43, 13/70/21/30) are displaced to block edges, and furigana/mojibake fragments (かくた, ճίϯςετ, ˙΍·ͱ) inject localized noise. |
| yamato-p4-mixed-orient | partial | same | Body prose reads in correct order overall, but a chart-legend fragment is interleaved mid-sentence (うち１００デベシル以上騒音測定回数), digits are dropped/split (１０デシベル０, 月には), and one mojibake run (ԋủϰỸϦΞϯτ...) breaks a heading; chart-number soup is expected graph noise. |
| yamato-p9-pure-vert | partial | same | Body paragraphs read as coherent prose, but the large headline (第○回大和市街づくり賞が決定〜表彰式とパネル展も開催します) is split into fragments interleaved between paragraphs, and route names appear as mojibake (˔͖ͭΈӺớΓỜ). |

## Known-bad / not-yet-fixed

ibk-72-102-academic-2col, ibk-72-102-p4-academic-2col, iwaki-p1-pure-vert, iwaki-p3-magazine-vert, iwaki-p6-mixed-orient, sodegaura-p3-pure-vert, sodegaura-p4-mixed-orient, yamato-p10-pure-vert, yamato-p4-mixed-orient, yamato-p9-pure-vert
