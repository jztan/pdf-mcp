#!/usr/bin/env python
"""
scripts/gen_bge_small_reference.py

(Re)generates src/pdf_mcp/bge_small_reference.json: the fixed reference
sentences + their local-fastembed BAAI/bge-small-en-v1.5 vectors used by
pdf_mcp.remote_embedding_check's startup safety check (issue #42).

Not run automatically and not part of any test -- this is a one-time (or
rare, e.g. if the sentence set is deliberately changed) generation step. The
resulting JSON is committed to the repo and read back by
remote_embedding_check.load_reference at startup; regenerating it changes
what every future remote-backend startup is checked against, so treat a
regeneration like any other change to committed reference data.

Run: uv run python scripts/gen_bge_small_reference.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from pdf_mcp import embedder  # noqa: E402

# Short, topically diverse (policy/science/security/consumer) so a pooling
# or quantization drift that only shows up on some sentence shapes is more
# likely to surface than if all eight sentences were near-duplicates.
SENTENCES = [
    "The quarterly report highlights a steady increase in cloud"
    " infrastructure spending.",
    "Photosynthesis converts sunlight, water, and carbon dioxide into"
    " glucose and oxygen.",
    "The committee voted to postpone the zoning decision until next"
    " month's session.",
    "A zero-trust architecture assumes no implicit trust between network" " segments.",
    "Artemis missions aim to return astronauts to the lunar surface by the"
    " end of the decade.",
    "Consumers reported rising concern over hidden fees in short-term"
    " lending products.",
    "The router firmware update patches a critical remote code execution"
    " vulnerability.",
    "Agricultural subsidies are intended to stabilize farm income during"
    " volatile harvests.",
]

OUTPUT_PATH = REPO / "src" / "pdf_mcp" / "bge_small_reference.json"


def main() -> int:
    vecs = embedder.encode(SENTENCES, embedder.DEFAULT_MODEL)
    data = {
        "model": embedder.DEFAULT_MODEL,
        "sentences": SENTENCES,
        "vectors": [v.tolist() for v in vecs],
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(
        f"wrote {len(SENTENCES)} reference vectors (dim={len(vecs[0])})"
        f" to {OUTPUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
