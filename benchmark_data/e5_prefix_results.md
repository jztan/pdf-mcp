# E5 query/passage prefix evaluation

> Part of the [embedding evaluation summary](embedding_evaluation_summary.md).

Benchmark-first evaluation of a `passage:`/`query:` prefix change, run
**before** adopting it in production code.

The intfloat E5 family is trained with asymmetric instruction prefixes:
passages embedded as `passage: <text>`, queries as `query: <text>`. fastembed's
`.embed()` does not apply these. This measures whether adding them lifts
retrieval on our ground-truth corpus.

Harness: `scripts/archive/benchmark_e5_prefix.py` runs the same model two ways
(verbatim vs. prefixed) via monkeypatch — **no production code changed**. Each
run uses a fresh cache, so embeddings never leak across conditions. Metric: MRR
over all scenarios at each scenario's native k. Model:
`intfloat/multilingual-e5-large` (the only E5 model fastembed exposes).

## Results

| Corpus | PDFs | Scenarios | no-prefix MRR | prefix MRR | Δ MRR |
|--------|------|-----------|---------------|------------|-------|
| Original (`ground_truth.json`)        | 2 | 7  | 0.378 | 0.413 | **+0.035** |
| Expanded (`e5_prefix_corpus.json`)    | 5 | 22 | 0.531 | 0.485 | **−0.046** |

Decision gate: adopt iff Δ MRR ≥ +0.05. **Not met in either run**, and the sign
flips negative once the corpus is honest. Latency is a non-factor (prefixes add
~2 tokens; p50 was 0.66–0.78× baseline, i.e. within noise).

The expanded corpus adds 15 hand-verified scenarios across BERT, ResNet, and
Adam (5 each), annotated by reading actual page text — model-independent, never
via semantic search, so the ground truth is not biased toward the model under
test.

## Verdict

**Do not adopt the E5 prefix change on retrieval-quality grounds.** The
small-sample +0.035 was the classic overstated gap; on 22 scenarios the prefixes
are net-negative (−0.046). Per-scenario the effect is mixed (helps `1a`, `2b`;
hurts `b2`, `a1`, `a4`, `a5`, `1c`) and cancels out unfavourably.

This is corpus-specific (English ML papers, mostly keyword-friendly factual
queries). The prefix recipe could still matter on a multilingual or
conceptual-paraphrase corpus — but we have no evidence for it here, and E5's
absolute MRR is already below the `bge-small` default, so E5 is not a default
candidate regardless.

## Side finding (unrelated to prefixes)

fastembed 0.8 emits: *"intfloat/multilingual-e5-large now uses mean pooling
instead of CLS embedding ... pin fastembed 0.5.1 to preserve previous
behaviour."* Our `semantic` extra pins `fastembed>=0.7,<1.0`, so this pooling
change is already live for any E5 user — independent of the prefix question and
worth a separate look.
