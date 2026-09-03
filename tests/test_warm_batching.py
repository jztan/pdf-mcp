"""Spec validation items 1-2: resume-equivalence and kill-mid-batch.

The batched+resumed cache must equal a single-shot warm byte for byte,
and a SIGKILL between batch commits must lose at most the in-flight
batch.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time

from pdf_mcp import corpus
from pdf_mcp.cache import PDFCache


def _embed(texts):
    # Deterministic, text-dependent blobs so byte-comparison is meaningful.
    return [bytes([sum(t.encode()) % 251] * 4) for t in texts]


def _dump_rows(cache, path, pages, model):
    embs = cache.get_page_embeddings(path, list(range(pages)), model)
    return {pn: tuple(blobs) for pn, blobs in embs.items()}


class TestResumeEquivalence:
    def test_forced_partials_end_byte_identical(
        self, corpus_dir, cache, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(corpus, "WARM_EMBED_BATCH_PAGES", 1)
        path = str(corpus_dir / "bravo.pdf")  # 4 pages

        # Arm 1: single-shot warm into a fresh cache.
        one = PDFCache(cache_dir=tmp_path / "oneshot", ttl_hours=1)
        pc, complete, _ = corpus._warm_one_doc(path, one, True, "fake-model", _embed)
        assert complete is True

        # Arm 2: batched with a deadline that stops after every batch,
        # resumed until complete.
        pc2, complete2, _ = corpus._warm_one_doc(
            path,
            cache,
            True,
            "fake-model",
            _embed,
            deadline=-1.0,
            clock=lambda: 0.0,
        )
        assert complete2 is False
        for _ in range(pc + 2):
            if complete2:
                break
            texts = cache.get_pages_text(path, list(range(pc2)))
            complete2, _ = corpus._embed_doc_batched(
                path,
                texts,
                cache,
                "fake-model",
                _embed,
                deadline=-1.0,
                clock=lambda: 0.0,
            )
        assert complete2 is True
        assert _dump_rows(cache, path, pc, "fake-model") == _dump_rows(
            one, path, pc, "fake-model"
        )
        assert (
            one.get_doc_profiles([path], "fake-model").keys()
            == cache.get_doc_profiles([path], "fake-model").keys()
        )


class TestKillMidBatch:
    def test_sigkill_between_commits_resumes_clean(
        self, corpus_dir, temp_cache_dir, monkeypatch
    ):
        path = str(corpus_dir / "bravo.pdf")
        script = textwrap.dedent(f"""
            import time
            from pathlib import Path
            from pdf_mcp import corpus
            from pdf_mcp.cache import PDFCache
            corpus.WARM_EMBED_BATCH_PAGES = 1

            def slow_embed(texts):
                time.sleep(0.8)  # wide kill window per batch
                return [bytes([sum(t.encode()) % 251] * 4) for t in texts]

            cache = PDFCache(cache_dir=Path({str(temp_cache_dir)!r}),
                             ttl_hours=1)
            corpus._warm_one_doc({path!r}, cache, True, "fake-model",
                                 slow_embed)
            """)
        proc = subprocess.Popen([sys.executable, "-c", script])
        # Deterministic kill point: wait until at least one batch has
        # committed, then SIGKILL while batches remain in flight.
        pc = 4
        watcher = PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            committed = len(_dump_rows(watcher, path, pc, "fake-model"))
            if committed >= 1:
                break
            time.sleep(0.05)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()

        cache = PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)
        # Non-vacuous: the subprocess must have committed at least one
        # batch before dying, or this test proves nothing.
        before = _dump_rows(cache, path, pc, "fake-model")
        assert before, "subprocess died before any batch committed"
        assert len(before) < pc, "subprocess finished; kill came too late"
        # No torn state: every stored page has a full, valid row list.
        assert all(blobs for blobs in before.values())
        # Resume converges without re-extracting what survived.
        if corpus._cached_pages(path, cache, False, "fake-model") is None:
            _, complete, _ = corpus._warm_one_doc(
                path, cache, True, "fake-model", _embed
            )
        else:
            texts = cache.get_pages_text(path, list(range(pc)))
            complete, _ = corpus._embed_doc_batched(
                path,
                texts,
                cache,
                "fake-model",
                _embed,
                deadline=float("inf"),
            )
        assert complete is True
        assert corpus._cached_pages(path, cache, True, "fake-model") == pc


class TestOvershootBound:
    def test_sequential_overshoot_is_at_most_one_batch(
        self, corpus_dir, cache, monkeypatch
    ):
        monkeypatch.setattr(corpus, "WARM_EMBED_BATCH_PAGES", 1)
        path = str(corpus_dir / "bravo.pdf")  # 4 pages
        embed_calls = []

        def counting_embed(texts):
            embed_calls.append(len(texts))
            return [b"\x00\x00\x80?" for _ in texts]

        # Deadline in the past: exactly one batch (the floor) may run.
        _, complete, embedded = corpus._warm_one_doc(
            path,
            cache,
            True,
            "fake-model",
            counting_embed,
            deadline=-1.0,
            clock=lambda: 0.0,
        )
        assert complete is False
        assert embedded == 1
        assert len(embed_calls) == 1  # one batch, no profile encode
