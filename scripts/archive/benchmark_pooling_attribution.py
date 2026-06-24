#!/usr/bin/env python
"""
scripts/benchmark_pooling_attribution.py

Proves WHY the fastembed and MLX backends diverge for bge but match for e5.

The MLX-backend benchmark showed bge diverges (cosine 0.89) and e5 is equivalent
(cosine 1.0), but that only proves the *symptom*. This script proves the
*cause*: it reconstructs CLS-pooled and mean-pooled sentence vectors from the
model's own token-level `last_hidden_state`, then checks which pooling each
backend actually matches.

Expected if the pooling-mismatch theory is correct:
    fastembed(bge)  ~ CLS-pooled    (and far from mean-pooled)
    MLX(bge)        ~ mean-pooled   (and far from CLS-pooled)
    fastembed(e5)   ~ mean-pooled   (fastembed 0.8 switched e5 to mean)
    MLX(e5)         ~ mean-pooled
→ bge backends disagree because they pool differently; e5 backends agree.

No production code changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np  # noqa: E402

import pdf_mcp.embedder as embedder  # noqa: E402

TEXTS = [
    "The Transformer uses multi-head self-attention instead of recurrence.",
    "Residual connections let very deep networks train without degradation.",
    "Adam combines momentum with per-parameter adaptive learning rates.",
    "BERT is pre-trained with a masked language modelling objective.",
    "Dropout regularizes neural networks by randomly zeroing activations.",
]


def _n(x: np.ndarray) -> np.ndarray:
    return x / np.clip(np.linalg.norm(x, axis=1, keepdims=True), 1e-12, None)


def _rowcos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sum(_n(a) * _n(b), axis=1).mean())


def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if sys.stdout.isatty() else t


def main() -> None:
    from mlx_embeddings.utils import load
    import mlx.core as mx

    print("\npdf-mcp pooling attribution  (why bge diverges, e5 doesn't)")
    print("─" * 66)

    for name in ["BAAI/bge-small-en-v1.5", "intfloat/multilingual-e5-large"]:
        model, tok = load(name)
        inp = tok.batch_encode_plus(
            TEXTS, return_tensors="mlx", padding=True, truncation=True, max_length=512
        )
        out = model(inp["input_ids"], attention_mask=inp["attention_mask"])
        mx.eval(out.last_hidden_state, out.text_embeds)

        lhs = np.array(out.last_hidden_state, dtype=np.float32)  # (N, seq, H)
        mask = np.array(inp["attention_mask"], dtype=np.float32)  # (N, seq)

        cls_pool = lhs[:, 0, :]  # [CLS] token vector
        m = mask[:, :, None]
        mean_pool = (lhs * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)

        mlx_te = np.array(out.text_embeds, dtype=np.float32)  # MLX pooled output
        fe = np.asarray(embedder.encode(TEXTS, name), dtype=np.float32)  # production

        print(f"\n  ── {name} ──")
        print("    Reconstructed-pooling identity check (row cosine, normalized):")
        print(
            f"      MLX text_embeds  vs  mean-pooled : {_rowcos(mlx_te, mean_pool):.4f}"
        )
        print(
            f"      MLX text_embeds  vs  CLS-pooled  : {_rowcos(mlx_te, cls_pool):.4f}"
        )
        print("    Which pooling does each backend match?")
        fe_cls, fe_mean = _rowcos(fe, cls_pool), _rowcos(fe, mean_pool)
        print(f"      fastembed        vs  CLS-pooled  : {fe_cls:.4f}")
        print(f"      fastembed        vs  mean-pooled : {fe_mean:.4f}")
        winner = "CLS" if fe_cls > fe_mean else "mean"
        print(f"      → fastembed pools by: {_c('1', winner)}")
        print("    Cross-backend agreement (the symptom):")
        print(f"      fastembed        vs  MLX text_embeds : {_rowcos(fe, mlx_te):.4f}")


if __name__ == "__main__":
    main()
