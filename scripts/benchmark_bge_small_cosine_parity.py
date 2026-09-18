#!/usr/bin/env python
"""
scripts/benchmark_bge_small_cosine_parity.py

Cosine parity between local fastembed BAAI/bge-small-en-v1.5 (CLS pooling)
and the SAME model quantized to Q8_0 GGUF and served remotely via
`llama-server`, on real page-chunk passages -- not the tiny fixed reference
set used by the startup safety check (`remote_embedding_check.py`), which
this script does not touch.

This settles the first of jztan's two asks in issue #42: "cosine against
fastembed on the Q8_0 GGUF, since quantization can shift vectors on top of
any pooling difference."

Requires `llama-server` already running and serving bge-small-en-v1.5 with
CLS pooling -- this script makes no attempt to start one. Get the exact
combination this repo has validated with:

    llama-server -m <bge-small-en-v1.5-q8_0.gguf> --embedding \\
        --pooling cls --port 8712 -ngl 99 --host 127.0.0.1

--pooling cls is not optional: fastembed pools bge-small with CLS, not
mean (see docs/investigated-rejected.md's MLX entry and
benchmark_data/mlx_backend_results.md); omitting it would measure a
pooling-mismatch confound, not the quantization effect this script exists
to isolate.

Passages: real ~300-token page-chunk windows from pages/corpus/*.pdf, via
the SHIPPED extractor.chunk_page_text (the same chunking real warming
uses) -- not synthetic 30-word passages.

Run:
    uv run python scripts/benchmark_bge_small_cosine_parity.py
    uv run python scripts/benchmark_bge_small_cosine_parity.py \\
        --base-url http://127.0.0.1:8712/v1 --json out.json

Exit code 0 always (informational; no live server in CI to gate on).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from pdf_mcp import embedder  # noqa: E402
from pdf_mcp.docopen import open_pdf  # noqa: E402
from pdf_mcp.extractor import chunk_page_text, extract_text_from_page  # noqa: E402
from pdf_mcp.remote_embedder import RemoteSpec, encode as remote_encode  # noqa: E402

CORPUS_DIR = REPO / "pages" / "corpus"
DEFAULT_BASE_URL = "http://127.0.0.1:8712/v1"
DEFAULT_THRESHOLD = 0.99
# Cap on chunks per PDF so the passage set stays balanced across the 6
# corpus documents rather than dominated by whichever has the most pages.
MAX_CHUNKS_PER_PDF = 6
# bge-small-en-v1.5's real context window is 512 subword tokens.
# chunk_page_text's ~300-token target is a character-count estimate (4
# chars/token); a real minority of dense pages tokenize far below that
# ratio, so a chunk that looks like ~300 tokens by character count can
# really be 700+ subword tokens. Filtered by the server's own /tokenize
# endpoint below, not by character count -- see
# benchmark_bge_small_throughput.py's module docstring for the fuller
# story (that script hit this first).
MAX_CONTEXT_TOKENS = 512
TOKEN_SAFETY_MARGIN = 32


def _token_count(base_url: str, text: str) -> int:
    import httpx

    root = base_url.rsplit("/v1", 1)[0] if base_url.endswith("/v1") else base_url
    resp = httpx.post(
        root.rstrip("/") + "/tokenize", json={"content": text}, timeout=30
    )
    resp.raise_for_status()
    tokens: list[Any] = resp.json()["tokens"]
    return len(tokens)


def collect_passages(
    base_url: str,
    corpus_dir: Path = CORPUS_DIR,
    max_per_pdf: int = MAX_CHUNKS_PER_PDF,
) -> list[dict[str, Any]]:
    """Real ~300-token page-chunk windows from every PDF in corpus_dir.

    Uses extractor.chunk_page_text, the same sub-page windowing real warm
    embedding uses (see extractor.page_embedding_units) -- not an ad hoc
    splitter. Returns dicts with pdf/page/chunk_index/text so results can
    be reported per-passage.
    """
    limit = MAX_CONTEXT_TOKENS - TOKEN_SAFETY_MARGIN
    passages: list[dict[str, Any]] = []
    for pdf_path in sorted(corpus_dir.glob("*.pdf")):
        doc = open_pdf(str(pdf_path))
        try:
            page_count = len(doc)
            taken = 0
            for pn in range(page_count):
                if taken >= max_per_pdf:
                    break
                text = extract_text_from_page(doc[pn])
                if not text or not text.strip():
                    continue
                chunks = chunk_page_text(text)
                for ci, chunk in enumerate(chunks):
                    if taken >= max_per_pdf:
                        break
                    # Skip tiny leftover chunks; they're not representative
                    # ~300-token warm units.
                    if len(chunk) < 200:
                        continue
                    if _token_count(base_url, chunk) > limit:
                        continue
                    passages.append(
                        {
                            "pdf": pdf_path.name,
                            "page": pn,
                            "chunk_index": ci,
                            "text": chunk,
                        }
                    )
                    taken += 1
        finally:
            close = getattr(doc, "close", None)
            if close:
                close()
    return passages


def run(
    base_url: str, model: str, threshold: float, max_per_pdf: int
) -> dict[str, Any]:
    passages = collect_passages(base_url, max_per_pdf=max_per_pdf)
    if not passages:
        raise RuntimeError(f"no passages collected from {CORPUS_DIR}")
    texts = [p["text"] for p in passages]

    n_pdfs = len({p["pdf"] for p in passages})
    print(f"Collected {len(texts)} real page-chunk passages from {n_pdfs} corpus PDFs")

    local_vecs = embedder.encode(texts, embedder.DEFAULT_MODEL)  # normalized

    spec = RemoteSpec(base_url=base_url, model=model, batch_size=16)
    remote_raw = remote_encode(texts, spec)  # UNNORMALIZED, per remote_embedder
    import numpy as np

    norms = np.linalg.norm(remote_raw, axis=1, keepdims=True)
    remote_vecs = remote_raw / np.clip(norms, 1e-12, None)

    cosines = np.sum(local_vecs * remote_vecs, axis=1)
    for p, c in zip(passages, cosines):
        p["cosine"] = float(c)

    mean_cos = float(np.mean(cosines))
    min_cos = float(np.min(cosines))
    max_cos = float(np.max(cosines))
    below = [p for p in passages if p["cosine"] < threshold]

    print(f"\n{'pdf':<26}{'page':>5}{'chunk':>7}{'cosine':>10}")
    for p in passages:
        print(f"{p['pdf']:<26}{p['page']:>5}{p['chunk_index']:>7}{p['cosine']:>10.5f}")

    print(f"\n=== Cosine parity: fastembed CPU vs {model} @ {base_url} ===")
    print(f"  passages: {len(passages)}")
    print(f"  mean cosine: {mean_cos:.5f}")
    print(f"  min  cosine: {min_cos:.5f}")
    print(f"  max  cosine: {max_cos:.5f}")
    print(f"  below threshold ({threshold}): {len(below)}/{len(passages)}")
    verdict = "PASS" if min_cos >= threshold else "FAIL"
    print(f"  verdict: {verdict}")

    return {
        "base_url": base_url,
        "model": model,
        "threshold": threshold,
        "n_passages": len(passages),
        "mean_cosine": mean_cos,
        "min_cosine": min_cos,
        "max_cosine": max_cos,
        "n_below_threshold": len(below),
        "verdict": verdict,
        "passages": passages,
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--model", default="bge-small-en-v1.5-q8_0")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--max-per-pdf", type=int, default=MAX_CHUNKS_PER_PDF)
    ap.add_argument("--json", type=Path, default=None, help="dump full results here")
    args = ap.parse_args(argv)

    result = run(args.base_url, args.model, args.threshold, args.max_per_pdf)

    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
        print(f"\nWrote full results to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
