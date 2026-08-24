#!/usr/bin/env python
"""Fetch ~N unlabeled arXiv distractor PDFs for the corpus-scale benchmark.

Distractors carry no labels; they only add rank competition. IDs are listed
from the arXiv API by category, deduped against the 100 gold ids (by base id
and by title), downloaded to benchmark_data/.corpus_distractors/ (gitignored),
and recorded with sha256 + title in distractor_manifest.json so the run is
reproducible without redistributing PDFs.

arXiv asks for <=1 request / 3s; fetches are serialized and back off on
403/429. PDFs are not committed.

Run:  uv run python scripts/fetch_corpus_distractors.py --count 400
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
GOLD = REPO / "benchmark_data" / "corpus_search" / "manifest.json"
OUT_MANIFEST = REPO / "benchmark_data" / "corpus_search" / "distractor_manifest.json"
PDF_DIR = REPO / "benchmark_data" / ".corpus_distractors"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
API = "https://export.arxiv.org/api/query"
# Match the gold set's broad STEM mix without overlapping its exact ids.
CATEGORIES = ("cs.LG", "cs.CL", "cs.CV", "math.PR", "physics.data-an")
DELAY_SECONDS = 3.0
RETRY_DELAYS = (15.0, 60.0, 180.0)
_ATOM = "{http://www.w3.org/2005/Atom}"


def _base_id(arxiv_id: str) -> str:
    """Strip a trailing version (v1/v2/...) and any category prefix."""
    tail = arxiv_id.rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", tail)


def gold_base_ids(gold_manifest: dict) -> set[str]:
    return {_base_id(d["id"]) for d in gold_manifest["docs"]}


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().lower()


def dedup_candidates(
    candidates: list[dict], gold_ids: set[str], gold_titles: set[str]
) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for c in candidates:
        bid = _base_id(c["id"])
        if bid in gold_ids or bid in seen:
            continue
        if _norm_title(c["title"]) in gold_titles:
            continue
        seen.add(bid)
        out.append({"id": bid, "title": re.sub(r"\s+", " ", c["title"]).strip()})
    return out


def parse_entries(xml_text: str) -> list[dict]:
    """[{"id","title"}] from an arXiv Atom API response. id keeps its
    raw form (e.g. '2401.00001v1'); caller strips the version."""
    out = []
    for e in ET.fromstring(xml_text).findall(f"{_ATOM}entry"):
        raw = e.find(f"{_ATOM}id").text.rsplit("/abs/", 1)[-1]
        title = e.find(f"{_ATOM}title").text or ""
        out.append({"id": raw, "title": title})
    return out


def arxiv_pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_candidates(need: int) -> list[dict]:
    """Page the arXiv API across CATEGORIES until >= need candidates gathered."""
    out: list[dict] = []
    per_cat = max(need // len(CATEGORIES) + 20, 30)
    for cat in CATEGORIES:
        start = 0
        while start < per_cat:
            params = {
                "search_query": f"cat:{cat}",
                "start": str(start),
                "max_results": "30",
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            resp = httpx.get(
                API,
                params=params,
                headers={"User-Agent": BROWSER_UA},
                timeout=60.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
            entries = parse_entries(resp.text)
            if not entries:
                break
            out.extend(entries)
            start += 30
            time.sleep(DELAY_SECONDS)
    return out


def fetch_gold_titles(gold_ids: set[str]) -> set[str]:
    """Normalized titles of the gold docs, via the arXiv API id_list
    endpoint. On any HTTP/parse error, returns an empty set (base-id
    dedup still protects) after printing a warning."""
    titles: set[str] = set()
    ids = sorted(gold_ids)
    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        try:
            resp = httpx.get(
                API,
                params={"id_list": ",".join(batch), "max_results": "50"},
                headers={"User-Agent": BROWSER_UA},
                timeout=60.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
            for e in parse_entries(resp.text):
                titles.add(_norm_title(e["title"]))
        except (httpx.HTTPError, ET.ParseError) as exc:
            print(
                f"    WARNING gold-title fetch failed ({exc}); "
                "title dedup disabled, base-id dedup still active"
            )
            return set()
        time.sleep(DELAY_SECONDS)
    return titles


def download(url: str, dest: Path) -> None:
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"}
    last: Exception | None = None
    for attempt, delay in enumerate((0.0,) + RETRY_DELAYS):
        if delay:
            print(f"    blocked; backing off {delay:.0f}s (attempt {attempt + 1})")
            time.sleep(delay)
        try:
            with httpx.stream(
                "GET",
                url,
                headers=headers,
                follow_redirects=True,
                timeout=180.0,
            ) as resp:
                if resp.status_code in (403, 429):
                    last = RuntimeError(f"HTTP {resp.status_code}")
                    continue
                resp.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".part")
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
                tmp.replace(dest)
                return
        except httpx.HTTPError as exc:
            last = exc
    raise RuntimeError(f"giving up on {url}: {last}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=400, help="distractor PDFs to fetch")
    args = ap.parse_args(argv)

    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    gold_ids = gold_base_ids(gold)
    gold_titles = fetch_gold_titles(gold_ids)

    existing = (
        json.loads(OUT_MANIFEST.read_text(encoding="utf-8"))
        if OUT_MANIFEST.exists()
        else {
            "description": "arXiv distractor corpus for the scale-1k benchmark "
            "(unlabeled; ids deduped against gold). PDFs not committed.",
            "docs": [],
        }
    )
    have = {d["id"] for d in existing["docs"]}

    candidates = dedup_candidates(list_candidates(args.count), gold_ids, gold_titles)
    candidates = [c for c in candidates if c["id"] not in have][: args.count]
    print(f"{len(candidates)} new candidates after dedup")

    failures: list[str] = []
    for c in candidates:
        dest = PDF_DIR / f"{c['id']}.pdf"
        if not dest.exists():
            print(f"[get ] {c['id']}")
            try:
                download(arxiv_pdf_url(c["id"]), dest)
            except RuntimeError as exc:
                print(f"    FAIL {exc}")
                failures.append(c["id"])
                continue
            time.sleep(DELAY_SECONDS)
        with dest.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                print(f"    FAIL {c['id']} not a PDF")
                dest.unlink(missing_ok=True)
                failures.append(c["id"])
                continue
        existing["docs"].append(
            {
                "id": c["id"],
                "path": f"benchmark_data/.corpus_distractors/{c['id']}.pdf",
                "title": c["title"],
                "sha256": sha256_file(dest),
            }
        )

    OUT_MANIFEST.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    print(
        f"\nmanifest now holds {len(existing['docs'])} distractors "
        f"({len(failures)} failed this run)"
    )
    return 1 if failures and not existing["docs"] else 0


if __name__ == "__main__":
    sys.exit(main())
