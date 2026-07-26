#!/usr/bin/env python
"""Fetch the financial-report benchmark corpus.

PDFs are not committed. This script downloads every doc listed in
benchmark_data/financial_reports/manifest.json, verifies it is a real PDF,
and checks its SHA256 against the manifest.

sec.gov requires a declaring User-Agent and rate-limits hard by IP: a burst
earns a temporary block that 403s every sec.gov request. Downloads are
therefore serialized with a delay, and 403/429 responses back off and retry.
If sec.gov stays blocked, wait ~15 minutes and rerun -- do not swap the URLs.

Run:  uv run python scripts/fetch_financial_corpus.py
      uv run python scripts/fetch_financial_corpus.py --update-checksums
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time

from pathlib import Path
from urllib.parse import urlparse

import httpx

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "benchmark_data" / "financial_reports" / "manifest.json"

SEC_USER_AGENT = "pdf-mcp-benchmark (https://github.com/jztan/pdf-mcp)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SEC_HOSTS = {"sec.gov", "www.sec.gov", "data.sec.gov"}

DELAY_SECONDS = 1.0
RETRY_DELAYS = (15.0, 60.0, 180.0)


def ua_for_url(url: str) -> str:
    """sec.gov demands a declaring UA; everything else wants a browser UA."""
    return SEC_USER_AGENT if urlparse(url).hostname in SEC_HOSTS else BROWSER_USER_AGENT


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path) -> None:
    """Download url to dest, retrying on 403/429 with backoff. Raises on failure."""
    headers = {"User-Agent": ua_for_url(url), "Accept": "application/pdf,*/*"}
    last: Exception | None = None
    for attempt, delay in enumerate((0.0,) + RETRY_DELAYS):
        if delay:
            print(f"    blocked; backing off {delay:.0f}s (attempt {attempt + 1})")
            time.sleep(delay)
        try:
            with httpx.stream(
                "GET", url, headers=headers, follow_redirects=True, timeout=120.0
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
        except httpx.HTTPError as exc:  # network/timeout/status errors
            last = exc
    raise RuntimeError(f"giving up on {url}: {last}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--update-checksums",
        action="store_true",
        help="record the sha256 of each freshly downloaded file into the manifest",
    )
    ap.add_argument(
        "--force", action="store_true", help="re-download even if the file exists"
    )
    args = ap.parse_args(argv)

    manifest = json.loads(MANIFEST.read_text())
    failures: list[str] = []
    changed = False

    for doc in manifest["docs"]:
        dest = REPO / doc["path"]
        if dest.exists() and not args.force:
            print(f"[skip] {doc['id']} (already present)")
        else:
            print(f"[get ] {doc['id']} <- {doc['url']}")
            try:
                download(doc["url"], dest)
            except RuntimeError as exc:
                print(f"    FAIL {exc}")
                failures.append(doc["id"])
                continue
            time.sleep(DELAY_SECONDS)

        with dest.open("rb") as fh:
            head = fh.read(5)
        if head != b"%PDF-":
            print(f"    FAIL {doc['id']} is not a PDF (starts {head!r})")
            failures.append(doc["id"])
            continue

        digest = sha256_file(dest)
        if doc.get("sha256") is None:
            if args.update_checksums:
                doc["sha256"] = digest
                changed = True
                print(f"    recorded sha256 {digest[:16]}...")
            else:
                print(f"    WARNING no sha256 in manifest ({digest[:16]}...)")
        elif doc["sha256"] != digest:
            print(f"    FAIL sha256 mismatch: expected {doc['sha256']}, got {digest}")
            failures.append(doc["id"])

    if changed:
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"\nupdated {MANIFEST}")

    total = len(manifest["docs"])
    print(f"\n{total - len(failures)}/{total} docs OK")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
