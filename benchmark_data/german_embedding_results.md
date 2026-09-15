# German retrieval-quality benchmark (BGB)

> Part of the [embedding evaluation summary](embedding_evaluation_summary.md).
> Answers [jztan/pdf-mcp#46](https://github.com/jztan/pdf-mcp/issues/46): the
> original numbers (bge-small MRR 0.075 on natural-language German queries,
> vs 0.266 for a remote `bge-m3`) came from a private German commentary that
> can't be shared. This is the same style of benchmark on a document anyone
> can download.

## A note on process

The first version of this benchmark (n=90 sampled norms, 29 usable
natural-language pairs) had two real methodology bugs, both caught before
anything was posted: an independent review found that (a) roughly 14% of
the sampled cross-references actually cited a *different* statute (the BGB
cites other codes constantly — "§ 109 der Zivilprozessordnung" is not BGB
§ 109), mislabeling those scenarios, and (b) the natural-language query text
is lifted verbatim from a *citing* page, so a model that retrieves that
citing page back was being scored as a miss even though finding the
sentence's own source is not a wrong answer. Both depressed every model's
MRR by an unknown, model-dependent amount, and (b) especially could have
distorted the *ranking* between models, not just the absolute numbers. Both
are fixed in `scripts/gen_german_ground_truth.py` (see its git history for
detail); the numbers below are from the corrected corpus, at 300 sampled
norms this time (a bigger sample was cheap once available, per
`docs/contributing.md`'s "Quality loop" — small-sample benchmarks overstate
the gap). A second review pass found and fixed four more correctness bugs in
the generator and the harness (word-boundary citation matching, a
sentence-split truncation on legal abbreviations, an extent-ordering bug on
duplicate norm numbers, and two silent-failure paths in the harness) — see
the same commit history.

A third, pre-merge review found that scoring `semantic_xref` against
`relevant_pages` (which includes the referrer page the query sentence was
lifted from, alongside the actual norm) mostly rewards finding that
referrer sentence back: keyword-only search alone scores 0.858 on it, with
86 of 119 top hits landing on the referrer rather than the cited norm.
Retrieving the referrer is not *wrong* — the corpus generator deliberately
counts it as relevant, see step 2 below — but it is a much easier target
than the norm itself, so it inflates every model's headline number by a
roughly model-independent amount. `scripts/gen_german_ground_truth.py`
already emits both page sets per scenario (`target_pages` = norm only,
`relevant_pages` = norm + referrer); `scripts/benchmark_embedding_models.py`
now has a `--score-pages {relevant,target}` flag, and **target-only is the
headline below**, with the original `relevant_pages` numbers kept as a
secondary table for context.

## Corpus and method

Ground truth is derived mechanically from
[gesetze-im-internet.de's BGB.pdf](https://www.gesetze-im-internet.de/bgb/BGB.pdf)
(490 pages, born-digital) — **no manual annotation, no LLM**. Method, in
`scripts/gen_german_ground_truth.py`:

1. The PDF outline lists every norm as `§ N Rubrik` with a resolvable page
   (2,510 such entries after dropping `(weggefallen)`/repealed norms and
   `§§ N bis M` ranges). Each norm's own citation and rubric are verified to
   actually occur on its anchor page (word-boundary matched) before it's
   trusted.
2. The body text cites other norms constantly (~10 `§`-citations per page).
   For each sampled norm, one citing page elsewhere in the document supplies
   a natural-language query: the sentence leading up to the citation, with
   every `§`, `Absatz`/`Abs.`/`Satz`/`Nr.` token and bare digit stripped out,
   wrapped in a fixed German question frame (`"Was gilt für …?"` /
   `"Wo ist … geregelt?"`). This carries no verbatim citation for keyword
   search to win on for free. Citations to a *different* statute are
   filtered out (59 of them, across the whole document). The referrer page
   itself counts as relevant alongside the cited norm's own page(s) — the
   query text came from there, so retrieving it is a correct answer, not a
   miss.
3. A second, deliberately easy arm per norm uses the rubric verbatim as a
   keyword-style query — the **control** that proves the corpus itself
   isn't broken, independent of embedding model.

300 norms sampled (`random.Random(20260913)`, stratified across the BGB's
five *Bücher*, `§ 611a` force-included per the issue discussion). 119
produced a usable natural-language pair after the anti-triviality filters (a
citation too close to its own target extent, an empty clause, a query that
mostly restated the rubric, or a citation to a different statute are all
rejected rather than silently re-rolled — see skip counts in
`benchmark_data/german_ground_truth_provenance.json`); every sampled norm
gets the keyword-control arm regardless (300 scenarios), so the committed
ground truth carries **419 scenarios** total.

Reproduce:
```
python scripts/gen_german_ground_truth.py           # regenerates the ground truth (checks the sha256 pin first)
python scripts/benchmark_embedding_models.py \
  --ground-truth benchmark_data/german_ground_truth.json \
  --mode semantic --arms semantic_xref --score-pages target \
  --models BAAI/bge-small-en-v1.5,<candidate> --baseline BAAI/bge-small-en-v1.5
# --mode auto instead of semantic for the hybrid rows below.
```

## Keyword control arm (sanity check)

```
python scripts/benchmark_embedding_models.py \
  --ground-truth benchmark_data/german_ground_truth.json \
  --mode keyword --arms keyword_control
```

| Mode | MRR |
|------|-----|
| keyword, rubric queries (300 scenarios) | **0.850** |

High and model-independent, as expected — this confirms the ground truth
and German text extraction are sound. Any gap seen below on the semantic
and hybrid arms is a *model* limitation, not a corpus problem.

**Note on FTS language:** the benchmark harness's `PDFCache` is built
without `fts_language`, so the keyword/hybrid arms above run FTS5 with its
default (non-German) stemmer, not the `[fts] language = "de"` config a
real German deployment would set. This likely *understates* the
keyword/hybrid numbers throughout this document.

## Semantic and hybrid arms — 119 scenarios, scored on `target_pages`, `k=5`

All runs were sequential (never overlapping) on an otherwise idle machine.
Each row below is a fresh cold-embed run (`--score-pages target`); bge-small
reproduced the same MRR (0.164 semantic, 0.222 hybrid) across every pairing
below, a good reproducibility signal. CIs are a paired bootstrap (2000
resamples) of MRR lift vs. `BAAI/bge-small-en-v1.5`, matched per scenario —
see `compute_ci_vs_baseline` in `scripts/benchmark_embedding_models.py`.

### Local (fastembed, CPU) — reproducible by anyone with this repo

| Model | Semantic MRR | Hybrid MRR | Δ hybrid vs bge-small | 95% CI (hybrid) | p50 (hybrid) | Latency ratio | Cold embed (490p) | Dim |
|-------|--------------|------------|------------------------|------------------|--------------|----------------|--------------------|-----|
| `BAAI/bge-small-en-v1.5` *(default, English)* | 0.164 | 0.222 | — | — | 121.2 ms | 1.00x | 139 s | 384 |
| `intfloat/multilingual-e5-large` | **0.268** | **0.297** | **+0.075** | [+0.011, +0.139] | 162.1 ms | 1.34x | 819 s | 1024 |
| `jinaai/jina-embeddings-v2-base-de` | **0.247** | **0.295** | **+0.073** | [+0.014, +0.132] | 161.6 ms | 1.33x | 360 s | 768 |
| `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | 0.184 | 0.282 | +0.060 | [+0.002, +0.117] | 135.4 ms | 1.12x | 221 s | 768 |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 0.163 | 0.253 | +0.031 | [−0.032, +0.098] (includes zero) | 129.6 ms | 1.07x | 49 s | 384 |

Keyword search alone on this same arm's natural-language queries scores
0.222 MRR — identical to bge-small's hybrid MRR, which is expected since
hybrid mode folds a keyword pass into its ranking. Scored on the looser
`relevant_pages` (referrer counts as a hit too), keyword alone reaches
0.858, with 86 of 119 top hits landing on the referrer page rather than the
norm — the gap this document's headline scoring now closes. Semantic-only
referrer-top-1 counts (how often each model's #1 hit is the referrer
rather than the target, before hybrid's keyword boost): bge-small 21/119,
e5-large 45/119, jina-de 38/119, mpnet 12/119, MiniLM 18/119.

**`jina-embeddings-v2-base-de` — the model the issue specifically asked
about — works locally**, with a one-line workaround. It fails to load
under fastembed's hardcoded `onnxruntime.GraphOptimizationLevel
.ORT_ENABLE_ALL` (a `graph_utils.cc` assertion in the
`SimplifiedLayerNormFusion` pass, reproducible standalone with the raw ONNX
file), but loads and embeds correctly at `ORT_ENABLE_EXTENDED` (one level
down) or lower. `scripts/benchmark_embedding_models.py
--patch-onnx-graph-opt` downgrades that one level, process-wide, for the
duration of the benchmark run only (never touches `src/pdf_mcp/embedder
.py`), and every number above for this model was produced with it.
Shipping this as a permanent fix for the production path would need
`embedder.py` to accept a per-model session-options override — out of
scope here, tracked as a fast-follow if `jina-de` is adopted.

**On hybrid MRR/latency, three of four candidates clear both of this
repo's existing gates (+0.05 MRR lift, ≤1.5x p50 latency): `e5-large`
(1.34x), `jina-de` (1.33x), and `mpnet` (1.12x)** — all three CIs exclude
zero, so the lift is real, not noise. Only `MiniLM` fails, and only the
MRR gate (+0.031, CI includes zero); its latency ratio (1.07x) would also
pass. **`mpnet` is the standout on the gate's own terms**: cheapest to
embed of the three passing candidates (221s vs e5-large's 819s), lowest
latency ratio, and it clears cleanly — but it is not the model either the
original issue or `jina-de`'s workaround investigation was about, so it
hasn't had the same scrutiny as the other two here. This is a real,
reproducible change to what the existing decision gate would recommend on
this corpus (`compute_verdict` would pick `e5-large` as the highest-MRR
passing challenger if all four ran together); it is presented here as a
finding, not a recommendation to change the production default — that
decision is the maintainer's, and the gate itself (see below) may not be
the right one for German. All models run raw, with **no**
`query:`/`passage:` prefix — matching pdf-mcp's actual production path
today (no prefix mechanism exists in `embedder.py`) — so these numbers
likely understate what prefix-aware models could do, at the cost of the
extra machinery `benchmark_data/e5_prefix_results.md` already found
net-negative for English. Hybrid-mode numbers also depend on
`confidence_threshold`, pinned here to `server.py`'s current default; a
future PR making that configurable should not be read as a regression
against these numbers. The latency gate itself is tuned for the *English*
arxiv corpus's decision, not written with German in mind, so whether it
should bind the same way here is a real open question.

`MiniLM` is also markedly cheaper to embed than the default (49s vs 139s
cold) — worth keeping in mind as a budget option even though its lift
doesn't clear the MRR gate.

### Secondary: scored on `relevant_pages` (referrer counts as a hit), semantic only

Kept for context on why the original arm design counted the referrer page
as relevant at all (see "A note on process" above) — not the headline,
per the referrer-leak discussion above.

| Model | MRR | Δ vs bge-small |
|-------|-----|-----------------|
| `BAAI/bge-small-en-v1.5` *(default, English)* | 0.383 | — |
| `intfloat/multilingual-e5-large` | 0.632 | +0.249 |
| `jinaai/jina-embeddings-v2-base-de` | 0.565 | +0.182 |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 0.340 | −0.043 |
| `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | 0.313 | −0.070 |

## English regression check

The same harness, unmodified corpus (`benchmark_data/ground_truth.json`),
`BAAI/bge-small-en-v1.5` only — confirms the `--models`/`--mode`/`--arms`
additions to `scripts/benchmark_embedding_models.py` don't change the
existing English default's measured quality.

| Model | MRR | p50 latency |
|-------|-----|-------------|
| `BAAI/bge-small-en-v1.5` *(default)* | 0.726 | 61.6 ms |

**Running this exposed a real, pre-existing bug, unrelated to anything in
this change**: `ground_truth.json` on `develop` carries two PDF entries
("bert", "resnet") with an empty `"scenarios": {}` — added in a prior
commit reserving them for a corpus this harness doesn't yet consume — and
`run_model`'s per-PDF warm-up loop called `next(iter(pdf["scenarios"]))`
unconditionally, crashing with `StopIteration`. Verified against
`origin/develop`'s own unmodified `scripts/benchmark_embedding_models.py`
and its own committed `ground_truth.json` in an isolated worktree — this
crash predates this branch and would hit anyone running the harness today
with no arguments. Fixed alongside (skip a PDF with no scenarios in the
warm-up loop; the latency probe now picks the first PDF that actually has
a warmed query), with regression tests.

The MRR above (0.726) differs from `docs/embedding-models.md`'s committed
table (0.806, dated 2026-05-09) — expected drift since that run, not
something this change touches.
