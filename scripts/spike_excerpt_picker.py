"""Block-picker variants raced on the excerpt-miss failure set (Phase 3).

Root causes measured on the 7 true EXCERPT MISSes (see
`grouped_response_verdict.md` era forensics, 2026-07-28):
  RC1  ties on distinct-token count break by document order, so the
       span-bearing abstract loses to an earlier title/caption block
  RC2  substring matching is hyphen-blind ('pretraining' cannot match
       'pre-training'), zeroing the span block's score
  RC3  the objective is query-side; the answer span is not visible to it

Variants (cumulative where noted):
  V0  baseline: exact reimplementation of get_best_paragraph_for_query
  V1  RC2 fix: hyphen-folded matching (both token and text)
  V2  V1 + tie-break by total token occurrences
  V3  V1 + tie-break by sentence count (prose-ness)
  V4  V1 + tie-break by block length

Metric: on each described question's span-bearing gold page, does the
variant's chosen block contain the answer span (block-level fidelity
proxy for the semantic-excerpt upgrade path)? Reported for the 7 known
misses and the other 18 (do-no-harm within the set). The end-to-end
gates (benchmark_excerpt_quality.py, both fidelity arms, 10-K corpus)
run after a winner emerges, before any src/ change ships.

Free and deterministic.

Run:  uv run python scripts/spike_excerpt_picker.py
"""

from __future__ import annotations

import json
import re
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

DATA = REPO / "benchmark_data" / "corpus_search"
MISS_IDS = {
    "described-01",
    "described-02",
    "described-05",
    "described-06",
    "described-13",
    "described-14",
    "described-23",
}


def fold(s: str) -> str:
    return s.lower().replace("-", "")


def pick(
    blocks: list[str],
    tokens: list[str],
    variant: str,
    wants_figure: bool,
    figure_re: re.Pattern,
) -> int | None:
    """Return the chosen block index under `variant`'s rules."""
    hyphen_fold = variant != "V0"
    best_key: tuple = ()
    best_idx: int | None = None
    for idx, raw in enumerate(blocks):
        text = fold(raw) if hyphen_fold else raw.lower()
        toks = [fold(t) for t in tokens] if hyphen_fold else tokens
        score = sum(1 for t in toks if t in text)
        if score == 0:
            continue
        carries = 1 if wants_figure and figure_re.search(raw) else 0
        if variant in ("V0", "V1"):
            key: tuple = (score, carries)
        elif variant == "V2":
            occurrences = sum(text.count(t) for t in toks)
            key = (score, carries, occurrences)
        elif variant == "V3":
            sentences = raw.count(". ") + raw.count(".\n")
            key = (score, carries, sentences)
        else:  # V4
            key = (score, carries, len(raw.strip()))
        # strict > keeps document-order ties exactly like the shipped code
        if best_idx is None or key > best_key:
            best_key = key
            best_idx = idx
    return best_idx


def main() -> int:
    import pymupdf

    from diagnose_excerpt_fidelity import norm
    from pdf_mcp.extractor import (
        _FIGURE_RE,
        _PARAGRAPH_MAX_CHARS,
        _query_tokens,
        _wants_a_figure,
    )

    man = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    path_by_id = {d["id"]: str(REPO / d["path"]) for d in man["docs"]}
    fq = {
        q["id"]: q
        for q in json.loads(
            (DATA / "fidelity_questions.json").read_text(encoding="utf-8")
        )["questions"]
    }
    emit = {
        r["id"]: r["old_query"]
        for r in json.loads(
            (DATA / "c2_rewrite" / "caller_eval_results.json").read_text(
                encoding="utf-8"
            )
        )["rows"]
        if r["class"] == "described" and r.get("old_query")
    }

    variants = ("V0", "V1", "V2", "V3", "V4")
    results: dict[str, dict[str, bool]] = {v: {} for v in variants}
    for qid, q in sorted(fq.items()):
        query = emit[qid]
        span = norm(q["answer_span"])
        doc = pymupdf.open(path_by_id[q["expect_doc"]])
        # evaluate on the first page that actually carries the span
        span_page = None
        for pno in range(min(6, len(doc))):
            if span in norm(doc[pno].get_text()):
                span_page = pno
                break
        if span_page is None:
            for v in variants:
                results[v][qid] = False
            doc.close()
            continue
        page = doc[span_page]
        blocks = [b[4] for b in page.get_text("blocks", sort=True) if b[6] == 0]
        tokens = _query_tokens(query)
        wants = _wants_a_figure(query)
        for v in variants:
            idx = pick(blocks, tokens, v, wants, _FIGURE_RE)
            ok = False
            if idx is not None:
                chosen = blocks[idx].strip()
                if len(chosen) <= _PARAGRAPH_MAX_CHARS:
                    ok = span in norm(chosen)
            results[v][qid] = ok
        doc.close()

    print(f"{'variant':<9}{'7 misses fixed':>15}{'18 oks kept':>13}{'total':>7}")
    oks = [q for q in fq if q not in MISS_IDS]
    for v in variants:
        fixed = sum(1 for q in MISS_IDS if results[v][q])
        kept = sum(1 for q in oks if results[v][q])
        total = sum(1 for q in fq if results[v][q])
        print(f"{v:<9}{fixed:>10}/7{kept:>10}/18{total:>5}/25")
    print("\nper-miss detail (1 = picked block carries the span):")
    print(f"{'id':<15}" + "".join(f"{v:>5}" for v in variants))
    for qid in sorted(MISS_IDS):
        print(f"{qid:<15}" + "".join(f"{int(results[v][qid]):>5}" for v in variants))
    out = DATA / "excerpt_picker_variants.json"
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
