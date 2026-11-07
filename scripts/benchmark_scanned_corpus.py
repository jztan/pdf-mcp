#!/usr/bin/env python
"""
scripts/benchmark_scanned_corpus.py

How much of a corpus can an agent still find once the PDFs are scans?

`pdf_corpus_warm` reads the text layer only, so a folder of scans warms to
zero characters and corpus search is silently empty (issue #103). This
harness measures that gap and, once OCR on warm exists, how much of it OCR
closes. Free and deterministic: no judge calls.

  build   render every filing in the 10-K set to an image-only PDF
          (grayscale JPEG pages, no text layer) under
          benchmark_data/.scanned_pdfs/<set>/, with a manifest pinning the
          sha256 of each source and each scan.
  score   warm a corpus (born-digital originals, or the scans) in its own
          cache, loop until warm_complete, run every single-doc question of
          the financial set through pdf_corpus_search, and score it with
          diagnose_excerpt_fidelity's own rules (same gold pages, same
          buckets), so the born-digital arm is directly comparable.

Gold pages always come from the born-digital text: a scan keeps the
original pagination, so the same page numbers are gold in both arms.

Run:
  uv run python scripts/benchmark_scanned_corpus.py build
  uv run python scripts/benchmark_scanned_corpus.py score --arm born
  uv run python scripts/benchmark_scanned_corpus.py score --arm scanned
  # once #103 lands, the OCR arm is the scanned arm with warm arguments:
  uv run python scripts/benchmark_scanned_corpus.py score --arm scanned \
      --warm-args '{"ocr": "auto"}' --label ocr-auto

Exit codes: 0 ran, 2 setup error (missing PDF, digest mismatch, mixed cache,
corpus never warmed to completion).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time

from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

DATA = REPO / "benchmark_data" / "financial_reports"
SCAN_ROOT = REPO / "benchmark_data" / ".scanned_pdfs"
CACHE_ROOT = REPO / "benchmark_data" / ".scanned_corpus_cache"
SET_NAME = "financial"
DPI = 200
JPEG_QUALITY = 75
TOP_K = 10
MAX_PAGES = 400
WARM_BUDGET = 300
MAX_WARM_CALLS = 2000
OCR_POLL_SECONDS = 5


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def source_docs() -> list[dict[str, Any]]:
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    return manifest["docs"]


def rasterize(src: Path, dst: Path, dpi: int, quality: int) -> int:
    """Write an image-only copy of `src`: one grayscale JPEG per page, page
    size kept in points, no text objects. Returns the page count."""
    import pypdfium2 as pdfium

    pdf_in = pdfium.PdfDocument(str(src))
    pdf_out = pdfium.PdfDocument.new()
    try:
        for i in range(len(pdf_in)):
            page = pdf_in[i]
            w, h = page.get_size()
            pil = page.render(scale=dpi / 72, grayscale=True).to_pil()
            page.close()
            buf = io.BytesIO()
            pil.convert("L").save(buf, format="JPEG", quality=quality)
            buf.seek(0)

            out_page = pdf_out.new_page(w, h)
            img = pdfium.PdfImage.new(pdf_out)
            img.load_jpeg(buf, pages=[out_page], inline=False, autoclose=True)
            img.set_matrix(pdfium.PdfMatrix(w, 0, 0, h, 0, 0))
            out_page.insert_obj(img)
            out_page.gen_content()
            out_page.close()
        n = len(pdf_in)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".pdf.tmp")
        pdf_out.save(str(tmp))
        tmp.replace(dst)
        return n
    finally:
        pdf_out.close()
        pdf_in.close()


def cmd_build(args: argparse.Namespace) -> int:
    out_dir = SCAN_ROOT / SET_NAME
    man_path = out_dir / "manifest.json"
    old = {}
    if man_path.exists():
        old = {d["id"]: d for d in json.loads(man_path.read_text())["docs"]}
    docs = []
    for d in source_docs():
        src = REPO / d["path"]
        if not src.exists():
            print(f"ERROR: missing source {src}")
            return 2
        src_sha = sha256(src)
        dst = out_dir / f"{d['id']}.pdf"
        prev = old.get(d["id"])
        if (
            prev
            and dst.exists()
            and prev["source_sha256"] == src_sha
            and prev["dpi"] == args.dpi
            and prev["jpeg_quality"] == args.quality
            and prev["sha256"] == sha256(dst)
        ):
            docs.append(prev)
            print(f"  {d['id']:14s} up to date")
            continue
        t0 = time.monotonic()
        n = rasterize(src, dst, args.dpi, args.quality)
        docs.append(
            {
                "id": d["id"],
                "path": str(dst.relative_to(REPO)),
                "source_path": d["path"],
                "source_sha256": src_sha,
                "sha256": sha256(dst),
                "pages": n,
                "dpi": args.dpi,
                "jpeg_quality": args.quality,
                "bytes": dst.stat().st_size,
            }
        )
        print(
            f"  {d['id']:14s} {n:4d} pages  {dst.stat().st_size / 1e6:6.1f} MB"
            f"  {time.monotonic() - t0:5.1f}s"
        )
        man_path.parent.mkdir(parents=True, exist_ok=True)
        man_path.write_text(json.dumps({"docs": docs}, indent=2) + "\n")
    man_path.write_text(
        json.dumps(
            {
                "description": (
                    "Image-only copies of the 10-K set: each page rendered"
                    f" grayscale at {args.dpi} DPI, JPEG q{args.quality}, no"
                    " text layer. Built by scripts/benchmark_scanned_corpus.py."
                ),
                "docs": docs,
            },
            indent=2,
        )
        + "\n"
    )
    total = sum(d["pages"] for d in docs)
    size = sum(d["bytes"] for d in docs) / 1e6
    print(f"\n{len(docs)} scans, {total} pages, {size:.0f} MB -> {man_path}")
    return 0


def verified_scans() -> tuple[dict[str, str] | None, str | None]:
    man_path = SCAN_ROOT / SET_NAME / "manifest.json"
    if not man_path.exists():
        return None, f"no scan manifest at {man_path}; run `build` first"
    src_sha = {d["id"]: sha256(REPO / d["path"]) for d in source_docs()}
    out = {}
    for d in json.loads(man_path.read_text())["docs"]:
        p = REPO / d["path"]
        if not p.exists():
            return None, f"missing scan {p}"
        if d["source_sha256"] != src_sha.get(d["id"]):
            return None, f"{d['id']}: source changed since the scan was built"
        if sha256(p) != d["sha256"]:
            return None, f"{d['id']}: scan digest mismatch"
        out[d["id"]] = str(p)
    return out, None


def warm_until_complete(
    paths: list[str], embeddings: bool, extra: dict[str, Any]
) -> dict[str, Any]:
    from pdf_mcp.server import pdf_corpus_warm

    t0 = time.monotonic()
    calls = 0
    last: dict[str, Any] = {}
    while calls < MAX_WARM_CALLS:
        last = pdf_corpus_warm(
            paths, budget_seconds=WARM_BUDGET, embeddings=embeddings, **extra
        )
        calls += 1
        if "error" in last:
            raise SystemExit(f"warm error: {last['error']}")
        ocr = last.get("ocr") or {}
        if calls == 1 or ocr.get("status") != "running" or calls % 12 == 0:
            print(
                f"    warm call {calls}: warm_complete={last.get('warm_complete')}"
                f" unwarmed={last.get('unwarmed')} ocr={ocr.get('status')}"
                f" pending={ocr.get('pages_pending')} done={ocr.get('pages_done')}"
                f" no_text={ocr.get('pages_no_text')}"
                f" t={time.monotonic() - t0:.0f}s",
                flush=True,
            )
        if last.get("warm_complete"):
            break
        if ocr.get("status") in ("running", "setting_up"):
            # OCR runs between calls (issue #103): give it time, as an
            # agent looping on warm_complete would.
            time.sleep(OCR_POLL_SECONDS)
    return {
        "calls": calls,
        "seconds": round(time.monotonic() - t0, 1),
        "warm_complete": bool(last.get("warm_complete")),
        "ocr": last.get("ocr"),
        "last_response_keys": sorted(last),
    }


def cmd_score(args: argparse.Namespace) -> int:
    os.environ.setdefault("PDF_MCP_CACHE_TTL", "8760")
    from bench_env import environment, mixed_cache_gate
    from diagnose_excerpt_fidelity import classify, load_dataset, locate

    from pdf_mcp import _core
    from pdf_mcp.cache import PDFCache

    born = {d["id"]: str(REPO / d["path"]) for d in source_docs()}
    for p in born.values():
        if not Path(p).exists():
            print(f"ERROR: missing source {p}")
            return 2
    if args.arm == "scanned":
        searched, err = verified_scans()
        if err or searched is None:
            print(f"ERROR: {err}")
            return 2
    else:
        searched = born
    label = args.label or args.arm
    warm_extra = json.loads(args.warm_args) if args.warm_args else {}

    # Gold pages come from the ORIGINAL text, in a cache of its own, so the
    # scanned arm is graded on the same pages as the born-digital one.
    gold_cache = PDFCache(cache_dir=CACHE_ROOT / "born", ttl_hours=8760)
    arm_dir = CACHE_ROOT / ("born" if label == "born" else label)
    if args.fresh and label != "born" and arm_dir.exists():
        import shutil

        shutil.rmtree(arm_dir)

    print("warming born-digital originals (gold pages)")
    _core.cache = gold_cache
    # ocr="off": gold pages come from the text layer only, so OCR on the
    # MSFT cover pages (issue #103) cannot move them between runs.
    if label == "born" and warm_extra:
        print("ERROR: --warm-args needs a --label of its own (born is gold)")
        return 2
    born_warm = warm_until_complete(
        list(born.values()), args.embeddings, {"ocr": "off"}
    )

    if label == "born":
        cache, arm_warm = gold_cache, born_warm
    else:
        cache = PDFCache(cache_dir=arm_dir, ttl_hours=8760)
        _core.cache = cache
        print(f"warming arm {label!r} {warm_extra or ''}")
        arm_warm = warm_until_complete(
            list(searched.values()), args.embeddings, warm_extra
        )
    if not arm_warm["warm_complete"]:
        print("ERROR: corpus never reached warm_complete")
        return 2

    paths = list(searched.values())
    comp, err = mixed_cache_gate(cache.db_path, paths, False)
    if err:
        print(f"ERROR: {err}")
        return 2

    chars = 0
    text_pages = 0
    for p in paths:
        for t in cache.get_pages_text(p, list(range(MAX_PAGES))).values():
            chars += len(t.strip())
            text_pages += bool(t.strip())

    from pdf_mcp.server import pdf_corpus_search

    questions = load_dataset(DATA)
    rows = []
    t0 = time.monotonic()
    for q in questions:
        raw = gold_cache.get_pages_text(born[q.doc], list(range(MAX_PAGES)))
        gold = locate(q.fact, {n + 1: t for n, t in raw.items()})
        info = pdf_corpus_search(paths, q.question, mode=args.mode, top_k=TOP_K)
        if "error" in info:
            print(f"ERROR: {q.id}: {info['error']}")
            return 2
        want = searched[q.doc]
        matches = [m for m in info.get("matches", []) if m.get("path") == want]
        routed: list[str] = []
        for m in info.get("matches", []):
            if m.get("path") not in routed:
                routed.append(m.get("path"))
        hit = [m["page"] for m in matches if m["page"] in gold]
        if not gold:
            bucket, carries = "unlocatable", False
        elif not hit:
            bucket = "PAGE MISS" if matches else "DOC MISS"
            carries = False
        else:
            ex = [m.get("excerpt") or "" for m in matches if m["page"] == hit[0]]
            _, carries = classify(q.fact, ex, q.span)
            bucket = "ok" if carries else "EXCERPT MISS"
        rows.append(
            {
                "id": q.id,
                "type": q.type,
                "doc": q.doc,
                "gold_pages": gold,
                "gold_rank": next(
                    (
                        i + 1
                        for i, m in enumerate(info.get("matches", []))
                        if m.get("path") == want and m["page"] in gold
                    ),
                    None,
                ),
                "doc_rank": routed.index(want) + 1 if want in routed else None,
                "n_matches": len(info.get("matches", [])),
                "bucket": bucket,
                "carries_figure": carries,
            }
        )
    secs = time.monotonic() - t0

    n = len(rows)
    counts = {
        b: sum(r["bucket"] == b for r in rows)
        for b in ("ok", "EXCERPT MISS", "PAGE MISS", "DOC MISS", "unlocatable")
    }
    locatable = n - counts["unlocatable"]
    found = counts["ok"] + counts["EXCERPT MISS"]
    doc3 = sum(1 for r in rows if r["doc_rank"] and r["doc_rank"] <= 3)
    empty = sum(1 for r in rows if r["n_matches"] == 0)
    summary = {
        "questions": n,
        "locatable": locatable,
        "page_recall": round(found / locatable, 3) if locatable else None,
        "answer_in_excerpt": round(counts["ok"] / locatable, 3) if locatable else None,
        "doc_hit_at_3": round(doc3 / n, 3),
        "empty_responses": empty,
        "buckets": counts,
        "text_pages": text_pages,
        "text_chars": chars,
        "seconds_per_query": round(secs / n, 3),
    }
    print(f"\narm {label}: {n} questions over {len(paths)} docs, mode={args.mode}")
    for k, v in summary.items():
        print(f"  {k:18s} {v}")

    out = DATA / f"scanned_corpus_{label}_{args.mode}.json"
    out.write_text(
        json.dumps(
            {
                "arm": args.arm,
                "label": label,
                "mode": args.mode,
                "top_k": TOP_K,
                "embeddings": args.embeddings,
                "warm_args": warm_extra,
                "warm": arm_warm,
                "cache": comp,
                "environment": environment(),
                "summary": summary,
                "rows": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="render the image-only copies")
    b.add_argument("--dpi", type=int, default=DPI)
    b.add_argument("--quality", type=int, default=JPEG_QUALITY)
    s = sub.add_parser("score", help="warm one arm and score corpus search")
    s.add_argument("--arm", choices=["born", "scanned"], required=True)
    s.add_argument("--mode", default="auto", choices=["auto", "keyword", "semantic"])
    s.add_argument("--label", help="results/cache name (default: the arm)")
    s.add_argument("--warm-args", help="JSON of extra pdf_corpus_warm arguments")
    s.add_argument(
        "--no-embeddings",
        dest="embeddings",
        action="store_false",
        help="warm text only (auto mode then searches keyword-first)",
    )
    s.add_argument(
        "--fresh", action="store_true", help="drop this arm's cache before warming"
    )
    args = ap.parse_args(argv)
    return cmd_build(args) if args.cmd == "build" else cmd_score(args)


if __name__ == "__main__":
    raise SystemExit(main())
