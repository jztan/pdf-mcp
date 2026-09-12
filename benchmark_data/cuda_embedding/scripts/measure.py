"""Time pdf_mcp.embedder.encode over real sub-page units; emit one JSON line.
usage: python measure.py <label> <corpus_dir> <out.json> [ref_vectors.npy]"""
import glob, json, os, sys, time, warnings
import numpy as np
label, corpus, out = sys.argv[1:4]
ref = sys.argv[4] if len(sys.argv) > 4 else None
import pypdfium2 as pdfium
from pdf_mcp import embedder
from pdf_mcp.extractor import page_embedding_units
per_doc = {}
units = []
for p in sorted(glob.glob(os.path.join(corpus, "*.pdf"))):
    doc = pdfium.PdfDocument(p)
    u = []
    for i in range(len(doc)):
        u.extend(page_embedding_units(doc[i].get_textpage().get_text_range()))
    per_doc[os.path.basename(p)] = (len(units), len(units) + len(u), len(doc))
    units.extend(u)
model = "BAAI/bge-small-en-v1.5"
caught = []
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    t0 = time.perf_counter()
    embedder.encode(units[:4], model)          # load + warm-up
    t_load = time.perf_counter() - t0
    caught = [str(x.message) for x in w]
providers = embedder._providers(embedder._get_model(model))
t0 = time.perf_counter()
vecs = np.array([np.frombuffer(b, dtype=np.float32) for b in embedder.encode(units, model)])
dt = time.perf_counter() - t0
# gao-cloud alone, for parity with the PR's table
g = per_doc.get("gao-cloud.pdf")
gao = None
if g:
    t0 = time.perf_counter(); embedder.encode(units[g[0]:g[1]], model); gao = time.perf_counter() - t0
res = dict(label=label, env=os.environ.get("PDF_MCP_CUDA"), providers=providers,
           warnings=caught, units=len(units), load_s=round(t_load, 2),
           all_s=round(dt, 2), units_per_s=round(len(units) / dt, 1),
           gao_cloud_s=round(gao, 2) if gao else None,
           docs={k: v[2] for k, v in per_doc.items()})
if ref:
    r = np.load(ref)
    cos = (vecs * r).sum(1) / (np.linalg.norm(vecs, axis=1) * np.linalg.norm(r, axis=1))
    res["cosine_vs_ref_min"] = float(cos.min()); res["cosine_vs_ref_mean"] = float(cos.mean())
np.save(out.replace(".json", ".npy"), vecs)
open(out, "w").write(json.dumps(res))
print(json.dumps(res))
