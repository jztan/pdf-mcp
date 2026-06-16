# pdf-mcp vs pdf_oxide — text-extraction benchmark

Reproduce with `python scripts/benchmark_vs_pdf_oxide.py`. Each number is a
cold full-document text extraction (open + extract every page + join), warmup +
best-of-7 on the same PDF. The corpus is generated deterministically from the
demo sample prose at several page counts, so the run needs no network and is
reproducible anywhere.

- **Hardware:** 4-core Linux sandbox. pdf-mcp on PyMuPDF 1.27.2;
  [pdf_oxide](https://github.com/yfedoseev/pdf_oxide) **0.3.64** (pure-Rust,
  Python bindings); [pdfminer.six](https://github.com/pdfminer/pdfminer.six)
  **20260107** (pure-Python). `pip install pdf_oxide pdfminer.six`.
- Four engines:
  - **pdf-mcp (reading-order)** — our production `pdf_read_all` path,
    `extract_text_from_page(page, sort_by_position=True)`. With the
    `multicolumn` extra installed (it is here) this runs column detection per
    page so multi-column text is not interleaved.
  - **pdf-mcp (raw)** — bare `page.get_text()`, the PyMuPDF floor with no
    reading-order work. This is the apples-to-apples engine comparison.
  - **pdf_oxide** — `PdfDocument(path)[i].text` joined over all pages.
  - **pdfminer.six** — `high_level.extract_text(path)`, the pure-Python
    reference extractor, included as a familiar slow baseline.
- All four extract essentially the same string (char counts within **~0.2%**
  for the PyMuPDF/pdf_oxide trio; pdfminer.six emits ~7% more, mostly extra
  intra-word spaces). Cross-language (Rust / PyMuPDF-C / pure-Python); read the
  ratios as directional.

## Full-document text extraction (min ms, best-of-7)

| PDF (pages) | pdf-mcp reading-order | pdf-mcp raw | pdf_oxide | pdfminer.six |
|-------------|----------------------:|------------:|----------:|-------------:|
| synthetic (15)  | 42.5 ms  | 14.8 ms  | 11.3 ms  | 375.2 ms  |
| synthetic (26)  | 79.2 ms  | 24.7 ms  | 20.5 ms  | 652.3 ms  |
| synthetic (75)  | 222.7 ms | 69.7 ms  | 69.0 ms  | 1942.0 ms |
| synthetic (216) | 651.0 ms | 210.0 ms | 201.3 ms | 5641.5 ms |

Output chars (216p): reading-order 443338 · raw 442042 · pdf_oxide 442474 ·
pdfminer.six 477354.

## Relative speed (min ms ratios)

| PDF (pages) | pdf_oxide vs reading-order | pdf_oxide vs raw | pdfminer.six vs pdf_oxide |
|-------------|---------------------------:|-----------------:|--------------------------:|
| synthetic (15)  | **3.8x** | 1.31x | **33x slower** |
| synthetic (26)  | **3.9x** | 1.20x | **32x slower** |
| synthetic (75)  | **3.2x** | 1.01x | **28x slower** |
| synthetic (216) | **3.2x** | 1.04x | **28x slower** |

## Takeaways

1. **Against the raw PyMuPDF engine, pdf_oxide is roughly at parity** — ~1.0x on
   the larger docs, with a ~1.2–1.3x edge on small ones (lower per-call init
   overhead). pdf_oxide's headline "5x faster than industry leaders" does **not**
   hold against PyMuPDF for plain text extraction; the two are peers.
2. **pdf_oxide is ~3.2–4.0x faster than pdf-mcp's production reading-order
   path.** That entire gap is our per-page column detection (the `multicolumn`
   extra / onnxruntime), *not* the base engine. We pay it deliberately to keep
   multi-column reading order correct — but it is the clear optimization target
   if extraction latency matters (e.g. sample pages for layout, cache the column
   decision, or make it opt-in per call).
3. **pdfminer.six is ~28–33x slower than pdf_oxide** and ~27x slower than raw
   PyMuPDF — even our column-detecting reading-order path beats it by ~9x. The
   pure-Python reference extractor is the clear loser on speed; both pdf_oxide
   and PyMuPDF are in a different performance class.
4. **Not measured here, and a real pdf-mcp advantage:** our SQLite page cache
   makes a re-read of the same path effectively free (~5–10 ms regardless of
   size — see `vs_pdf_reader_mcp_results.md`). pdf_oxide has no persistent cache,
   so in an agent loop that revisits the same document, pdf-mcp wins decisively
   on the second and later reads even where it loses cold.
5. Absolute numbers are from a 4-core Linux box and will differ on other
   hardware; the ratios (same machine, same PDFs, ~identical output) should hold.

## Tategaki 縦書き — vertical-Japanese reading-order correctness

Speed is one axis; *reading order on vertical Japanese* is where the engines
actually diverge. Reproduce with `python scripts/benchmark_vs_pdf_oxide.py
--tategaki` (needs `reportlab`). The test builds an authentic vertical PDF
(reportlab + Adobe-Japan1 `UniJIS-UCS2-V` CMap): three right-to-left columns
emitted to the content stream in *scrambled* order, so recovering the order
requires real vertical layout analysis — not luck of stream order. Score =
char-order similarity to the ground truth
`これは縦書きの日本語です。右から左へ読みます。正しい順序を確認する。`.

| engine | reading-order accuracy | note |
|--------|-----------------------:|------|
| pdf-mcp (reading-order) | 62% | columns out of order |
| pdf-mcp (raw) | 62% | follows stream order; no RTL column logic |
| pdf_oxide | 38% | sorts columns **left-to-right** → RTL order reversed |
| pdfminer.six (default) | 62% | columns out of order |
| **pdfminer.six `detect_vertical=True`** | **100%** ✅ | true vertical RTL handling |

Findings:

1. **Character fidelity is a tie** — on a horizontal Japanese PDF (embedded
   IPAGothic) every engine extracts the kanji/kana at 100%. The split is purely
   about *order*.
2. **Only `pdfminer.six` with `detect_vertical=True` reconstructs tategaki
   reading order correctly** (100%). It is the slowest engine by far, but it is
   the single tool here with real vertical-writing-mode support.
3. **pdf_oxide is the worst on tategaki** (38%): it geometrically orders columns
   left-to-right, which is exactly backwards for 縦書き, so it reliably *reverses*
   the column order.
4. **pdf-mcp and raw PyMuPDF land in the middle** (62%): they don't reverse the
   columns but don't apply RTL ordering either — correct only when the PDF's
   content stream already happens to be in reading order. (An earlier non-
   scrambled PDF made raw PyMuPDF look like 100%; the scrambled stream above is
   the honest test.)
5. **Implication for pdf-mcp:** proper 縦書き support (detect `WMode`/vertical
   glyph runs, order columns right-to-left) is a gap in our stack regardless of
   engine. The pragmatic path is to detect vertical pages and delegate them to a
   `detect_vertical` pdfminer.six pass, keeping the fast PyMuPDF path for the
   common horizontal case.

**Caveat:** tested on reportlab-generated vertical PDFs using the standard
Adobe-Japan1 vertical CMap; real-world vertical PDFs (embedded fonts, mixed
horizontal runs, ruby/furigana) may behave differently.
