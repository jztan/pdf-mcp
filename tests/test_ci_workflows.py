"""ci.yml and docs.yml must partition pushes the same way.

ci.yml skips its seven-job matrix on pushes that touch only prose, and
docs.yml runs the doc-reading tests on exactly those pushes. The two
workflows carry the same path list, once as `paths-ignore` and once as
`paths`. If they drift, a path lands in neither workflow (a doc-reading
test silently stops running on it) or in both (the matrix runs for
nothing again). Nothing in GitHub checks this, so this test does.
"""

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
