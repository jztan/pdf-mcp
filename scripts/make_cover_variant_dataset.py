"""Build a cover-variant copy of a corpus_search dataset.

Picks N docs that have graded queries and prepends two pages to each: a
blank page and an image-only page (a solid raster, no text layer). Every
gold page of those docs in queries.json shifts by +2; the other docs keep
their original paths. Used to measure document-profile head selection on
docs whose page 1 carries no text.

    uv run python scripts/make_cover_variant_dataset.py \\
        --data-dir benchmark_data/corpus_search \\
        --out benchmark_data/corpus_covers --docs 10 --seed 1

Writes OUT/manifest.json, OUT/queries.json (and fidelity_questions.json
when the source has one) and OUT/pdfs/<id>.pdf. Deterministic per seed.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHIFT = 2
PAGE_SIZE = (612.0, 792.0)


def _image_only_pdf() -> bytes:
    """One page holding a solid grey raster and no text layer."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (612, 792), (200, 200, 200)).save(
        buf, format="PDF", resolution=72.0
    )
    return buf.getvalue()


def prepend_cover_pages(src: Path, dst: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(*PAGE_SIZE)
    writer.append(PdfReader(io.BytesIO(_image_only_pdf())))
    writer.append(PdfReader(str(src)))
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as fh:
        writer.write(fh)


def graded_doc_ids(queries: dict) -> list[str]:
    seen: set[str] = set()
    for q in queries["queries"]:
        for label in q["labels"]:
            seen.add(label["doc"])
    return sorted(seen)


def build_variant(data_dir: Path, out: Path, n_docs: int, seed: int) -> list[str]:
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    queries = json.loads((data_dir / "queries.json").read_text(encoding="utf-8"))
    by_id = {d["id"]: d for d in manifest["docs"]}
    candidates = [i for i in graded_doc_ids(queries) if i in by_id]
    if n_docs > len(candidates):
        raise SystemExit(f"only {len(candidates)} docs have graded queries")
    chosen = sorted(random.Random(seed).sample(candidates, n_docs))

    new_manifest = copy.deepcopy(manifest)
    new_queries = copy.deepcopy(queries)
    for doc in new_manifest["docs"]:
        if doc["id"] not in chosen:
            continue
        src = REPO / doc["path"]  # an absolute path wins over REPO
        dst = (out / "pdfs" / f"{doc['id']}.pdf").resolve()
        prepend_cover_pages(src, dst)
        try:
            doc["path"] = str(dst.relative_to(REPO))
        except ValueError:
            doc["path"] = str(dst)
    for q in new_queries["queries"]:
        for label in q["labels"]:
            if label["doc"] in chosen and "page" in label:
                label["page"] += SHIFT
    new_manifest["description"] = (
        f"{new_manifest.get('description', '')} Cover variant: {n_docs} docs"
        f" ({', '.join(chosen)}) have a blank page and an image-only page"
        f" prepended; their gold pages shift by +{SHIFT}."
    ).strip()
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(new_manifest, indent=2) + "\n", encoding="utf-8"
    )
    (out / "queries.json").write_text(
        json.dumps(new_queries, indent=2) + "\n", encoding="utf-8"
    )
    fidelity = data_dir / "fidelity_questions.json"
    if fidelity.exists():
        (out / "fidelity_questions.json").write_text(
            fidelity.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return chosen


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--docs", type=int, required=True)
    ap.add_argument("--seed", type=int, default=1)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    chosen = build_variant(REPO / args.data_dir, REPO / args.out, args.docs, args.seed)
    print(json.dumps({"chosen": chosen, "shift": SHIFT}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
