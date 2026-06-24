#!/usr/bin/env python
"""
scripts/benchmark_minilm_mlx.py

Tests the "MLX + a small mean-pooled model" idea end to end. The MLX backend
mean-pools every model, which is wrong for the CLS-pooled default bge. Pairing
MLX with a natively mean-pooled model removes the mismatch — but only pays off
if that model is small AND retrieves as well as bge.

Candidate: sentence-transformers/all-MiniLM-L6-v2 (384-dim, ~92 MB, mean pooled).

Three measurements:
  1. Pooling + equivalence — prove fastembed pools MiniLM by mean (so MLX, which
     also mean-pools, is geometrically equivalent: cosine ~1.0).
  2. Latency — fastembed/CPU vs MLX/GPU encode throughput.
  3. Retrieval MRR vs bge — both models on the 22-scenario e5_prefix_corpus,
     via the production fastembed path (quality is backend-independent once
     equivalence in #1 holds). Answers the previously-unmeasured bge-vs-X gap.

No production code changed.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np  # noqa: E402

import pdf_mcp.embedder as embedder  # noqa: E402

from benchmark_embedding_models import (  # noqa: E402
    SCENARIO_K,
    load_ground_truth,
    run_model,
)
from benchmark_mlx_backend import (  # noqa: E402
    _Tee,
    _l2norm,
    build_corpus,
    mlx_encode,
    save_results,
    time_encode,
)

MINILM = "sentence-transformers/all-MiniLM-L6-v2"
BGE = "BAAI/bge-small-en-v1.5"

ATTR_TEXTS = [
    "The Transformer uses multi-head self-attention instead of recurrence.",
    "Residual connections let very deep networks train without degradation.",
    "Adam combines momentum with per-parameter adaptive learning rates.",
    "BERT is pre-trained with a masked language modelling objective.",
    "Dropout regularizes neural networks by randomly zeroing activations.",
]


def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if sys.stdout.isatty() else t


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def bold(t: str) -> str:
    return _c("1", t)


def _rowcos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sum(_l2norm(a) * _l2norm(b), axis=1).mean())


def attribution() -> dict:
    from mlx_embeddings.utils import load
    import mlx.core as mx

    model, tok = load(MINILM)
    inp = tok.batch_encode_plus(
        ATTR_TEXTS, return_tensors="mlx", padding=True, truncation=True, max_length=512
    )
    out = model(inp["input_ids"], attention_mask=inp["attention_mask"])
    mx.eval(out.last_hidden_state, out.text_embeds)

    lhs = np.array(out.last_hidden_state, dtype=np.float32)
    mask = np.array(inp["attention_mask"], dtype=np.float32)
    cls_pool = lhs[:, 0, :]
    m = mask[:, :, None]
    mean_pool = (lhs * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)

    mlx_te = np.array(out.text_embeds, dtype=np.float32)
    fe = np.asarray(embedder.encode(ATTR_TEXTS, MINILM), dtype=np.float32)

    print(bold("\n  [1] Pooling + MLX/fastembed equivalence"))
    print(f"      fastembed vs CLS-pooled  : {_rowcos(fe, cls_pool):.4f}")
    print(f"      fastembed vs mean-pooled : {_rowcos(fe, mean_pool):.4f}")
    fe_cls, fe_mean = _rowcos(fe, cls_pool), _rowcos(fe, mean_pool)
    print(f"      → fastembed pools by: {bold('mean' if fe_mean > fe_cls else 'CLS')}")
    print(f"      fastembed raw norm       : {np.linalg.norm(fe, axis=1).mean():.3f}")
    cos = _rowcos(fe, mlx_te)
    col = green if cos >= 0.999 else red
    print(
        f"      fastembed vs MLX         : {col(f'{cos:.4f}')}"
        f"   {'(MLX is correct for this model)' if cos >= 0.999 else '(DIVERGES)'}"
    )
    return {
        "fastembed_vs_cls": round(fe_cls, 4),
        "fastembed_vs_mean": round(fe_mean, 4),
        "fastembed_pools_by": "mean" if fe_mean > fe_cls else "CLS",
        "fastembed_raw_norm": round(float(np.linalg.norm(fe, axis=1).mean()), 3),
        "fastembed_vs_mlx": round(cos, 4),
    }


def latency() -> dict:
    corpus = build_corpus()
    fe_ms = time_encode(embedder.encode, corpus, MINILM)
    ml_ms = time_encode(mlx_encode, corpus, MINILM)
    n = len(corpus)
    speed = fe_ms / ml_ms
    col = green if speed >= 1.3 else red
    print(bold(f"\n  [2] Latency  ({n} chunks, warm)"))
    print(f"      fastembed/CPU : {fe_ms:7.1f} ms  ({n / fe_ms * 1000:5.1f} chunks/s)")
    print(
        f"      mlx/GPU       : {ml_ms:7.1f} ms  ({n / ml_ms * 1000:5.1f} chunks/s)"
        f"   {col(f'{speed:.2f}x vs CPU')}"
    )
    return {
        "n_chunks": n,
        "fastembed_ms": round(fe_ms, 1),
        "mlx_ms": round(ml_ms, 1),
        "speedup_vs_cpu": round(speed, 3),
    }


def retrieval() -> dict:
    gt = load_ground_truth("benchmark_data/e5_prefix_corpus.json")
    sids: set[str] = set()
    for pdf in gt["pdfs"].values():
        sids.update(pdf["scenarios"].keys())
    scenario_k = {sid: SCENARIO_K.get(sid, 5) for sid in sids}

    print(bold("\n  [3] Retrieval MRR on the 22-scenario corpus (fastembed path)"))
    mrr = {}
    for name in [BGE, MINILM]:
        r = run_model(name, gt, scenario_k)
        mrr[name] = r["mrr"]
        print(f"      {name:<46} MRR {r['mrr']:.3f}")
    delta = mrr[MINILM] - mrr[BGE]
    col = green if delta >= 0 else red
    print(
        f"      → MiniLM vs bge: {col(f'{delta:+.3f}')}"
        f"   ({'no quality loss' if delta >= -0.02 else 'quality regression'})"
    )
    return {
        "bge_mrr": round(mrr[BGE], 3),
        "minilm_mrr": round(mrr[MINILM], 3),
        "delta_vs_bge": round(delta, 3),
    }


def main() -> None:
    tee = _Tee(sys.stdout)
    with contextlib.redirect_stdout(tee):
        print(bold("\npdf-mcp MiniLM + MLX evaluation"))
        print("─" * 66)
        print(f"  Candidate: {MINILM}")
        data = {
            "benchmark": "minilm_mlx",
            "candidate": MINILM,
            "baseline": BGE,
            "equivalence": attribution(),
            "latency": latency(),
            "retrieval": retrieval(),
        }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = save_results("minilm_mlx", data, file_timestamp=ts, text=tee.getvalue())
    print(f"\n  Saved: {path}  (+ .txt)")


if __name__ == "__main__":
    main()
