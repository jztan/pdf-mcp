import json
import statistics
import sys
import time
from pathlib import Path
from pdf_mcp.server import pdf_corpus_warm, pdf_corpus_search

REPO = Path(sys.argv[1])
label = sys.argv[2]
docs = json.loads((REPO / "benchmark_data/corpus_search/manifest.json").read_text())[
    "docs"
]
qs = json.loads((REPO / "benchmark_data/corpus_search/queries.json").read_text())[
    "queries"
]
paths = [str((REPO / d["path"]).resolve()) for d in docs]
t0 = time.time()
r = pdf_corpus_warm(paths, budget_seconds=900, embeddings=True)
while r.get("unprocessed"):
    r = pdf_corpus_warm(paths, budget_seconds=900, embeddings=True)
warm_s = time.time() - t0
scores, low = [], 0
for q in qs:
    res = pdf_corpus_search(paths, q["query"], mode="semantic", top_k=10)
    for m in res.get("matches", []):
        s = m.get("semantic_score", m.get("score"))
        if s is not None:
            scores.append(float(s))
            low += int(bool(m.get("low_confidence")))
out = {
    "label": label,
    "warm_seconds": round(warm_s),
    "n": len(scores),
    "mean": round(statistics.mean(scores), 4),
    "median": round(statistics.median(scores), 4),
    "p10": round(sorted(scores)[len(scores) // 10], 4),
    "p90": round(sorted(scores)[9 * len(scores) // 10], 4),
    "low_confidence_rate": round(low / max(1, len(scores)), 4),
}
print("RESULT " + json.dumps(out))
