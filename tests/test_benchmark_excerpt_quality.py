"""Tests for scripts/benchmark_excerpt_quality.py helpers."""

import pymupdf

from scripts.benchmark_excerpt_quality import bbox_contains_answer


def test_bbox_contains_answer_true_and_false():
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 200), "the answer phrase lives here in this block.", fontsize=11
    )
    blocks = [b for b in page.get_text("blocks", sort=True) if b[6] == 0]
    bbox = list(blocks[0][:4])
    assert bbox_contains_answer(page, bbox, "answer phrase") is True
    assert bbox_contains_answer(page, [0, 0, 5, 5], "answer phrase") is False
    doc.close()


def test_bbox_contains_answer_normalizes_hyphens_and_case():
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 200), "In-Context Learning is a prompting technique.", fontsize=11
    )
    blocks = [b for b in page.get_text("blocks", sort=True) if b[6] == 0]
    bbox = list(blocks[0][:4])
    assert bbox_contains_answer(page, bbox, "in context learning") is True
    doc.close()
