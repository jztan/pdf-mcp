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
