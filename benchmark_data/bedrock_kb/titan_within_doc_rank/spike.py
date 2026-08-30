"""THROWAWAY: within-document gold-page rank for the 41 unanswered described
queries under Titan v2 embeddings vs the shipped bge-small."""
import json, sys, time, statistics as st
from pathlib import Path
import numpy as np
import boto3

REPO = Path("/Users/jztan/src/pdf-mcp")
S = Path(sys.argv[1])
from pdf_mcp.server import cache, pdf_config
from pdf_mcp import embedder
from pdf_mcp.extractor import page_embedding_units

rt = boto3.Session(region_name="us-east-1").client("bedrock-runtime")
MODEL = "amazon.titan-embed-text-v2:0"
cache_path = S / "titan_vectors.jsonl"
vec_cache = {}
if cache_path.exists():
    for line in cache_path.read_text().splitlines():
        r = json.loads(line); vec_cache[r["k"]] = r["v"]

def titan(text: str, key: str):
    if key in vec_cache:
        return np.array(vec_cache[key], dtype=np.float32)
    body = json.dumps({"inputText": text[:8000], "dimensions": 1024, "normalize": True})
    for attempt in range(5):
        try:
            out = rt.invoke_model(modelId=MODEL, body=body, contentType="application/json", accept="application/json")
            v = json.loads(out["body"].read())["embedding"]; break
        except Exception as e:
            if attempt == 4: raise
            time.sleep(1.5 * (attempt + 1))
    vec_cache[key] = v
    with cache_path.open("a") as fh: fh.write(json.dumps({"k": key, "v": v}) + "\n")
    return np.array(v, dtype=np.float32)

n = json.load(open(REPO / "benchmark_data/bedrock_kb/results.json"))
q = {x["id"]: x for x in json.load(open(REPO / "benchmark_data/corpus_search/queries.json"))["queries"]}
man = {d["id"]: str(REPO / d["path"]) for d in json.load(open(REPO / "benchmark_data/corpus_search/manifest.json"))["docs"]}
flagged = [i for i in n["summary"]["flagged"] if i.startswith("described")]
model = pdf_config.embedding_model
rows = []
calls = 0
for qid in flagged:
    lab = q[qid]["labels"][0]; path = man[lab["doc"]]; meta = cache.get_metadata(path)
    gold = lab["page"] - 1
    # bge (shipped)
    embs = cache.get_page_embeddings(path, list(range(meta["page_count"])), model)
    qv = embedder.encode_query(q[qid]["query"], model)
    bge_scores = {pn: float((np.stack([np.frombuffer(b, dtype=np.float32) for b in bl]) @ qv).max()) for pn, bl in embs.items() if bl}
    # titan on the same unit texts
    tq = titan(q[qid]["query"], f"q|{qid}")
    titan_scores = {}
    for pn in bge_scores:
        text = cache.get_page_text(path, pn) or ""
        units = page_embedding_units(text)
        if not units: continue
        vs = np.stack([titan(u, f"u|{lab['doc']}|{pn}|{k}") for k, u in enumerate(units)])
        calls += len(units)
        titan_scores[pn] = float((vs @ tq).max())
    def rank(scores):
        if gold not in scores: return None, None
        ranked = sorted(scores, key=lambda p: -scores[p]); return ranked.index(gold) + 1, round(scores[ranked[0]] - scores[gold], 3)
    rb, mb = rank(bge_scores); rt_, mt = rank(titan_scores)
    rows.append((qid, lab["doc"], lab["page"], meta["page_count"], rb, mb, rt_, mt))
    print(qid, lab["doc"], f"p{lab['page']}/{meta['page_count']}", "bge rank", rb, "margin", mb, "| titan rank", rt_, "margin", mt, flush=True)

ok = [r for r in rows if r[4] and r[6]]
b = [r[4] for r in ok]; t = [r[6] for r in ok]
print("\nn", len(ok))
print("bge   : median", st.median(b), "==1", sum(x == 1 for x in b), "<=3", sum(x <= 3 for x in b), "<=5", sum(x <= 5 for x in b), ">10", sum(x > 10 for x in b))
print("titan : median", st.median(t), "==1", sum(x == 1 for x in t), "<=3", sum(x <= 3 for x in t), "<=5", sum(x <= 5 for x in t), ">10", sum(x > 10 for x in t))
print("titan better / same / worse:", sum(1 for x, y in zip(b, t) if y < x), sum(1 for x, y in zip(b, t) if y == x), sum(1 for x, y in zip(b, t) if y > x))
print("bge median margin", st.median([r[5] for r in ok]), "| titan median margin", st.median([r[7] for r in ok]))
json.dump(rows, open(S / "titan_spike_rows.json", "w"))
print("DONE")
