#!/usr/bin/env python
"""
scripts/author_corpus_queries.py

Draft new graded queries for benchmark_data/corpus_search/queries.json,
blind to every retrieval result.

Why: the described class has 25 queries, 12 of which no arm can answer and
all 25 of which have their gold on page 1, so a described verdict rests on
13 title-and-abstract lookups and its paired CI is about +/-0.24. Nothing
of size 0.1 can be confirmed or denied there. This script grows the set to
where a 0.1 effect resolves (about 75 scored queries per class) and moves
the gold below page 1.

Protocol (fixed before any arm runs; never revise after seeing results):
  1. Sample (doc, page) with a seeded RNG BEFORE reading the page: docs
     that carry no gold yet are weighted 3:1, page 1 is drawn at most 15%
     of the time, otherwise uniform over pages 2..n. Pages with too little
     text or that look like a reference list are skipped and the draw is
     logged as skipped, not re-rolled silently.
  2. Evidence first, query second. A `claude -p` drafter reads the raw
     pdfium text of that page (not pdf-mcp's extraction, so labels cannot
     inherit pdf-mcp's bugs), copies ONE verbatim span stating a specific
     claim, then writes the query:
       described: a paraphrase question, no proper nouns, no digits, at
                  most ONE content token in common with the span.
       needle:    a 2-4 word literal query whose terms all occur in the
                  span and in at most 3 documents of the corpus.
       spread:    two documents paired by cached head-vector cosine (the
                  seed doc's nearest neighbour), one page sampled in each;
                  the drafter names a 2-3 word topic both pages discuss and
                  copies one verbatim span per page; a query token must
                  occur in both spans.
  3. Every draft passes mechanical checks or is rejected with a reason.
     Accepted drafts go to --out as candidates for a human veto pass;
     they are NOT merged into queries.json by this script.

Cost: one claude -p call per sampled page, with JUDGE_CONTEXT_FLAGS (see
eval_financial_answerability.py) and --max-budget-usd per call. Calls are
cached in --cache keyed by (class, doc, page, seed) so a re-run is free.

Usage:
    python scripts/author_corpus_queries.py --klass described --n 80
    python scripts/author_corpus_queries.py --klass needle --n 30
    python scripts/author_corpus_queries.py --klass spread --n 45
    python scripts/author_corpus_queries.py --klass described --n 80 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
DATA = REPO / "benchmark_data" / "corpus_search"

from eval_financial_answerability import JUDGE_CONTEXT_FLAGS  # noqa: E402

DEFAULT_MODEL = "claude-opus-4-8"
PER_CALL_BUDGET_USD = "0.50"
CALL_TIMEOUT_S = 180
PAGE1_RATE = 0.15
MIN_PAGE_CHARS = 1200
PAGE_TEXT_CAP = 7000
UNLABELLED_WEIGHT = 3

_WS = re.compile(r"\s+")
_STOP = set("""a an and are as at be by for from has have how in is it its of on or
    that the this to was were what when where which with does do did can
    could would should may might than then there these those into over
    under between among about after before during while any all each more
    most much many such only also both either whether not no nor so if but
    their they them he she his her we our you your who whom whose one two
    three four five six seven eight nine ten first second third per via
    using used use across within without because since until per""".split())


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def fold(text: str) -> str:
    """NFKC, lower, alphanumerics only. pdfium emits control characters for
    some ligatures (`di\x1bers`, `general\ufffeized`) and spaces around
    punctuation differ, so a faithful copy can fail a whitespace-only
    comparison. Used only to LOCATE a span; the stored evidence is always
    the raw substring."""
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[^a-z0-9\u0080-\uffff]+", "", text)


def locate_raw(page_text: str, evidence: str) -> tuple[str, str] | None:
    """(raw substring of page_text matching evidence, how) or None.
    how is 'exact' (whitespace-normalised match) or 'folded'."""
    if normalize(evidence) in normalize(page_text):
        return evidence, "exact"
    target = fold(evidence)
    if not target:
        return None
    # map folded positions back to raw indices
    folded_chars: list[str] = []
    raw_idx: list[int] = []
    for i, ch in enumerate(page_text):
        f = fold(ch)
        for fc in f:
            folded_chars.append(fc)
            raw_idx.append(i)
    folded = "".join(folded_chars)
    j = folded.find(target)
    if j < 0:
        return None
    start = raw_idx[j]
    end = raw_idx[j + len(target) - 1] + 1
    return page_text[start:end], "folded"


def content_tokens(text: str) -> set[str]:
    toks = re.findall(r"[a-z][a-z\-]+", text.lower())
    return {t[:6] for t in toks if t not in _STOP and len(t) > 2}


# ── corpus access ─────────────────────────────────────────────────────


def raw_page_text(pdf_path: Path, page_num_0: int) -> str:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc[page_num_0]
        tp = page.get_textpage()
        try:
            return tp.get_text_range()
        finally:
            tp.close()
            page.close()
    finally:
        doc.close()


def page_count(pdf_path: Path) -> int:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(doc)
    finally:
        doc.close()


def looks_like_references(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    bracketed = sum(1 for ln in lines if re.match(r"\s*\[\d+\]", ln))
    etal = len(re.findall(r"\bet al\b", text))
    return bracketed > 0.25 * len(lines) or etal > 12


# ── drafting ──────────────────────────────────────────────────────────


def draft_prompt(klass: str, text: str) -> str:
    head = (
        "You are authoring one graded retrieval query for a benchmark. Below "
        "is the raw text of one PDF page. Do NOT use any outside knowledge.\n\n"
        "Step 1: choose ONE sentence (or a clause of one) on this page that "
        "states a specific, checkable claim: a result, a number, a mechanism, "
        "a definition. Copy it VERBATIM as `evidence`: 40 to 160 characters, "
        "exactly as printed, on one line, no ellipsis, no edits.\n"
    )
    if klass == "described":
        rule = (
            "Step 2: write `query`: a natural-language question a reader would "
            "ask that this evidence answers, of at most 14 words, WITHOUT proper "
            "nouns, WITHOUT digits, and sharing AT MOST ONE content word with the "
            "evidence (paraphrase everything else: different verbs, synonyms, "
            "describe the concept instead of naming it).\n"
        )
    else:
        rule = (
            "Step 2: write `query`: 2 to 4 literal words that appear in the "
            "evidence and are distinctive of this page (a rare technical term, a "
            "named quantity, an unusual collocation), the way an agent searching "
            "for this exact sentence would type them. No stopwords.\n"
        )
    tail = (
        "Reply with ONLY a JSON object on one line: "
        '{"evidence": "...", "query": "..."}. If the page has no suitable '
        'sentence (references, figures only, boilerplate), reply {"skip": '
        '"reason"}.\n\n=== PAGE TEXT ===\n'
    )
    return head + rule + tail + text[:PAGE_TEXT_CAP]


def spread_prompt(two_pages: str) -> str:
    return (
        "You are authoring one graded cross-document retrieval query for a "
        "benchmark. Below are the raw texts of one page from each of TWO "
        "different PDFs. Do NOT use outside knowledge.\n\n"
        "Step 1: find ONE technical topic, method, quantity or concept that "
        "BOTH pages genuinely discuss (not a generic word like 'model' or "
        "'results').\n"
        "Step 2: copy ONE verbatim span from page A (`evidence_a`) and ONE from "
        "page B (`evidence_b`), 30 to 160 characters each, exactly as printed, "
        "on one line, each containing that topic's term.\n"
        "Step 3: write `query`: 2 to 3 literal words naming the topic, the way "
        "an agent would search for it across a corpus. No stopwords.\n"
        "Reply with ONLY a JSON object on one line: "
        '{"query": "...", "evidence_a": "...", "evidence_b": "..."}. If the '
        'pages share no real topic, reply {"skip": "reason"}.\n\n' + two_pages
    )


def ask(prompt: str, model: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                model,
                "--disallowedTools",
                "Bash,Read,Write,Edit,WebFetch,WebSearch",
                "--max-budget-usd",
                PER_CALL_BUDGET_USD,
                *JUDGE_CONTEXT_FLAGS,
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def parse_draft(raw: str) -> dict | None:
    """First balanced JSON object in the reply that parses (the drafter
    sometimes appends prose after the object)."""
    for m in re.finditer(r"\{[^{}]*\}", raw, re.S):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ── checks ────────────────────────────────────────────────────────────


def check_candidate(
    klass: str,
    draft: dict,
    page_text: str,
    doc_freq: dict[str, int] | None,
) -> list[str]:
    """Reasons to reject; empty means accepted."""
    reasons: list[str] = []
    ev = str(draft.get("evidence") or draft.get("evidence_a") or "").strip()
    q = str(draft.get("query", "")).strip()
    if not ev or not q:
        return ["empty"]
    if not (30 <= len(ev) <= 200):
        reasons.append(f"evidence length {len(ev)}")
    if locate_raw(page_text, ev) is None:
        reasons.append("evidence not on page (even folded)")
    if "..." in ev or "…" in ev:
        reasons.append("ellipsis in evidence")
    words = q.split()
    if klass == "described":
        if len(words) > 16:
            reasons.append(f"query {len(words)} words")
        if re.search(r"\d", q):
            reasons.append("digit in query")
        shared = content_tokens(q) & content_tokens(ev)
        if len(shared) > 1:
            reasons.append(f"shares {sorted(shared)} with evidence")
        # proper-noun heuristic: a query word that appears capitalised in the
        # page text mid-sentence (not sentence-initial) and is not a common word
        caps = {
            w.lower()
            for w in re.findall(r"(?<=[a-z,;] )([A-Z][a-zA-Z]{2,})", page_text)
        }
        pn = [
            w
            for w in words
            if w.lower().strip("?,.") in caps and w.lower() not in _STOP
        ]
        if pn:
            reasons.append(f"proper noun(s) {pn}")
    elif klass == "spread":
        if not (2 <= len(words) <= 4):
            reasons.append(f"spread query {len(words)} words")
        qtoks = content_tokens(q)
        for label, ev_text in (("a", ev), ("b", str(draft.get("evidence_b", "")))):
            if not (qtoks & content_tokens(ev_text)):
                reasons.append(f"no query token in evidence_{label}")
    else:
        if not (2 <= len(words) <= 4):
            reasons.append(f"needle query {len(words)} words")
        missing = [w for w in words if fold(w) not in fold(ev)]
        if missing:
            reasons.append(f"query words not in evidence {missing}")
        if doc_freq is not None:
            rare = min(doc_freq.get(w.lower(), 0) for w in words) if words else 0
            if rare > 3:
                reasons.append(f"least rare query word occurs in {rare} docs")
    return reasons


# ── sampling ──────────────────────────────────────────────────────────


def sample_pages(
    docs: list[dict], labelled: set[str], n: int, rng: random.Random
) -> list[tuple[str, int]]:
    weights = [UNLABELLED_WEIGHT if d["id"] not in labelled else 1 for d in docs]
    out: list[tuple[str, int]] = []
    counts: dict[str, int] = {}
    while len(out) < n:
        d = rng.choices(docs, weights=weights, k=1)[0]
        pc = page_count(REPO / d["path"])
        if counts.get(d["id"], 0) >= 2:
            continue
        if pc == 1 or rng.random() < PAGE1_RATE:
            page = 1
        else:
            page = rng.randint(2, pc)
        out.append((d["id"], page))
        counts[d["id"]] = counts.get(d["id"], 0) + 1
    return out


def nearest_neighbours(docs: list[dict]) -> dict[str, str]:
    """seed doc id -> id of its nearest other doc by cached head-vector
    cosine (the document arm's profile). Docs without a profile are
    skipped."""
    import numpy as np

    sys.path.insert(0, str(REPO / "src"))
    from pdf_mcp.server import cache, pdf_config

    # cache rows are keyed by the resolved path (benchmark dirs are symlinks
    # into the main checkout from a worktree)
    paths = {d["id"]: str((REPO / d["path"]).resolve()) for d in docs}
    profiles = cache.get_doc_profiles(list(paths.values()), pdf_config.embedding_model)
    ids = [i for i, p in paths.items() if profiles.get(p) is not None]
    if len(ids) < 2:
        return {}
    mat = np.stack([np.frombuffer(profiles[paths[i]], dtype=np.float32) for i in ids])
    sims = mat @ mat.T
    np.fill_diagonal(sims, -1.0)
    return {i: ids[int(j)] for i, j in zip(ids, sims.argmax(axis=1))}


def build_doc_freq(docs: list[dict]) -> dict[str, int]:
    """word -> number of documents whose raw text contains it (needle rarity)."""
    freq: dict[str, int] = {}
    for d in docs:
        path = REPO / d["path"]
        seen: set[str] = set()
        for pn in range(page_count(path)):
            seen.update(
                re.findall(r"[a-z][a-z\-]{2,}", raw_page_text(path, pn).lower())
            )
        for w in seen:
            freq[w] = freq.get(w, 0) + 1
    return freq


# ── main ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--klass", choices=("described", "needle", "spread"), required=True)
    ap.add_argument("--n", type=int, required=True, help="pages to sample")
    ap.add_argument("--seed", type=int, default=20260830)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="sample only, no calls")
    args = ap.parse_args(argv)

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    queries = json.loads((DATA / "queries.json").read_text(encoding="utf-8"))
    docs = [d for d in manifest["docs"] if d.get("lang", "en") == "en"]
    labelled = {lab["doc"] for q in queries["queries"] for lab in q["labels"]}
    existing = [q["id"] for q in queries["queries"] if q["class"] == args.klass]
    next_num = 1 + max(int(i.split("-")[1]) for i in existing)

    out_path = args.out or DATA / f"candidates_{args.klass}.json"
    cache_path = args.cache or DATA / f"author_cache_{args.klass}.jsonl"
    cache: dict[str, dict] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                cache[rec["key"]] = rec

    rng = random.Random(args.seed)
    samples = sample_pages(docs, labelled, args.n, rng)
    print(f"{args.klass}: {len(samples)} samples, seed {args.seed}", file=sys.stderr)
    page1 = sum(1 for _d, p in samples if p == 1)
    new_docs = sum(1 for d, _p in samples if d not in labelled)
    print(f"  page-1 draws {page1}, unlabelled-doc draws {new_docs}", file=sys.stderr)
    if args.dry_run:
        for d, p in samples:
            print(f"  {d} p{p}")
        return 0

    doc_freq = build_doc_freq(docs) if args.klass == "needle" else None
    neighbours = nearest_neighbours(docs) if args.klass == "spread" else {}
    path_by_id = {d["id"]: REPO / d["path"] for d in docs}
    accepted: list[dict] = []
    rejected: list[dict] = []
    skipped: list[dict] = []
    for idx, (doc_id, page) in enumerate(samples):
        text = raw_page_text(path_by_id[doc_id], page - 1)
        if len(text) < MIN_PAGE_CHARS or looks_like_references(text):
            skipped.append({"doc": doc_id, "page": page, "why": "thin or references"})
            continue
        partner: tuple[str, int, str] | None = None
        if args.klass == "spread":
            nb = neighbours.get(doc_id)
            if nb is None:
                skipped.append({"doc": doc_id, "page": page, "why": "no profile"})
                continue
            pc = page_count(path_by_id[nb])
            nb_page = (
                1 if (pc == 1 or rng.random() < PAGE1_RATE) else rng.randint(2, pc)
            )
            nb_text = raw_page_text(path_by_id[nb], nb_page - 1)
            if len(nb_text) < MIN_PAGE_CHARS or looks_like_references(nb_text):
                skipped.append({"doc": nb, "page": nb_page, "why": "partner thin"})
                continue
            partner = (nb, nb_page, nb_text)
        key = f"{args.klass}|{doc_id}|{page}|{args.seed}"
        if key in cache:
            raw = cache[key]["raw"]
        elif partner is not None:
            raw = ask(
                spread_prompt(
                    f"=== PAGE A ({doc_id} p{page}) ===\n{text[:PAGE_TEXT_CAP // 2]}"
                    f"\n\n=== PAGE B ({partner[0]} p{partner[1]}) ===\n"
                    f"{partner[2][:PAGE_TEXT_CAP // 2]}"
                ),
                args.model,
            )
        else:
            raw = ask(draft_prompt(args.klass, text), args.model)
        if key not in cache:
            # cache every fresh call, whichever branch made it (the spread
            # branch once skipped this and re-drafted on every run)
            rec = {
                "key": key,
                "raw": raw,
                "model": args.model,
                "partner": list(partner[:2]) if partner else None,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            with cache_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            cache[key] = rec
        draft = parse_draft(raw or "")
        if draft is None:
            rejected.append(
                {"doc": doc_id, "page": page, "why": "unparseable", "raw": raw}
            )
            continue
        if "skip" in draft:
            skipped.append(
                {"doc": doc_id, "page": page, "why": f"drafter: {draft['skip']}"}
            )
            continue
        reasons = check_candidate(args.klass, draft, text, doc_freq)
        if partner is not None:
            loc_a = locate_raw(text, str(draft.get("evidence_a", "")).strip())
            loc_b = locate_raw(partner[2], str(draft.get("evidence_b", "")).strip())
            if loc_b is None:
                reasons.append("evidence_b not on partner page (even folded)")
            ev_a, how_a = loc_a if loc_a else ("", "missing")
            ev_b, how_b = loc_b if loc_b else ("", "missing")
            labels = [
                {
                    "doc": doc_id,
                    "page": page,
                    "gain": 2,
                    "evidence": ev_a,
                    "match": how_a,
                },
                {
                    "doc": partner[0],
                    "page": partner[1],
                    "gain": 2,
                    "evidence": ev_b,
                    "match": how_b,
                },
            ]
        else:
            located = locate_raw(text, str(draft.get("evidence", "")).strip())
            ev_raw, how = located if located else ("", "missing")
            labels = [
                {
                    "doc": doc_id,
                    "page": page,
                    "gain": 2,
                    "evidence": ev_raw,
                    "match": how,
                }
            ]
        cand = {
            "id": f"{args.klass}-{next_num + len(accepted):02d}",
            "class": args.klass,
            "query": str(draft.get("query", "")).strip(),
            "labels": labels,
            "provenance": {
                "authored": "author_corpus_queries.py",
                "seed": args.seed,
                "sample_index": idx,
                "model": args.model,
                "blind": "drafted from raw pdfium page text before any arm ran",
            },
        }
        if reasons:
            rejected.append({**cand, "why": reasons})
        else:
            accepted.append(cand)
        print(
            f"  [{idx + 1}/{len(samples)}] {doc_id} p{page}: "
            f"{'ACCEPT' if not reasons else 'reject ' + '; '.join(reasons)}",
            file=sys.stderr,
        )

    out = {
        "description": (
            f"Candidate {args.klass} queries drafted blind by "
            f"author_corpus_queries.py (seed {args.seed}, model {args.model}); "
            "pending human veto; not yet merged into queries.json."
        ),
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
    }
    out_path.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"accepted {len(accepted)}, rejected {len(rejected)}, skipped {len(skipped)} "
        f"-> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
