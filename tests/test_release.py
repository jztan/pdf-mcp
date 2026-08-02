# tests/test_release.py
"""Tests for scripts/release.py pre-flight behavior."""

import inspect
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import release  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


def test_preflight_pytest_cmd_deselects_slow_tests():
    """The release gate must not run billed/slow tests (e.g. the coherence
    eval that shells out to the real `claude` CLI over the corpus)."""
    cmd = release.preflight_pytest_cmd()
    assert "tests/" in cmd
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "not slow"


def test_slow_marker_deselects_coherence_eval():
    """`-m "not slow"` must actually exclude the billed coherence guard.

    Real behavior check: collect test_eval_coherence.py with the gate's marker
    filter and confirm the billed node is not collected."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_eval_coherence.py",
            "-m",
            "not slow",
            "--collect-only",
            "-q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert "test_coherence_no_regression_vs_baseline" not in result.stdout


def test_publish_workflow_wait_covers_the_image_builds():
    """The release run now carries two native image builds, an assemble,
    two smoke jobs, and a promote on top of the test matrix. The old
    15-minute budget would time out on a cold cache and print recovery
    instructions for a perfectly healthy release."""
    default = (
        inspect.signature(release.wait_for_publish_workflow)
        .parameters["max_wait"]
        .default
    )
    assert default >= 1800


def test_ghcr_pull_token_url_is_the_discriminating_endpoint():
    """A plain manifest GET answers 401 for public and private packages
    alike, so it cannot tell them apart. The anonymous pull-token endpoint
    answers 200 for a public package and 403 for a private or absent one,
    which is the question preflight actually needs answered."""
    assert release.ghcr_pull_token_url() == (
        "https://ghcr.io/token" "?scope=repository:jztan/pdf-mcp:pull&service=ghcr.io"
    )


def test_ghcr_visibility_check_distinguishes_preflight_from_post_release():
    """A 403 means two different things depending on when it is seen.

    In pre-flight the image does not exist yet, so 403 is expected. After
    the release it means the just-published package is private and every
    documented `docker pull` fails, which has to be shouted about."""
    param = inspect.signature(release.check_ghcr_public).parameters["post_release"]
    assert param.default is False


def test_ghcr_pull_token_url_derives_the_repository_from_the_image():
    url = release.ghcr_pull_token_url("ghcr.io/someone/other-image")
    assert "repository:someone/other-image:pull" in url
    assert url.startswith("https://ghcr.io/token?")
