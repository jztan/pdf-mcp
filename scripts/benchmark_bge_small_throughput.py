#!/usr/bin/env python
"""
scripts/benchmark_bge_small_throughput.py

Throughput of local fastembed CPU vs the remote Vulkan `llama-server`
bge-small-en-v1.5 Q8_0 backend, on real ~300-token warm chunks -- not
30-word synthetic passages. Settles the second of jztan's two asks in
issue #42: "a throughput run on real warm chunks (~300 tokens, e.g.
pages/corpus) instead of 30-word passages."

Passages: every real page-chunk window (extractor.chunk_page_text, the
SAME chunker `corpus.warm_docs` uses via page_embedding_units) from every
PDF in pages/corpus/, minus tiny leftover chunks.

Requires `llama-server` already running and serving bge-small-en-v1.5 with
CLS pooling -- this script makes no attempt to start one:

    llama-server -m <bge-small-en-v1.5-q8_0.gguf> --embedding \\
        --pooling cls --port 8712 -ngl 99 --host 127.0.0.1 \\
        --parallel <max concurrency you intend to test>

--parallel matters here (unlike the cosine-parity script, which only ever
sends one request at a time): llama-server's default parallel slot count
is 1, so testing concurrency > 1 against a server started without
--parallel N measures queuing inside llama-server, not this client's
concurrency.

A finding from running this against the real corpus: chunk_page_text's
~300-token target is a character-count estimate (4 chars/token), and a
real minority of dense NIST/USDA pages tokenize as low as ~1.4 chars/token
-- some individual real chunks are 700-800+ real subword tokens, over
bge-small's 512-token context, even though the character estimate said
~300. llama-server hard-errors on an oversized single input rather than
truncating, so `collect_chunks` checks each candidate's REAL token count
via the server's own `/tokenize` endpoint before sending it and reports
how many were skipped -- see MAX_CONTEXT_TOKENS. See
benchmark_data/bge_small_throughput_results.md for the measured count.

Run:
    uv run python scripts/benchmark_bge_small_throughput.py
    uv run python scripts/benchmark_bge_small_throughput.py \\
        --concurrency 1 4 8 --json out.json

Exit code 0 always (informational; no live server in CI to gate on).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
DEFAULT_CONCURRENCY = [1, 4, 8]
MIN_CHUNK_CHARS = 200
# bge-small-en-v1.5's real context window is 512 subword tokens.
# chunk_page_text targets ~300 tokens via a 4-chars/token character
# estimate, but that estimate is only an average: chunk_page_text returns a
# page's text UNSPLIT whenever it estimates the page fits in one window,
# and measured against this repo's actual corpus (see
# benchmark_data/bge_small_throughput_results.md), a handful of dense
# NIST/USDA pages tokenize as low as ~1.4 chars/token -- some real
# ~1200-char chunks are 800+ real subword tokens, well over 512. llama-
# server hard-errors (HTTP 400 exceed_context_size_error) rather than
# truncating, so those outliers are filtered out here by REAL token count
# (via the server's own /tokenize endpoint, not the char-based estimate)
# rather than crashing the whole throughput run.
MAX_CONTEXT_TOKENS = 512
TOKEN_SAFETY_MARGIN = 32  # keep clear of the hard 512 ceiling


def _token_count(base_url: str, text: str) -> int:
    import httpx

    root = base_url.rsplit("/v1", 1)[0] if base_url.endswith("/v1") else base_url
    resp = httpx.post(
        root.rstrip("/") + "/tokenize", json={"content": text}, timeout=30
    )
    resp.raise_for_status()
    tokens: list[Any] = resp.json()["tokens"]
    return len(tokens)


def collect_chunks(
    base_url: str, corpus_dir: Path = CORPUS_DIR
) -> tuple[list[str], int]:
    """Every real ~300-token page-chunk window from every PDF in
    corpus_dir, via the shipped extractor.chunk_page_text -- the same
    chunker real warming uses (extractor.page_embedding_units).

    Filters out the rare chunk whose REAL token count (checked against
    `base_url`'s own /tokenize endpoint) exceeds bge-small's 512-token
    context, so those don't crash the encode() call below -- see
    MAX_CONTEXT_TOKENS. Returns (chunks, n_skipped_oversized).
    """
    candidates: list[str] = []
    for pdf_path in sorted(corpus_dir.glob("*.pdf")):
        doc = open_pdf(str(pdf_path))
        try:
            for pn in range(len(doc)):
                text = extract_text_from_page(doc[pn])
                if not text or not text.strip():
                    continue
                for c in chunk_page_text(text):
                    if len(c) >= MIN_CHUNK_CHARS:
                        candidates.append(c)
        finally:
            close = getattr(doc, "close", None)
            if close:
                close()

    chunks: list[str] = []
    skipped = 0
    limit = MAX_CONTEXT_TOKENS - TOKEN_SAFETY_MARGIN
    for c in candidates:
        if _token_count(base_url, c) > limit:
            skipped += 1
            continue
        chunks.append(c)
    return chunks, skipped


def run_local(texts: list[str]) -> dict[str, Any]:
    t0 = time.monotonic()
    arr = embedder.encode(texts, embedder.DEFAULT_MODEL)
    dt = time.monotonic() - t0
    return {"seconds": dt, "texts_per_sec": len(texts) / dt, "dim": int(arr.shape[1])}


def run_remote(
    texts: list[str], base_url: str, model: str, batch_size: int, concurrency: int
) -> dict[str, Any]:
    spec = RemoteSpec(
        base_url=base_url,
        model=model,
        batch_size=batch_size,
        max_concurrency=concurrency,
    )
    t0 = time.monotonic()
    arr = remote_encode(texts, spec)
    dt = time.monotonic() - t0
    return {"seconds": dt, "texts_per_sec": len(texts) / dt, "dim": int(arr.shape[1])}


def run(
    base_url: str,
    model: str,
    concurrency_levels: list[int],
    batch_size: int,
    n_texts: int,
) -> dict[str, Any]:
    chunks, skipped = collect_chunks(base_url)
    if not chunks:
        raise RuntimeError(f"no chunks collected from {CORPUS_DIR}")
    texts = chunks[:n_texts] if n_texts else chunks
    avg_chars = sum(len(t) for t in texts) / len(texts)
    print(
        f"Collected {len(chunks)} real ~300-token chunks from "
        f"{CORPUS_DIR}; using {len(texts)} (avg {avg_chars:.0f} chars each)"
    )
    if skipped:
        print(
            f"  (skipped {skipped} chunk(s) over bge-small's "
            f"{MAX_CONTEXT_TOKENS}-token context -- see MAX_CONTEXT_TOKENS)"
        )

    print("\n=== fastembed CPU (BAAI/bge-small-en-v1.5) ===")
    local = run_local(texts)
    print(
        f"  {local['seconds']:.2f}s -> {local['texts_per_sec']:.1f} texts/s "
        f"(dim={local['dim']})"
    )

    print(f"\n=== remote {model} @ {base_url} (llama-server, Vulkan) ===")
    remote_results = {}
    for conc in concurrency_levels:
        r = run_remote(texts, base_url, model, batch_size, conc)
        remote_results[conc] = r
        speedup = r["texts_per_sec"] / local["texts_per_sec"]
        print(
            f"  concurrency={conc:<3} {r['seconds']:.2f}s -> "
            f"{r['texts_per_sec']:.1f} texts/s (dim={r['dim']}, "
            f"{speedup:.2f}x vs fastembed CPU)"
        )

    return {
        "base_url": base_url,
        "model": model,
        "n_texts": len(texts),
        "avg_chars": avg_chars,
        "batch_size": batch_size,
        "local_fastembed_cpu": local,
        "remote_by_concurrency": {str(k): v for k, v in remote_results.items()},
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--model", default="bge-small-en-v1.5-q8_0")
    ap.add_argument("--concurrency", type=int, nargs="+", default=DEFAULT_CONCURRENCY)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument(
        "--n-texts", type=int, default=0, help="0 = use every collected chunk"
    )
    ap.add_argument("--json", type=Path, default=None, help="dump full results here")
    args = ap.parse_args(argv)

    result = run(
        args.base_url, args.model, args.concurrency, args.batch_size, args.n_texts
    )

    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
        print(f"\nWrote full results to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
