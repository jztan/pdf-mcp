"""scripts/bench_env.py: the environment a benchmark ran in.

On 2026-09-12 identical code measured ~37% slower on corpus semantic
search from a worktree whose venv had resolved Python 3.12 / SQLite 3.50
instead of the main checkout's 3.13 / 3.51, and read as a regression
until the interpreters were swapped. Timings carry their environment so
that confound is visible in the output, and comparisons across differing
environments are refused rather than reported.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bench_env  # noqa: E402


def test_environment_names_what_moves_timings():
    env = bench_env.environment()
    assert env["python"] == ".".join(map(str, sys.version_info[:3]))
    assert env["sqlite"] == sqlite3.sqlite_version
    for key in ("implementation", "machine", "platform", "executable", "packages"):
        assert env[key], key
    assert set(env["packages"]) >= {"pdf-mcp", "fastembed", "onnxruntime", "numpy"}


def test_missing_package_is_reported_not_raised(monkeypatch):
    from importlib import metadata

    real = metadata.version

    def fake(name):
        if name == "onnxruntime":
            raise metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(bench_env.metadata, "version", fake)
    assert bench_env.environment()["packages"]["onnxruntime"] == "absent"


def _env(**over):
    env = {
        "python": "3.13.1",
        "implementation": "CPython",
        "sqlite": "3.51.0",
        "machine": "arm64",
        "platform": "macOS-26.6-arm64",
        "executable": "/a/.venv/bin/python",
        "packages": {"onnxruntime": "1.24.4", "numpy": "2.4.4"},
    }
    env.update(over)
    return env


def test_identical_environments_are_comparable():
    assert bench_env.timing_mismatch(_env(), _env()) == []


def test_the_2026_09_12_confound_is_caught():
    diffs = bench_env.timing_mismatch(_env(), _env(python="3.12.12", sqlite="3.50.4"))
    assert "python 3.13.1 != 3.12.12" in diffs
    assert "sqlite 3.51.0 != 3.50.4" in diffs


def test_package_version_drift_is_caught():
    other = _env(packages={"onnxruntime": "1.23.0", "numpy": "2.4.4"})
    assert bench_env.timing_mismatch(_env(), other) == ["onnxruntime 1.24.4 != 1.23.0"]


def test_a_different_venv_path_alone_is_comparable():
    """Two worktrees on the same interpreter and libraries time alike."""
    assert bench_env.timing_mismatch(_env(), _env(executable="/b/python")) == []


def test_an_unrecorded_environment_is_not_comparable():
    assert bench_env.timing_mismatch(None, _env()) == [
        "baseline recorded no environment"
    ]
    assert bench_env.timing_mismatch(_env(), None) == [
        "current run recorded no environment"
    ]


def test_markdown_line_names_interpreter_and_sqlite():
    line = bench_env.markdown_line(_env())
    assert "Python 3.13.1" in line and "SQLite 3.51.0" in line
    assert "arm64" in line


# Corpus keyword and hybrid search take BM25 word rarity from every
# document in the cache's shared FTS table, not only the searched ones, so a
# corpus benchmark scored on a cache that also holds unrelated PDFs is not
# comparable with a corpus-only run (84 unrelated filings moved the Bedrock
# anchor's described class from 0.325 to 0.289 on identical code). The
# harnesses that score corpus search on the active cache check the index
# first and refuse a mixed cache unless told otherwise.


def _fts_db(tmp_path, paths):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE VIRTUAL TABLE pdf_search_fts USING fts5("
        "file_path UNINDEXED, page_num UNINDEXED, text)"
    )
    conn.executemany(
        "INSERT INTO pdf_search_fts VALUES (?, ?, ?)",
        [(p, n, "some text") for p in paths for n in (0, 1)],
    )
    conn.commit()
    conn.close()
    return db


def test_keyword_index_docs_lists_distinct_paths(tmp_path):
    db = _fts_db(tmp_path, ["/c/a.pdf", "/c/b.pdf"])
    assert bench_env.keyword_index_docs(db) == {"/c/a.pdf", "/c/b.pdf"}


def test_corpus_only_cache_passes(tmp_path):
    db = _fts_db(tmp_path, ["/c/a.pdf", "/c/b.pdf"])
    comp, err = bench_env.mixed_cache_gate(db, ["/c/a.pdf", "/c/b.pdf"], allow=False)
    assert err is None
    assert comp["indexed_docs"] == 2 and comp["extra_docs"] == 0
    assert comp["mixed_allowed"] is False and len(comp["digest"]) == 16


def test_searched_docs_not_yet_indexed_are_not_extra(tmp_path):
    db = _fts_db(tmp_path, ["/c/a.pdf"])
    comp, err = bench_env.mixed_cache_gate(db, ["/c/a.pdf", "/c/b.pdf"], allow=False)
    assert err is None and comp["extra_docs"] == 0


def test_mixed_cache_is_refused_and_names_examples(tmp_path):
    db = _fts_db(tmp_path, ["/c/a.pdf", "/x/f1.pdf", "/x/f2.pdf"])
    comp, err = bench_env.mixed_cache_gate(db, ["/c/a.pdf"], allow=False)
    assert comp["extra_docs"] == 2 and comp["extra_sample"] == ["f1.pdf", "f2.pdf"]
    assert err is not None
    assert "2 documents outside" in err and "--allow-mixed-cache" in err


def test_mixed_cache_allowed_is_recorded_not_refused(tmp_path):
    db = _fts_db(tmp_path, ["/c/a.pdf", "/x/f1.pdf"])
    comp, err = bench_env.mixed_cache_gate(db, ["/c/a.pdf"], allow=True)
    assert err is None and comp["mixed_allowed"] is True and comp["extra_docs"] == 1


def test_digest_changes_with_cache_contents(tmp_path):
    a = bench_env.mixed_cache_gate(
        _fts_db(tmp_path / "a", ["/c/a.pdf"]), ["/c/a.pdf"], allow=False
    )[0]["digest"]
    b = bench_env.mixed_cache_gate(
        _fts_db(tmp_path / "b", ["/c/b.pdf"]), ["/c/b.pdf"], allow=False
    )[0]["digest"]
    assert a != b
