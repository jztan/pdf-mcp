"""Workflow invariants that GitHub itself never checks.

1. ci.yml and docs.yml must partition pushes the same way.

ci.yml skips its seven-job matrix on pushes that touch only prose, and
docs.yml runs the doc-reading tests on exactly those pushes. The two
workflows carry the same path list, once as `paths-ignore` and once as
`paths`. If they drift, a path lands in neither workflow (a doc-reading
test silently stops running on it) or in both (the matrix runs for
nothing again). Nothing in GitHub checks this, so this test does.

2. Every workflow installs from uv.lock and runs pytest under `uv run`, so
no job tests against dependency versions CI never saw (see the block at
the end of this file).
"""

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# The files ci.yml treats as prose. Kept explicit here rather than derived
# from one workflow so a drift in either file is caught.
DOC_PATHS = [
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "docs/**",
    "pages/**",
    "examples/README.md",
    "benchmark_data/**/*.md",
    ".github/ISSUE_TEMPLATE/**",
    ".github/PULL_REQUEST_TEMPLATE.md",
]


def _triggers(name: str) -> dict:
    with (WORKFLOWS / name).open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    # PyYAML parses the bare `on:` key as boolean True.
    return doc.get("on") or doc[True]


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_ci_ignores_exactly_the_doc_paths(event: str) -> None:
    assert _triggers("ci.yml")[event]["paths-ignore"] == DOC_PATHS


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_docs_runs_on_exactly_the_doc_paths(event: str) -> None:
    assert _triggers("docs.yml")[event]["paths"] == DOC_PATHS


def test_both_workflows_watch_the_same_branches() -> None:
    ci, docs = _triggers("ci.yml"), _triggers("docs.yml")
    for event in ("push", "pull_request"):
        assert ci[event]["branches"] == docs[event]["branches"]


def test_workflow_files_are_never_treated_as_prose() -> None:
    """An edit to a workflow must run the matrix, or a broken ci.yml could
    merge unexercised."""
    assert not any(p.startswith(".github/workflows") for p in DOC_PATHS)
    assert ".github/**" not in DOC_PATHS


# --- Every workflow tests against the lockfile ---------------------------
#
# The v3.4.0 publish run failed at tag time: publish-pypi.yml's test job ran
# `pip install -e .[dev]`, which ignores uv.lock, so it installed fastembed
# 0.8.1 over the locked 0.8.0 and the RRF gate refused the version change,
# while ci.yml (uv sync --frozen) stayed green on the same commit. These
# checks fail the PR that introduces such a job instead of the release.

_PROJECT_PIP_INSTALL = re.compile(
    r"\bpip\s+install\b[^\n]*?(?:\s-e\s+|\s)\.(?:\[|\s|$)"
)


def _steps() -> list[tuple[str, str, list[dict]]]:
    """(workflow, job, steps) for every job in every workflow file."""
    out = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        with path.open(encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        for job_name, job in (doc.get("jobs") or {}).items():
            out.append((path.name, job_name, job.get("steps") or []))
    return out


def test_project_pip_install_pattern() -> None:
    for cmd in ("pip install -e .[dev]", "pip install .", "pip install -e ."):
        assert _PROJECT_PIP_INSTALL.search(cmd), cmd
    for cmd in ("pip install build twine", "pip install --upgrade pip"):
        assert not _PROJECT_PIP_INSTALL.search(cmd), cmd


def test_no_workflow_installs_the_project_with_pip() -> None:
    offenders = [
        f"{wf}:{job}"
        for wf, job, steps in _steps()
        for step in steps
        if _PROJECT_PIP_INSTALL.search(step.get("run", ""))
    ]
    assert not offenders, (
        f"{offenders} install the project with pip, which ignores uv.lock; "
        "use `uv sync --frozen` as ci.yml does"
    )


def test_pytest_runs_only_under_a_frozen_uv_sync() -> None:
    offenders = []
    for wf, job, steps in _steps():
        synced = False
        for step in steps:
            run = step.get("run", "")
            if "uv sync --frozen" in run:
                synced = True
            for line in run.splitlines():
                if re.search(r"\bpytest\b", line) and not (
                    synced and re.search(r"\buv run pytest\b", line)
                ):
                    offenders.append(f"{wf}:{job}: {line.strip()}")
    assert not offenders, offenders
