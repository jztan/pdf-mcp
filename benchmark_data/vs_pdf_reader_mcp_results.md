# pdf-mcp vs @sylphx/pdf-reader-mcp — end-to-end MCP benchmark

Reproduce with `scripts/benchmark_vs_pdf_reader_mcp.py --pdf <file>`. Each number
is an MCP `tools/call` round trip (warmup + best-of-5) on the same PDF, so it
reflects what an agent experiences, not just the internal extractor.

- **Hardware:** 4-core Linux sandbox (fork). pdf-mcp on PyMuPDF 1.27.2;
  pdf-reader-mcp **v2.4.3** on Node 22 / PDF.js.
- **cold** = a fresh temp copy of the PDF each call (no path-cache hit, for
  both servers). **warm** = pdf-mcp's SQLite cache hit (pdf-reader-mcp has no
  persistent cache, so it has no warm row).
- Cross-language and cross-design — read as directional, with the caveats below.

## Full-text extraction (their headline "5–10x parallel" claim)

| PDF (pages) | pdf-mcp cold | pdf-mcp warm | pdf-reader-mcp cold | cold speedup | warm speedup |
|-------------|-------------:|-------------:|--------------------:|-------------:|-------------:|
| Attention 1706.03762 (15) | 100 ms | 5.3 ms | 336 ms | **3.4x** | **63x** |
| Survey 1812.08434 (26)    | 152 ms | 9.0 ms | 344 ms | **2.3x** | **38x** |
| GPT-3 2005.14165 (75)     | 200 ms | 10.2 ms | 575 ms | **2.9x** | **56x** |

(min ms, best-of-5)

**pdf-mcp is ~2.3–3.4x faster cold and ~38–63x faster warm.** Their PDF.js
async parallelism does not overtake our sequential PyMuPDF: the C engine is
simply faster per page than parallel JS, and our SQLite cache makes re-reads
effectively free.

### Output-size caveat (important for fairness)
pdf-reader-mcp returns ~2x the character count for the same PDF (e.g. Attention:
79,473 vs our 42,823 chars; raw PyMuPDF text is 39,498) — PDF.js emits much more
whitespace/layout filler. So the two aren't extracting identical strings. Even
normalized per output character, pdf-mcp is still faster (Attention: 428 vs 236
chars/ms). On the 75-page GPT-3 paper our `pdf_read_all` also byte-caps output
(203 K vs their 473 K chars) by design to protect agent context — so that row
additionally reflects us doing less work; the 15-page row (under the cap) is the
clean comparison and still shows 3.4x.

## Info — metadata + page count

| PDF (pages) | pdf-mcp `pdf_info` | pdf-reader-mcp | note |
|-------------|-------------------:|---------------:|------|
| Attention (15) | 134 ms | 4.7 ms | pdf-mcp ~29x slower |
| Survey (26)    | 142 ms | 6.0 ms | pdf-mcp ~24x slower |
| GPT-3 (75)     | 160 ms | 5.8 ms | pdf-mcp ~28x slower |

Here **pdf-reader-mcp wins decisively.** But it's not apples-to-apples: their
call reads only the PDF metadata dict + page count, while our `pdf_info` also
runs per-page scanned-document detection and builds a TOC summary (richer
output: ~1.8 K vs ~1 K chars). Still, ~24–29x is a real gap and flags
`pdf_info` as an optimization target — e.g. sample pages for scanned-detection
instead of scanning all, or make the heavy fields lazy/opt-in.

## Takeaways

1. **On their own headline metric (full-text speed) pdf-mcp wins** ~2.3–3.4x
   cold and dramatically more warm — parallel PDF.js doesn't beat PyMuPDF + cache.
2. **pdf_info is our weak spot** (~25x slower) because it does more per call;
   worth optimizing.
3. Numbers are from a 4-core Linux box; absolute values will differ on other
   hardware, but the ratios (same machine, both servers) should hold.
