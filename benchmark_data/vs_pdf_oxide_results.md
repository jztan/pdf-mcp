# pdf-mcp vs pdf_oxide — text-extraction benchmark

Reproduce with `python scripts/benchmark_vs_pdf_oxide.py`. Each number is a
cold full-document text extraction (open + extract every page + join), warmup +
best-of-7 on the same PDF. The corpus is generated deterministically from the
demo sample prose at several page counts, so the run needs no network and is
reproducible anywhere.

- **Hardware:** Apple M4 Pro (14-core), macOS (Darwin 25.5). Re-run 2026-06-16
  against current `develop`. pdf-mcp on PyMuPDF **1.27.2.2**;
  [pdf_oxide](https://github.com/yfedoseev/pdf_oxide) **0.3.64** (pure-Rust,
  Python bindings); [pdfminer.six](https://github.com/pdfminer/pdfminer.six)
  **20260107** (pure-Python). `pip install pdf_oxide pdfminer.six`.
- Four engines:
  - **pdf-mcp (reading-order)** — our production `pdf_read_all` path,
    `extract_text_from_page(page, sort_by_position=True)`. With the
    `multicolumn` extra installed (it is here) this runs column detection per
    page so multi-column text is not interleaved. As of the vertical-script
    merge it *also* runs `detect_writing_mode` on every page — see the per-stage
    breakdown below.
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
| synthetic (15)  | 85.2 ms   | 9.1 ms   | 3.4 ms  | 109.0 ms  |
| synthetic (26)  | 148.7 ms  | 15.6 ms  | 6.0 ms  | 188.6 ms  |
| synthetic (75)  | 429.7 ms  | 44.2 ms  | 18.9 ms | 555.7 ms  |
| synthetic (216) | 1257.3 ms | 130.5 ms | 53.2 ms | 1631.3 ms |

Output chars (216p): reading-order 443338 · raw 442042 · pdf_oxide 442474 ·
pdfminer.six 477354.

## Relative speed (min ms ratios)

| PDF (pages) | pdf_oxide vs reading-order | pdf_oxide vs raw | pdfminer.six vs pdf_oxide |
|-------------|---------------------------:|-----------------:|--------------------------:|
| synthetic (15)  | **25.1x** | 2.68x | **32x slower** |
| synthetic (26)  | **24.8x** | 2.60x | **31x slower** |
| synthetic (75)  | **22.7x** | 2.34x | **29x slower** |
| synthetic (216) | **23.6x** | 2.45x | **31x slower** |

> **Hardware note:** these ratios differ sharply from the earlier 4-core Linux
> run, where pdf_oxide was ~3.2x faster than reading-order and ~at parity with
> raw PyMuPDF. On native-ARM Apple Silicon, the pure-Rust pdf_oxide pulls much
> further ahead: ~2.5x faster than raw PyMuPDF and ~24x faster than our
> reading-order path. Absolute numbers and ratios are machine-specific; output
> is identical (deterministic corpus), so only the timings move.

## Where the reading-order time goes (216p, best-of-7)

The reading-order path is ~24x slower than pdf_oxide, but that is **not** the
base engine — it is the two per-page analysis passes layered on top:

| stage | min ms (216p) | note |
|-------|--------------:|------|
| raw `get_text` x216            | 263.6 ms  | the PyMuPDF floor |
| `detect_writing_mode` x216     | **822.8 ms** | full per-page glyph scan (vertical-script merge) |
| `detect_column_boxes` x216     | 559.9 ms  | onnxruntime column detection (`multicolumn` extra) |
| full reading-order x216        | 1255.3 ms | the production path |

The single largest cost is now **`detect_writing_mode`** (added by the
vertical-script feature), which scans every glyph on every page to classify
orientation — it outweighs even the onnxruntime column detector. It is the
clearest optimization target: short-circuit horizontal-dominant pages early, or
cache the per-page decision.

## Takeaways

1. **On Apple Silicon, pdf_oxide clearly beats raw PyMuPDF** (~2.3–2.7x), where
   on the old 4-core Linux box the two were near parity. The pure-Rust engine
   scales better on native ARM. pdf_oxide's headline "5x faster" still overstates
   the gap for plain text, but it is no longer a tie.
2. **pdf_oxide is ~23–25x faster than pdf-mcp's production reading-order path.**
   That entire gap is our two per-page passes — `detect_writing_mode` (the larger
   share now) and onnxruntime column detection — *not* the base engine. We pay it
   deliberately for correct multi-column and vertical reading order, but both are
   optimization targets if extraction latency matters (sample pages, cache the
   decision, make each pass opt-in per call).
3. **pdfminer.six is ~29–32x slower than pdf_oxide** and ~12x slower than raw
   PyMuPDF — even our two-pass reading-order path edges it out. The pure-Python
   reference extractor is the clear loser on speed; both pdf_oxide and PyMuPDF
   are in a different performance class.
4. **Not measured here, and a real pdf-mcp advantage:** our SQLite page cache
   makes a re-read of the same path effectively free (~5–10 ms regardless of
   size — see `vs_pdf_reader_mcp_results.md`). pdf_oxide has no persistent cache,
   so in an agent loop that revisits the same document, pdf-mcp wins decisively
   on the second and later reads even where it loses cold.
5. Absolute numbers are from an Apple M4 Pro and will differ on other hardware;
   the ratios (same machine, same PDFs, ~identical output) should hold per-class.

## Tategaki 縦書き — vertical-Japanese reading-order correctness

Speed is one axis; *reading order on vertical Japanese* is where the engines
actually diverge. Reproduce with `python scripts/benchmark_vs_pdf_oxide.py
--tategaki` (needs `reportlab`). The test builds an authentic vertical PDF
(reportlab + Adobe-Japan1 `UniJIS-UCS2-V` CMap): three right-to-left columns
emitted to the content stream in *scrambled* order, so recovering the order
requires real vertical layout analysis — not luck of stream order. Score =
char-order similarity to the ground truth
`これは縦書きの日本語です。右から左へ読みます。正しい順序を確認する。`

| engine | reading-order accuracy | note |
|--------|-----------------------:|------|
| **pdf-mcp (reading-order)** | **100%** ✅ | `reorder_vertical` — RTL column order, PyMuPDF-only |
| pdf-mcp (raw) | 62% | follows stream order; no RTL column logic |
| pdf_oxide | 38% | sorts columns **left-to-right** → RTL order reversed |
| pdfminer.six (default) | 62% | columns out of order |
| pdfminer.six `detect_vertical=True` | 100% ✅ | true vertical RTL handling |

Findings:

1. **Character fidelity is a tie** — on a horizontal Japanese PDF (embedded
   IPAGothic) every engine extracts the kanji/kana at 100%. The split is purely
   about *order*.
2. **pdf-mcp now reconstructs tategaki reading order correctly (100%)** — the
   vertical-script merge added a PyMuPDF-only `reorder_vertical` path
   (`detect_writing_mode` → glyph-positioned RTL column ordering). It matches the
   best result here **without** the pdfminer dependency the earlier run
   recommended. In that earlier run pdf-mcp scored 62%; this is the gap closing.
3. **pdfminer.six with `detect_vertical=True` also reaches 100%**, but it is the
   slowest engine by far. pdf-mcp now achieves the same order on the fast
   in-process PyMuPDF stack.
4. **pdf_oxide is the worst on tategaki** (38%): it geometrically orders columns
   left-to-right, which is exactly backwards for 縦書き, so it reliably *reverses*
   the column order.
5. **Raw PyMuPDF and default pdfminer land in the middle** (62%): they don't
   reverse the columns but don't apply RTL ordering either — correct only when
   the PDF's content stream already happens to be in reading order.

**Caveat:** tested on reportlab-generated vertical PDFs using the standard
Adobe-Japan1 vertical CMap; real-world vertical PDFs (embedded fonts, mixed
horizontal runs, ruby/furigana) may behave differently. See the project's own
vertical-jp corpus notes for the messier cases.
