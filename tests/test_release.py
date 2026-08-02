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


def test_ghcr_manifest_url_is_the_anonymous_registry_endpoint():
    """Deliberately NOT `gh api /users/.../packages/container/...`: that
    endpoint needs the read:packages scope, which the maintainer's gh
    token does not carry, so it answers 403 rather than a visibility and
    would warn on every release regardless of the true state."""
    assert (
        release.ghcr_manifest_url()
        == "https://ghcr.io/v2/jztan/pdf-mcp/manifests/latest"
    )


def test_ghcr_manifest_url_handles_an_explicit_tag():
    assert release.ghcr_manifest_url(tag="2.0.0").endswith("/manifests/2.0.0")
