#!/usr/bin/env python
"""Parallel-OCR benchmark + accuracy check on the UNLV/ISRI corpus.

Why this is a separate script from ``benchmark_parallel_pages.py``:
``benchmark_parallel_pages.py`` deliberately keeps ``pdf_mcp.server`` out of its
top-level imports so its isolation micro-benchmarks measure *cheap* worker
spawns. This script does the opposite on purpose -- it imports ``pdf_mcp.server``
at module top so that, under the ``spawn`` start method, each pooled worker
re-imports the server module (FastMCP + ``import fastmcp``, ~0.5 s) exactly like
the **real STDIO deployment** does. The numbers here therefore reflect what a
user actually experiences, not a library-only best case.

The UNLV/ISRI collection is the canonical Tesseract benchmark: 300 dpi bitonal
scans with ground-truth text, split by document class so we can show how OCR
parallel speedup scales with per-page density (``bus`` letters -> ``mag`` /
``news``). Images download on demand into a gitignored cache (like the arXiv
corpus used by the reading-order benchmark); nothing large is committed.

Usage::

    python scripts/benchmark_ocr_corpus.py                 # bus,mag,news x 1,8
    python scripts/benchmark_ocr_corpus.py --classes bus   # one class
    python scripts/benchmark_ocr_corpus.py --pages 16 --workers 1,4,8

Requires Tesseract on PATH (the OCR engine) and network access on first run.
"""

import argparse
import glob
import os
import re
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections import Counter
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Set the cache dir BEFORE importing pdf_mcp.server so the benchmark never
# touches the developer's real SQLite cache.
os.environ.setdefault("PDF_MCP_CACHE_DIR", tempfile.mkdtemp(prefix="ocr_corpus_cache_"))

from pdf_mcp.extractor import check_tesseract_available  # noqa: E402
from pdf_mcp.server import cache, pdf_read_pages  # noqa: E402

ISRI_CACHE = Path(__file__).parent.parent / "benchmark_data" / ".isri_cache"

# Canonical UNLV/ISRI "3B" sets (Fourth Annual Test of OCR Accuracy), hosted by
# the tesseract-ocr/test project. Each archive is one document class.
ISRI_URLS = {
    "bus": "https://sourceforge.net/projects/isri-ocr-evaluation-tools-alt/"
    "files/bus.3B.tar.gz/download",
    "mag": "https://sourceforge.net/projects/isri-ocr-evaluation-tools-alt/"
    "files/mag.3B.tar.gz/download",
    "news": "https://sourceforge.net/projects/isri-ocr-evaluation-tools-alt/"
    "files/news.3B.tar.gz/download",
    "doe": "https://sourceforge.net/projects/isri-ocr-evaluation-tools-alt/"
    "files/doe3.3B.tar.gz/download",
}
ISRI_LABEL = {
    "bus": "business letters (sparse)",
    "mag": "magazines (dense)",
    "news": "newspapers",
    "doe": "Dept. of Energy docs",
}


def fetch_isri(cls: str) -> Path | None:
    """Download + extract one ISRI class into the gitignored cache.

    Returns the extracted directory, or None if the download failed (e.g. no
    network) so the caller can skip the class with a clear message.
    """
    dest = ISRI_CACHE / cls
    if any(dest.glob("**/*.tif")):
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    archive = ISRI_CACHE / f"{cls}.3B.tar.gz"
    try:
        if not archive.exists():
            req = urllib.request.Request(
                ISRI_URLS[cls],
                headers={"User-Agent": "Mozilla/5.0 (pdf-mcp ocr benchmark)"},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                archive.write_bytes(r.read())
        with tarfile.open(archive) as tar:
            tar.extractall(dest)  # noqa: S202 - trusted ISRI archive
        return dest
    except Exception as exc:  # noqa: BLE001
        print(f"  {cls}: download/extract failed ({exc})")
        return None


def sample_pairs(class_dir: Path, n: int) -> list[tuple[str, str]]:
    """Return up to n (tif, ground_truth_txt) path pairs, deterministically."""
    pairs = []
    for tif in sorted(glob.glob(f"{class_dir}/**/*.tif", recursive=True)):
        gt = tif[:-4] + ".txt"
        if os.path.exists(gt):
            pairs.append((tif, gt))
        if len(pairs) >= n:
            break
    return pairs


def build_pdf(tifs: list[str], out_path: str) -> None:
    """Wrap each G4-TIFF scan into a one-image PDF page (mirrors a scanned PDF)."""
    out = pymupdf.open()
    for t in tifs:
        img = pymupdf.open(t)
        out.insert_pdf(pymupdf.open("pdf", img.convert_to_pdf()))
        img.close()
    out.save(out_path)
    out.close()


def ocr_pages(path: str, n_pages: int, workers: int) -> tuple[float, list[str]]:
    """Cache-cold pdf_read_pages(ocr=True); returns (wall_seconds, per-page text)."""
    os.environ["PDF_MCP_MAX_WORKERS"] = str(workers)
    cache.clear_all()
    pages = ",".join(str(i + 1) for i in range(n_pages))
    start = time.perf_counter()
    result = pdf_read_pages(path, pages, ocr=True)
    elapsed = time.perf_counter() - start
    return elapsed, [p["text"] for p in result["pages"]]


def word_recall(ocr_text: str, gt_path: str) -> float:
    """Order-insensitive OCR-quality proxy: fraction of ground-truth words
    (as a multiset) recovered by OCR. Robust to multi-column reading-order
    differences, unlike a character-sequence ratio -- magazines/newspapers
    have multi-column ground truth, so a sequence ratio understates quality.
    """

    def toks(s: str) -> "Counter[str]":
        return Counter(re.findall(r"[a-z0-9]+", s.lower()))

    gt = toks(Path(gt_path).read_text(errors="ignore"))
    if not gt:
        return 0.0
    hit = sum((toks(ocr_text) & gt).values())
    return hit / sum(gt.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classes", default="bus,mag,news", help="ISRI classes")
    parser.add_argument("--pages", type=int, default=8, help="pages per class")
    parser.add_argument("--workers", default="1,8", help="worker counts to time")
    args = parser.parse_args()

    classes = [c for c in args.classes.split(",") if c.strip()]
    workers = [int(w) for w in args.workers.split(",") if w.strip()]

    try:
        check_tesseract_available()
    except RuntimeError as exc:
        print(f"Tesseract unavailable: {exc}")
        sys.exit(1)

    import multiprocessing

    print(
        f"# Parallel OCR on UNLV/ISRI  (CPUs={os.cpu_count()}, "
        f"start={multiprocessing.get_start_method()}, "
        f"regime=server-loaded/real-deployment)\n"
    )
    header = f"{'class':6s} {'pages':>5s} {'seq/pg':>7s}"
    for w in workers:
        if w != 1:
            header += f" {('x' + str(w)):>8s}"
    header += f" {'par==seq':>9s} {'wordrec':>8s}"
    print(header)

    with tempfile.TemporaryDirectory() as tmp:
        for cls in classes:
            cdir = fetch_isri(cls)
            if cdir is None:
                continue
            pairs = sample_pairs(cdir, args.pages)
            if not pairs:
                print(f"{cls}: no (tif, gt) pairs found")
                continue
            tifs = [p[0] for p in pairs]
            gts = [p[1] for p in pairs]
            pdf_path = str(Path(tmp) / f"{cls}.pdf")
            build_pdf(tifs, pdf_path)
            npg = len(tifs)

            seq_time, seq_text = ocr_pages(pdf_path, npg, 1)
            row = f"{cls:6s} {npg:5d} {seq_time / npg:7.2f}"
            identical = True
            for w in workers:
                if w == 1:
                    continue
                par_time, par_text = ocr_pages(pdf_path, npg, w)
                identical = identical and (par_text == seq_text)
                row += f" {seq_time / par_time:7.2f}x"
            recalls = [word_recall(seq_text[i], gts[i]) for i in range(npg)]
            avg_recall = sum(recalls) / len(recalls)
            row += f" {('yes' if identical else 'NO'):>9s} {avg_recall:7.0%}"
            print(f"{row}   ({ISRI_LABEL.get(cls, cls)})")


if __name__ == "__main__":
    main()
