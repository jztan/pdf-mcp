#!/usr/bin/env python
"""
scripts/eval_financial_answerability.py

Can a caller ANSWER a realistic 10-K question from what the tool returns?

The retrieval benchmark (scripts/benchmark_corpus_modes.py) asks "is the
gold page in the top 10". That is necessary but not sufficient: a page can
be retrieved while the excerpt shown quotes the neighbouring segment, and
an agent reading that excerpt reports the wrong number. Rank metrics score
such a result as a hit. This eval scores it as a failure.

Two layers, deliberately separated:

  OBJECTIVE (computed in code, no model involved)
    - doc coverage: fraction of the docs a complete answer needs that are
      represented in the payload at all
    - balance: for multi-document questions, the share held by the
      least-represented expected doc (a 9-vs-1 split cannot answer
      "compare A with B")

  JUDGED (`claude -p`, same shelling-out pattern as eval_coherence.py)
    Given the question, the HAND-VERIFIED reference facts, and the payload
    the caller would actually see, the judge reports whether the payload
    supports a correct and complete answer, and whether it contains a
    wrong-attribution trap. The judge never supplies ground truth -- it
    only compares against reference facts authored from extracted text.

Billed: one `claude -p` call per question. Not part of any test suite.

Run:  uv run python scripts/eval_financial_answerability.py
      uv run python scripts/eval_financial_answerability.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import threading

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DATA = REPO / "benchmark_data" / "financial_reports"
DEFAULT_MODEL = "claude-opus-4-8"
DENIED_TOOLS = "Bash,Read,Write,Edit,WebFetch,WebSearch"
JUDGE_TIMEOUT_S = 180
# Every ballot ever paid for, appended as it completes.
BALLOT_CACHE = DATA / "judge_ballot_cache.jsonl"

# Strip context the judge cannot use. Measured per call on this project:
# 20,704 fresh input tokens, of which the rubric and payload were 2,495 --
# the other 88% was Claude Code reloading its own scaffolding on every one
# of the hundreds of invocations a run makes.
#
#   --setting-sources ''  drops the global and project CLAUDE.md
#                         (19,853 -> 8,807 tokens; the dominant saving)
#   --strict-mcp-config   drops the tool schemas of 10 MCP servers, none of
#     + empty config      which the judge may call anyway (-11%)
#
# Deliberately NOT set: --system-prompt. Replacing the default prompt looks
# cheaper on total context but bills MORE (23,680 fresh, zero cache reads),
# because a custom prompt busts the cache prefix the default one shares
# across invocations.
#
# Dropping CLAUDE.md is also correct on the merits: a grader should not
# inherit the repo's commit conventions and prose rules while scoring
# retrieval. But it does change the judge's context, so verdicts from
# JUDGE_CONTEXT_FLAGS runs are only comparable with earlier runs once an
# agreement check on identical payloads says so.
JUDGE_CONTEXT_FLAGS = [
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
]
TOP_K = 10
FOLLOWUP_LIMIT = 3

RUBRIC = """You are grading a document-retrieval result, not writing an answer.

You are given: a QUESTION, a list of REFERENCE FACTS (verified by hand from
the source documents), and the PAYLOAD a caller received from a search tool.
The payload is all the caller sees -- treat it as the complete evidence.

Decide:

1. answerable:
   - "full"    : the payload contains enough to answer the question
                 correctly and completely, covering every reference fact
                 the question requires.
   - "partial" : the payload supports part of the answer but a required
                 piece is missing (e.g. one of three years, or one of two
                 companies in a comparison).
   - "no"      : the payload does not support answering the question.

2. wrong_attribution (true/false): does the payload prominently present a
   figure or statement that belongs to a DIFFERENT segment, company, or
   fiscal year in a position where a reader would reasonably take it as the
   answer? This is the dangerous failure -- worse than returning nothing --
   so judge it strictly. If the payload's top excerpts quote a confusable
   neighbour instead of the asked-about subject, that is true.

3. missing: brief note on what a complete answer still needs, or "" if none.

Reply with ONLY a JSON object, no prose:
{"answerable": "full|partial|no", "wrong_attribution": true|false,
 "missing": "...", "reason": "one sentence"}"""


def followup_docs(
    matches: list[dict[str, Any]], doc_match_counts: dict[str, int], limit: int
) -> list[str]:
    """Documents worth a second, scoped call, ordered most-matching first.

    Uses ONLY what the response exposes -- which documents appear in
    `matches`, and which `doc_match_counts` says have matching pages. It
    must never consult the eval's expected-document list: that would
    measure an agent that already knew the answer.
    """
    represented = {m["path"] for m in matches}
    pending = [(p, n) for p, n in doc_match_counts.items() if p not in represented]
    pending.sort(key=lambda pair: (-pair[1], pair[0]))
    return [p for p, _n in pending[:limit]]


def build_payload(matches: list[dict[str, Any]], id_by_path: dict[str, str]) -> str:
    lines = []
    for i, m in enumerate(matches, 1):
        doc = id_by_path.get(m["path"], Path(m["path"]).stem)
        excerpt = " ".join((m.get("excerpt") or "").split())
        lines.append(f"{i}. [{doc} page {m['page']}] {excerpt}")
    return "\n".join(lines) if lines else "(no results returned)"


READ_PAGES = 2
READ_CHARS = 6000


def pages_to_read(
    matches: list[dict[str, Any]], limit: int = READ_PAGES
) -> list[tuple[str, int]]:
    """The (document, page) pairs an agent would open, best-ranked first.

    Uses only the response's own ranking, never `expect_docs` -- selecting
    by the answer key would measure an agent that already knew it.
    """
    seen: list[tuple[str, int]] = []
    for m in matches:
        key = (m["path"], m["page"])
        if key not in seen:
            seen.append(key)
        if len(seen) == limit:
            break
    return seen


def build_read_payload(
    search_payload: str, matches: list[dict[str, Any]], mode: str
) -> str:
    """Corpus search excerpts plus the full text of the pages opened.

    The corpus analogue of the single-document eval's decomposed arm: the
    flow this server documents is search to locate, then pdf_read_pages to
    answer. Grading the search payload alone measures excerpt quality.
    """
    from pdf_mcp.server import pdf_read_pages

    parts = [search_payload, ""]
    for path, page in pages_to_read(matches):
        try:
            result = pdf_read_pages(path, str(page))
        except Exception as exc:  # a read failure is data, not a crash
            parts.append(f"(could not read {Path(path).stem} page {page}: {exc})")
            continue
        for got in result.get("pages") or []:
            text = " ".join((got.get("text") or "").split())[:READ_CHARS]
            parts.append(
                f"--- FULL TEXT OF {Path(path).stem} PAGE {got.get('page')} ---"
            )
            parts.append(text)
            tables = got.get("tables") or []
            if tables:
                parts.append(f"--- TABLES ON PAGE {got.get('page')} ---")
                parts.append(json.dumps(tables, default=str)[:READ_CHARS])
    return "\n".join(parts)


def extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {"answerable": "error", "wrong_attribution": False, "reason": "no JSON"}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {
            "answerable": "error",
            "wrong_attribution": False,
            "reason": "bad JSON",
        }


def ballots_decided(states: list[str], wrongs: list[bool], votes: int) -> bool:
    """True when the remaining ballots cannot change either verdict.

    Both dimensions must be settled: `answerable` takes the plurality, and
    `wrong_attribution` needs a strict majority of all `votes`. A leader is
    unassailable once it beats the runner-up by more than the number of
    ballots still unspent -- so two agreeing ballots settle a majority of
    three, and the third call is pure waste.
    """
    remaining = votes - len(states)
    tally = sorted(Counter(states).values(), reverse=True)
    best = tally[0] if tally else 0
    runner_up = tally[1] if len(tally) > 1 else 0
    # With no ballots left the verdict stands however the counts fell --
    # including a three-way tie, which the plurality resolves by tie-break.
    answerable_settled = remaining <= 0 or best > runner_up + remaining

    needed = votes // 2 + 1
    trues = sum(1 for w in wrongs if w)
    wrong_settled = trues >= needed or trues + remaining < needed
    return answerable_settled and wrong_settled


def judge_majority(
    question: dict[str, Any], payload: str, model: str, votes: int = 3
) -> dict[str, Any]:
    """Majority-of-N verdict, mirroring eval_coherence.py.

    A single `claude -p` vote is demonstrably unstable here: on the first
    run, two questions whose two payloads were byte-identical (no follow-up
    calls were made) still received different verdicts. Across the 204
    recorded ballot triples, 16% of questions split, and a lone ballot
    disagrees with the majority 5.7% of the time -- noise of the same order
    as the mode differences being measured. So every verdict is the majority
    of `votes` independent calls.

    Ballots are spent one at a time and stop early once `ballots_decided`
    says the rest cannot matter. This is lossless by construction, not an
    approximation: replayed over those same triples it costs 2.11 calls per
    question instead of 3 and changes no verdict.
    """
    ballots: list[dict[str, Any]] = []
    states: list[str] = []
    wrongs: list[bool] = []
    for _ in range(votes):
        ballot = judge_one(question, payload, model)
        ballots.append(ballot)
        states.append(ballot.get("answerable"))
        wrongs.append(bool(ballot.get("wrong_attribution")))
        if ballots_decided(states, wrongs, votes):
            break

    # Deterministic tie-break. `max(set(states), ...)` iterated a set of
    # strings, whose order is hash-randomised per process, so a three-way
    # split (2 of the 204 recorded triples) could grade differently on a
    # rerun of identical ballots. Ties now fall to the same state every time.
    winner = min(sorted(set(states)), key=lambda s: -states.count(s))
    wrong = sum(wrongs) > votes // 2
    chosen = next(b for b in ballots if b.get("answerable") == winner)
    return {
        **chosen,
        "answerable": winner,
        "wrong_attribution": wrong,
        "votes": states,
        "ballots_spent": len(states),
        "unanimous": len(set(states)) == 1,
    }


def _cache_key(prompt: str, model: str) -> str:
    """Identity of a judge call: the exact prompt, on the exact model.

    Hashing the whole prompt means a cached ballot is reused only for
    byte-identical input -- change the rubric, the payload, or the
    question and the key changes, so a stale ballot can never be served.
    """
    return hashlib.sha256(f"{model}\0{prompt.strip()}".encode()).hexdigest()


def _load_ballot_cache() -> dict[str, list[dict[str, Any]]]:
    cache: dict[str, list[dict[str, Any]]] = {}
    if not BALLOT_CACHE.exists():
        return cache
    for line in BALLOT_CACHE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        cache.setdefault(row["key"], []).append(row["ballot"])
    return cache


_CACHE_LOCK = threading.Lock()
_BALLOT_CACHE: dict[str, list[dict[str, Any]]] | None = None


def _take_cached_ballot(key: str) -> dict[str, Any] | None:
    """Consume one stored ballot for this prompt, if any remain.

    Ballots are i.i.d. samples, so replaying a previously paid-for one is
    statistically identical to drawing a fresh one -- it just costs
    nothing. Each is consumed once so a majority of 3 never becomes the
    same ballot counted three times.
    """
    global _BALLOT_CACHE
    with _CACHE_LOCK:
        if _BALLOT_CACHE is None:
            _BALLOT_CACHE = _load_ballot_cache()
        pending = _BALLOT_CACHE.get(key)
        return pending.pop(0) if pending else None


def _record_ballot(key: str, ballot: dict[str, Any]) -> None:
    """Append immediately, so an interrupted run keeps everything it paid
    for. A previous run was killed 26 questions in and threw away 53
    completed calls because results were written only at the end."""
    with _CACHE_LOCK:
        with BALLOT_CACHE.open("a") as fh:
            fh.write(json.dumps({"key": key, "ballot": ballot}) + "\n")


def judge_one(question: dict[str, Any], payload: str, model: str) -> dict[str, Any]:
    facts = "\n".join(f"- {f}" for f in question["reference_facts"])
    confusable = question.get("confusable_with", "")
    prompt = (
        f"{RUBRIC}\n\n"
        f"QUESTION: {question['question']}\n\n"
        f"REFERENCE FACTS (ground truth):\n{facts}\n\n"
        f"COMMONLY CONFUSED WITH: {confusable}\n\n"
        f"PAYLOAD THE CALLER RECEIVED:\n{payload}\n"
    )
    key = _cache_key(prompt, model)
    cached = _take_cached_ballot(key)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                model,
                "--disallowedTools",
                DENIED_TOOLS,
                *JUDGE_CONTEXT_FLAGS,
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=JUDGE_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        return {"answerable": "error", "wrong_attribution": False, "reason": str(exc)}
    except subprocess.TimeoutExpired:
        return {"answerable": "error", "wrong_attribution": False, "reason": "timeout"}
    if result.returncode != 0 or not result.stdout.strip():
        return {
            "answerable": "error",
            "wrong_attribution": False,
            "reason": f"exit {result.returncode}",
        }
    ballot = extract_json(result.stdout)
    # Record only real verdicts. Caching an error would replay a transient
    # failure forever; those must be retried on the next run.
    if ballot.get("answerable") in ("full", "partial", "no"):
        _record_ballot(key, ballot)
    return ballot


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--mode", default="auto", choices=["auto", "keyword", "semantic"])
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="run retrieval and objective metrics only; no billed judge calls",
    )
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--excerpt-style",
        default="paragraph",
        choices=["paragraph", "snippet"],
        help=(
            "excerpt style passed to pdf_corpus_search. 'snippet' is"
            " byte-for-byte the pre-branch default, so judging the search"
            " arm under it measures the tool as it was before this branch."
        ),
    )
    ap.add_argument(
        "--arms",
        default="search,followup",
        help=(
            "comma-separated arms to grade. 'search' = the corpus_search"
            " payload alone; 'followup' = plus scoped re-searches of"
            " documents that matched but won no slot; 'read' = plus the full"
            " text of the top pages, the flow this server documents. Each"
            " arm costs a full judged pass."
        ),
    )
    args = ap.parse_args(argv)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        if a not in ("search", "followup", "read"):
            print(f"ERROR: unknown arm {a!r}")
            return 2

    if not args.dry_run and shutil.which("claude") is None:
        print("ERROR: the 'claude' CLI is not on PATH — the judge cannot run.")
        return 2

    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search, pdf_corpus_warm

    manifest = json.loads((DATA / "manifest.json").read_text())
    questions = json.loads((DATA / "answerability_questions.json").read_text())
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    paths = [p for p in id_by_path if Path(p).exists()]
    if not paths:
        print("ERROR: no corpus docs available; run scripts/fetch_financial_corpus.py")
        return 2

    cache_dir = REPO / "benchmark_data" / ".answerability_cache"
    server_module.cache = PDFCache(cache_dir=cache_dir, ttl_hours=24 * 30)
    warm = pdf_corpus_warm(paths, budget_seconds=600, embeddings=True)
    while warm.get("unprocessed"):
        warm = pdf_corpus_warm(paths, budget_seconds=600, embeddings=True)
    print(f"corpus warm ({len(paths)} docs)\n")

    from pdf_mcp.server import pdf_search

    rows: list[dict[str, Any]] = []
    for q in questions["questions"]:
        res = pdf_corpus_search(
            paths,
            q["question"],
            mode=args.mode,
            top_k=TOP_K,
            excerpt_style=args.excerpt_style,
        )
        matches = res.get("matches", [])
        counts = res.get("doc_match_counts", {})

        # The flow an agent should follow: a question spanning several
        # documents cannot be served by one ranked list, so follow up on
        # documents that matched but won no slot. Selection uses only the
        # response, never expect_docs.
        followups = followup_docs(matches, counts, FOLLOWUP_LIMIT)
        followup_matches: list[dict[str, Any]] = []
        for path in followups:
            sub = pdf_search(path, q["question"], mode=args.mode, max_results=3)
            for m in sub.get("matches", []):
                followup_matches.append({**m, "path": path})

        # Discoverability is objective: of the documents a complete answer
        # needs, how many did the FIRST response either return or name in
        # doc_match_counts? This is what the caller could have known.
        visible = {id_by_path.get(m["path"], "") for m in matches}
        visible |= {id_by_path.get(p, "") for p in counts}
        expect_ids = q["expect_docs"]
        discoverable = sum(1 for d in expect_ids if d in visible) / len(expect_ids)

        got_docs = [id_by_path.get(m["path"], "") for m in matches]
        expect = q["expect_docs"]
        present = [d for d in expect if d in got_docs]
        counts = {d: got_docs.count(d) for d in expect}
        balance = (
            min(counts.values()) / max(1, len(matches)) if len(expect) > 1 else 1.0
        )
        rows.append(
            {
                "id": q["id"],
                "scope": q["scope"],
                "type": q["type"],
                "question": q["question"],
                "doc_coverage": len(present) / len(expect),
                "expect_docs": expect,
                "doc_counts": counts,
                "balance": round(balance, 3),
                "n_matches": len(matches),
                "discoverable": round(discoverable, 3),
                "followup_docs": [id_by_path.get(p, p) for p in followups],
                "payload": build_payload(matches, id_by_path),
                "payload_decomposed": build_payload(
                    matches + followup_matches, id_by_path
                ),
                "payload_read": build_read_payload(
                    build_payload(matches, id_by_path), matches, args.mode
                ),
            }
        )

    KEY = {
        "search": ("payload", "verdict"),
        "followup": ("payload_decomposed", "verdict_decomposed"),
        "read": ("payload_read", "verdict_read"),
    }
    if not args.dry_run:
        for arm in arms:
            payload_key, verdict_key = KEY[arm]
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                got = list(
                    pool.map(
                        lambda pair: judge_majority(
                            pair[0], pair[1][payload_key], args.model
                        ),
                        zip(questions["questions"], rows),
                    )
                )
            for row, verdict in zip(rows, got):
                row[verdict_key] = verdict
            full = sum(1 for v in got if v.get("answerable") == "full")
            print(f"arm {arm:9s}: answerable in full {full}/{len(rows)}", flush=True)

    def tally(key: str) -> dict[str, int]:
        return {
            state: sum(1 for r in rows if r.get(key, {}).get("answerable") == state)
            for state in ("full", "partial", "no")
        } | {"wrong": sum(1 for r in rows if r.get(key, {}).get("wrong_attribution"))}

    n = len(rows)
    single = tally("verdict")
    decomposed = tally("verdict_decomposed")
    disc = sum(r["discoverable"] for r in rows) / n
    full = sum(1 for r in rows if r.get("verdict", {}).get("answerable") == "full")
    partial = sum(
        1 for r in rows if r.get("verdict", {}).get("answerable") == "partial"
    )
    none_ = sum(1 for r in rows if r.get("verdict", {}).get("answerable") == "no")
    wrong = sum(1 for r in rows if r.get("verdict", {}).get("wrong_attribution"))
    cov = sum(r["doc_coverage"] for r in rows) / n

    print(f"{'id':28s} {'disc':>5s}  {'1-call':10s} {'decomposed':10s} followups")
    for r in rows:
        v = r.get("verdict", {})
        vd = r.get("verdict_decomposed", {})
        print(
            f"{r['id']:28s} {r['discoverable']:5.2f}"
            f"  {str(v.get('answerable', '-')):10s}"
            f" {str(vd.get('answerable', '-')):10s}"
            f" {','.join(r['followup_docs']) or '-'}"
        )
    print()
    unan = sum(1 for r in rows if r.get("verdict", {}).get("unanimous"))
    print(f"questions                     : {n}")
    if not args.dry_run:
        print(
            f"unanimous judge verdicts      : {unan}/{n}"
            "   (low agreement => treat deltas cautiously)"
        )
    print(f"mean doc coverage (1 call)    : {cov:.2f}")
    print(
        f"mean DISCOVERABILITY          : {disc:.2f}"
        "   <- of the docs a full answer needs, the share the first"
    )
    print(
        "                                        response returned OR named"
        " in doc_match_counts"
    )
    if not args.dry_run:
        print()
        print(f"{'':22s} {'1 call':>8s} {'decomposed':>12s}")
        for label, key in (
            ("answerable in full", "full"),
            ("partial", "partial"),
            ("not answerable", "no"),
            ("WRONG ATTRIBUTION", "wrong"),
        ):
            print(
                f"{label:22s} {single[key]:>3d}/{n:<4d} {decomposed[key]:>7d}/{n:<4d}"
            )

    out = {
        "mode": args.mode,
        "excerpt_style": args.excerpt_style,
        "model": args.model,
        "judge_context_flags": JUDGE_CONTEXT_FLAGS,
        "top_k": TOP_K,
        "corpus_docs": len(paths),
        "summary": {
            "questions": n,
            "mean_doc_coverage": round(cov, 4),
            "mean_discoverability": round(disc, 4),
            "single_call": single,
            "decomposed": decomposed,
            "full": full,
            "partial": partial,
            "no": none_,
            "wrong_attribution": wrong,
        },
        "per_question": rows,
    }
    suffix = "" if args.excerpt_style == "paragraph" else f"_{args.excerpt_style}"
    (DATA / f"answerability_results{suffix}.json").write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n"
    )
    print(f"\nwrote {DATA / ('answerability_results' + suffix + '.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
