#!/usr/bin/env python
"""
scripts/benchmark_coreml_provider.py

Benchmark fastembed's ONNX Runtime CoreML execution provider as a simpler
alternative to a separate MLX backend.

The MLX backend is fast (2.5-2.8x) but mean-pools every model, which is wrong
for the CLS-pooled default bge (cosine 0.89, 16% ranking shift). CoreML EP runs
the SAME fastembed model+pooling on the Apple GPU/Neural Engine via one
constructor arg (`providers=[...]`) — so it should be a drop-in: same pooling,
no second backend, no new dependency, no cache/pooling-parity risk.

This measures whether it (a) actually accelerates or silently falls back to CPU
for unsupported ops, and (b) stays output-equivalent to the CPU path. Compares:
  * fastembed default (CPUExecutionProvider)
  * fastembed providers=['CoreMLExecutionProvider', 'CPUExecutionProvider']

No production code changed (instantiates TextEmbedding directly to set
providers, bypassing the embedder singleton).

Run:
    python scripts/benchmark_coreml_provider.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np  # noqa: E402

MODELS = [
    {"name": "BAAI/bge-small-en-v1.5", "note": "DEFAULT (CLS pooling)"},
    {"name": "intfloat/multilingual-e5-large", "note": "E5 (mean pooling)"},
]

PDFS = [
    "/tmp/e5_pdfs/attention.pdf",
    "/tmp/e5_pdfs/bert.pdf",
    "/tmp/e5_pdfs/resnet.pdf",
    "/tmp/e5_pdfs/adam.pdf",
]


def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if sys.stdout.isatty() else t


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def bold(t: str) -> str:
    return _c("1", t)


def build_corpus(max_chunks: int = 60) -> list[str]:
    import fitz  # type: ignore[import-untyped]

    chunks: list[str] = []
    for path in PDFS:
        if not Path(path).is_file():
            continue
        doc = fitz.open(path)
        for page in doc:
            txt = " ".join(page.get_text().split())
            if len(txt) > 200:
                chunks.append(txt[:2000])
        doc.close()
    return chunks[:max_chunks]


def make_model(model_name: str, providers: list[str] | None):
    from fastembed import TextEmbedding  # type: ignore[import-untyped]

    if providers is None:
        return TextEmbedding(model_name)
    return TextEmbedding(model_name, providers=providers)


def encode(model, texts: list[str]) -> np.ndarray:
    return np.array(list(model.embed(texts)), dtype=np.float32)


def time_encode(model, texts: list[str], runs: int = 3) -> float:
    encode(model, texts[:2])  # warm
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        encode(model, texts)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return samples[len(samples) // 2]


def _l2norm(mat: np.ndarray) -> np.ndarray:
    return mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)


def main() -> None:
    print(bold("\npdf-mcp CoreML execution-provider benchmark"))
    print("─" * 66)
    corpus = build_corpus()
    print(f"  Corpus: {len(corpus)} real page-text chunks from {len(PDFS)} PDFs")
    print("  Compare: fastembed CPU  vs  fastembed CoreML EP (same pooling)\n")

    coreml = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    for m in MODELS:
        name = m["name"]
        print(bold(f"  ── {name}  ({m['note']}) ──"))

        cpu_model = make_model(name, None)
        try:
            cml_model = make_model(name, coreml)
        except Exception as e:  # noqa: BLE001
            print(f"    CoreML EP unavailable: {type(e).__name__}: {e}\n")
            continue

        cpu_vecs = encode(cpu_model, corpus)
        cml_vecs = encode(cml_model, corpus)

        cpu_ms = time_encode(cpu_model, corpus)
        cml_ms = time_encode(cml_model, corpus)

        n = len(corpus)
        speed = cpu_ms / cml_ms
        scol = green if speed >= 1.3 else (yellow if speed >= 0.95 else red)
        print(f"    Latency (warm, {n} chunks):")
        print(f"      CPU EP    : {cpu_ms:7.1f} ms  ({n/cpu_ms*1000:5.1f} chunks/s)")
        print(
            f"      CoreML EP : {cml_ms:7.1f} ms  ({n/cml_ms*1000:5.1f} chunks/s)"
            f"   {scol(f'{speed:.2f}x vs CPU')}"
        )
        if speed < 0.95:
            print(red("      → no speedup — likely fell back to CPU for these ops"))

        rowcos = np.sum(_l2norm(cpu_vecs) * _l2norm(cml_vecs), axis=1)
        ccol = (
            green
            if rowcos.mean() >= 0.999
            else (yellow if rowcos.mean() >= 0.99 else red)
        )
        print("    Output equivalence vs CPU (same pooling expected):")
        print(
            f"      row cosine : mean {ccol(f'{rowcos.mean():.5f}')}  "
            f"min {rowcos.min():.5f}"
        )
        verdict = (
            green("DROP-IN (equivalent + faster)")
            if rowcos.mean() >= 0.999 and speed >= 1.3
            else (
                green("EQUIVALENT but no speedup")
                if rowcos.mean() >= 0.999
                else red("DIVERGES — unexpected")
            )
        )
        print(f"      verdict    : {verdict}\n")


if __name__ == "__main__":
    main()
