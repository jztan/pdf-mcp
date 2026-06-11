# Boilerplate detection on real PDFs

Full method (`freq_runs`) on real documents (`--real`). Real PDFs have no per-block labels, so this reports label-free signals: a recall check against hand-verified known boilerplate, a precision sentinel (real boilerplate is high-frequency, so low-coverage strips are surfaced for review), token savings, and how much extra a RAG-on-PDF-style naive filter would wrongly strip. PDFs are gitignored; see `benchmark_data/boilerplate_real_corpus.json` for sources.

| document | pages | known recall | tokens stripped | % | suspects | naive over-strip (tokens) |
| --- | --- | --- | --- | --- | --- | --- |
| attention.pdf | 15 | 1/1 | 14 | 0.2% | 0 | 335 |
| gdpr.pdf | 88 | 1/1 | 880 | 1.6% | 0 | 379 |
| gpt3.pdf | 75 | 1/1 | 74 | 0.2% | 0 | 106 |

## What the full method stripped (per document)

### attention.pdf — Attention Is All You Need

| stripped signature (digit-normalized) | page coverage |
| --- | --- |
| `#` | 93% |

Body content the naive filter would wrongly strip (sample): `Illia Polosukhin∗‡ illia.polosukhin@gmail.com`; `Abstract`; `1 Introduction`; `2 Background`; `3 Model Architecture`; `3.2 Attention`

### gdpr.pdf — Regulation (EU) 2016/679 (GDPR)

| stripped signature (digit-normalized) | page coverage |
| --- | --- |
| `#.#.# l #/# official journal of the european union en` | 100% |

Body content the naive filter would wrongly strip (sample): `I`; `(Legislative acts)`; `REGULATIONS`; `Whereas:`; `CHAPTER I`; `General provisions`

### gpt3.pdf — Language Models are Few-Shot Learners (GPT-3)

| stripped signature (digit-normalized) | page coverage |
| --- | --- |
| `#` | 99% |

Body content the naive filter would wrongly strip (sample): `OpenAI`; `Abstract`; `Contents`; `1 Introduction 3`; `5 Limitations 33`; `8 Conclusion 40`

