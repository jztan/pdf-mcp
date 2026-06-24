#!/usr/bin/env python
"""
scripts/benchmark_mlx_backend.py

Benchmark-first evaluation of an optional MLX (Apple-GPU) embedding backend,
BEFORE adopting it in production.

MLX is a *performance* backend, not a quality change: the same E5 prefixes are
applied in both paths, so retrieval quality should be identical. The right
axes to judge it on are therefore:

  1. OUTPUT EQUIVALENCE — does the MLX path produce the same embedding geometry
     as fastembed/ONNX for the SAME model? The MLX path mean-pools every BERT/XLM-R
     model. fastembed's default `bge-small-en-v1.5` uses CLS pooling, so the two
     backends may DIVERGE for the default model even though both L2-normalize.
     If they diverge, switching backends silently changes retrieval results.
  2. LATENCY — cold-cache embedding throughput. pdf-mcp embeds every page on the
     first search of a PDF, so encode throughput is the user-visible cost.

This script implements the MLX encode path faithfully (single
batch_encode_plus, mean-pooled text_embeds) and compares against the existing
fastembed path. NO production code is changed.

Run (downloads MLX weights on first use):
    python scripts/benchmark_mlx_backend.py
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np  # noqa: E402

import pdf_mcp.embedder as embedder  # noqa: E402

MODELS = [
    {
        "name": "BAAI/bge-small-en-v1.5",
        "dim": 384,
        "note": "DEFAULT (CLS pooling in fastembed)",
    },
    {
        "name": "intfloat/multilingual-e5-large",
        "dim": 1024,
        "note": "E5 (mean pooling in fastembed 0.8)",
    },
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


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def save_results(
    name: str,
    data: dict,
    *,
    file_timestamp: str,
    text: str = "",
    out_dir: str = "benchmark_results",
) -> Path:
    """Write `<name>_<file_timestamp>.json` (+ ANSI-stripped `.txt` console log).

    Returns the JSON path. Mirrors benchmark_embedding_models.py's
    `benchmark_results/` convention (gitignored run artifacts), so MLX runs are
    self-documenting instead of relying on a hand-curated markdown doc.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = out / f"{name}_{file_timestamp}"
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if text:
        base.with_suffix(".txt").write_text(_strip_ansi(text), encoding="utf-8")
    return json_path


class _Tee(io.StringIO):
    """Write-through buffer: echoes to a real stream while recording text."""

    def __init__(self, real: object) -> None:
        super().__init__()
        self._real = real

    def write(self, s: str) -> int:  # type: ignore[override]
        self._real.write(s)  # type: ignore[attr-defined]
        return super().write(s)


def build_corpus(max_chunks: int = 60) -> list[str]:
    """Extract realistic page-text chunks from the local PDFs."""
    import fitz  # type: ignore[import-untyped]

    chunks: list[str] = []
    for path in PDFS:
        if not Path(path).is_file():
            continue
        doc = fitz.open(path)
        for page in doc:
            txt = " ".join(page.get_text().split())
            if len(txt) > 200:  # skip near-empty pages
                chunks.append(txt[:2000])
        doc.close()
    return chunks[:max_chunks]


# ── MLX encode path (single batch, mean-pooled text_embeds) ──────────
_mlx_cache: dict[str, tuple] = {}


def mlx_encode(texts: list[str], model_name: str) -> np.ndarray:
    from mlx_embeddings.utils import load  # type: ignore[import-untyped]
    import mlx.core as mx  # type: ignore[import-untyped]

    if model_name not in _mlx_cache:
        _mlx_cache[model_name] = load(model_name)
    model, tokenizer = _mlx_cache[model_name]
    inputs = tokenizer.batch_encode_plus(
        list(texts),
        return_tensors="mlx",
        padding=True,
        truncation=True,
        max_length=512,
    )
    outputs = model(inputs["input_ids"], attention_mask=inputs["attention_mask"])
    embeds = outputs.text_embeds
    mx.eval(embeds)
    return np.array(embeds, dtype=np.float32)


def time_encode(fn, texts: list[str], model_name: str, runs: int = 3) -> float:
    """Median wall-clock encode time (ms) over `runs`, model already warm."""
    fn(texts[:2], model_name)  # warm
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn(texts, model_name)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return samples[len(samples) // 2]


def _l2norm(mat: np.ndarray) -> np.ndarray:
    return mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)


def equivalence(fe: np.ndarray, mlx: np.ndarray) -> dict:
    """True cosine between matched fastembed/MLX vectors + ranking overlap.

    Normalizes both sides explicitly first — do NOT assume either backend
    returns unit vectors (fastembed's e5 path in 0.8 does not). Also reports
    each backend's raw norms so an unnormalized path is visible, not hidden.
    """
    fe_norm_raw = float(np.linalg.norm(fe, axis=1).mean())
    mlx_norm_raw = float(np.linalg.norm(mlx, axis=1).mean())

    fen, mln = _l2norm(fe), _l2norm(mlx)
    rowcos = np.sum(fen * mln, axis=1)

    # Retrieval-ranking check on normalized vectors: first 5 rows as queries,
    # compare top-5 neighbour sets under each backend.
    def topk_sets(mat: np.ndarray, k: int = 5) -> list[set]:
        sims = mat @ mat.T
        np.fill_diagonal(sims, -1)
        return [set(np.argsort(-sims[i])[:k]) for i in range(min(5, len(mat)))]

    fe_sets, mlx_sets = topk_sets(fen), topk_sets(mln)
    overlaps = [len(a & b) / len(a) for a, b in zip(fe_sets, mlx_sets) if a]
    return {
        "fe_norm_raw": fe_norm_raw,
        "mlx_norm_raw": mlx_norm_raw,
        "rowcos_mean": float(rowcos.mean()),
        "rowcos_min": float(rowcos.min()),
        "rowcos_p10": float(np.percentile(rowcos, 10)),
        "frac_ge_099": float((rowcos >= 0.99).mean()),
        "rank_overlap_at5": float(np.mean(overlaps)) if overlaps else 0.0,
    }


def main() -> None:
    tee = _Tee(sys.stdout)
    results: list[dict] = []
    corpus: list[str] = []
    with contextlib.redirect_stdout(tee):
        print(bold("\npdf-mcp MLX backend benchmark"))
        print("─" * 70)
        corpus = build_corpus()
        print(f"  Corpus: {len(corpus)} real page-text chunks from {len(PDFS)} PDFs")
        print("  Backends: fastembed/ONNX-CPU  vs  mlx-embeddings/Apple-GPU\n")
        results = _run_models(corpus)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = {
        "benchmark": "mlx_backend",
        "comparison": "fastembed/ONNX-CPU vs mlx-embeddings/Apple-GPU",
        "n_chunks": len(corpus),
        "models": results,
    }
    path = save_results("mlx_backend", data, file_timestamp=ts, text=tee.getvalue())
    print(f"  Saved: {path}  (+ .txt)")


def _run_models(corpus: list[str]) -> list[dict]:
    results: list[dict] = []
    for m in MODELS:
        name = m["name"]
        print(bold(f"  ── {name}  ({m['note']}) ──"))

        # fastembed (production path)
        t0 = time.perf_counter()
        fe = np.asarray(embedder.encode(corpus, name), dtype=np.float32)
        fe_load_plus = (time.perf_counter() - t0) * 1000

        # MLX (candidate path)
        t0 = time.perf_counter()
        ml = mlx_encode(corpus, name)
        ml_load_plus = (time.perf_counter() - t0) * 1000

        # Latency (warm model, median of 3)
        fe_ms = time_encode(embedder.encode, corpus, name)
        ml_ms = time_encode(mlx_encode, corpus, name)

        eq = equivalence(fe, ml)
        n = len(corpus)
        print(f"    Latency (warm, {n} chunks):")
        print(f"      fastembed/CPU : {fe_ms:7.1f} ms  ({n/fe_ms*1000:5.1f} chunks/s)")
        speed = fe_ms / ml_ms
        col = green if speed >= 1.3 else (yellow if speed >= 0.9 else red)
        print(
            f"      mlx/GPU       : {ml_ms:7.1f} ms  ({n/ml_ms*1000:5.1f} chunks/s)"
            f"   {col(f'{speed:.2f}x vs CPU')}"
        )
        print(
            f"      (first-call incl. load: fastembed {fe_load_plus:.0f} ms, "
            f"mlx {ml_load_plus:.0f} ms)"
        )

        print("    Output equivalence (fastembed vs MLX, same model, raw text):")
        fe_n, mlx_n = eq["fe_norm_raw"], eq["mlx_norm_raw"]
        fe_n_s = green(f"{fe_n:.3f}") if abs(fe_n - 1) < 0.05 else red(f"{fe_n:.3f}")
        mlx_n_s = (
            green(f"{mlx_n:.3f}") if abs(mlx_n - 1) < 0.05 else red(f"{mlx_n:.3f}")
        )
        print(
            f"      raw vector norm : fastembed {fe_n_s}  mlx {mlx_n_s}"
            f"   (≠1.0 ⇒ unnormalized backend)"
        )
        cos = eq["rowcos_mean"]
        ccol = green if cos >= 0.99 else (yellow if cos >= 0.9 else red)
        print(
            f"      row-wise cosine : mean {ccol(f'{cos:.4f}')}  "
            f"min {eq['rowcos_min']:.4f}  p10 {eq['rowcos_p10']:.4f}"
        )
        print(f"      frac ≥ 0.99     : {eq['frac_ge_099']*100:.0f}%")
        ro = eq["rank_overlap_at5"]
        rcol = green if ro >= 0.9 else (yellow if ro >= 0.7 else red)
        print(
            f"      rank overlap@5  : {rcol(f'{ro:.2f}')}"
            f"   (1.0 = identical neighbour ranking)"
        )
        is_equivalent = cos >= 0.99 and eq["rank_overlap_at5"] >= 0.9
        verdict = (
            green("EQUIVALENT")
            if is_equivalent
            else red("DIVERGES — backend swap changes retrieval")
        )
        print(f"      verdict         : {verdict}\n")

        results.append(
            {
                "model": name,
                "note": m["note"],
                "fastembed_ms": round(fe_ms, 1),
                "mlx_ms": round(ml_ms, 1),
                "speedup_vs_cpu": round(speed, 3),
                "equivalence": {k: round(v, 5) for k, v in eq.items()},
                "is_equivalent": bool(is_equivalent),
            }
        )
    return results


if __name__ == "__main__":
    main()
