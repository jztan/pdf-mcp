"""Which CUDA libraries did the process actually map? (wheel vs system)"""
import os, sys, re, glob, time
os.environ["PDF_MCP_CUDA"] = "1"
import warnings
import pypdfium2 as pdfium
from pdf_mcp import embedder
from pdf_mcp.extractor import page_embedding_units
doc = pdfium.PdfDocument("repo/pages/corpus/gao-cloud.pdf")
units = []
for i in range(len(doc)):
    units.extend(page_embedding_units(doc[i].get_textpage().get_text_range()))
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    t0 = time.perf_counter(); embedder.encode(units, "BAAI/bge-small-en-v1.5"); dt = time.perf_counter() - t0
print("providers", embedder._providers(embedder._get_model("BAAI/bge-small-en-v1.5")), "gao_s", round(dt, 2))
for x in w: print("WARNING:", str(x.message)[:200])
seen = set()
for line in open("/proc/self/maps"):
    m = re.search(r"(/\S+/(libcudnn|libcublas|libcublasLt|libcudart|libcufft|libcurand)\S*\.so\S*)", line)
    if m and m.group(1) not in seen:
        seen.add(m.group(1)); print("MAPPED", m.group(1))
