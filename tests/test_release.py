# tests/test_release.py
"""Tests for scripts/release.py pre-flight behavior."""

import inspect
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

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


def test_ghcr_visibility_check_bounds_the_curl_call():
    """curl must carry --max-time so a stalled request cannot hang forever.

    curl defaults to a 300s connect timeout and no transfer timeout, and
    run_command sets no subprocess timeout, so a half-open connection would
    block indefinitely. The post-release call site runs after every mutating
    step, so a hang there strands a release that has already fully
    succeeded: the completion summary never prints and the stale-notes
    cleanup never runs.
    """
    source = inspect.getsource(release.check_ghcr_public)
    # Comments are stripped: the comment explaining the flag also contains
    # the string, so an unfiltered match would stay green after the flag
    # itself was deleted.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--max-time" in code, (
        "check_ghcr_public must pass curl --max-time; without it a "
        "half-open connection hangs the post-release check forever"
    )


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


def test_version_bump_keeps_roadmap_in_sync(tmp_path):
    """The bump must update ROADMAP alongside pyproject.

    test_docs_consistency asserts the two agree. If the release bumped only
    pyproject, the release branch would fail CI on `release/*` and again on
    the tag workflow that gates the PyPI publish.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ROADMAP.md").write_text(
        "# Roadmap\n\n- **Current version:** v1.0.0 (released 2020-01-01)\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "1.0.0"\n', encoding="utf-8"
    )

    release.update_roadmap_version(tmp_path, "1.1.0", dry_run=False)
    release.update_pyproject_toml(tmp_path, "1.1.0", dry_run=False)

    roadmap = (tmp_path / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    assert "v1.1.0" in roadmap
    assert "v1.0.0" not in roadmap
    assert '"1.1.0"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")


def test_version_bump_stages_the_roadmap():
    """ROADMAP must be in the staged file list, or the edit above never
    reaches the release commit."""
    source = inspect.getsource(release.commit_version_bump)
    assert '"docs/ROADMAP.md"' in source


def test_roadmap_bump_fails_loudly_when_the_status_line_moves(tmp_path):
    """If someone reformats the Project Status line, the bump must raise
    rather than silently no-op. A no-op would surface much later, as a red
    release CI run mid-release."""
    import pytest

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ROADMAP.md").write_text(
        "# Roadmap\n\nCurrent: 1.0.0\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="Current version"):
        release.update_roadmap_version(tmp_path, "9.9.9", dry_run=False)


CHANGELOG_WITH_CREDITS = """# Changelog

## [Unreleased]
### Contributors
- @newest — reported a bug ([#99](https://example.com/99))

## [1.1.0] - 2020-02-01
### Fixed
- Something. Thanks @not-a-credit for the idea.

### Contributors
- @second — sent a PR ([#2](https://example.com/2))
- @first — diagnosed and fixed the failure ([#1](https://example.com/1))

## [1.0.0] - 2020-01-01
### Contributors
- @first — reported it first ([#0](https://example.com/0))
"""

README_WITH_MARKERS = """# Project

## Contributors

Thanks:

<!-- contributors:start -->
[@stale](https://github.com/stale)
<!-- contributors:end -->

## License
"""


def _write_contributor_fixtures(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG_WITH_CREDITS, encoding="utf-8")
    (tmp_path / "README.md").write_text(README_WITH_MARKERS, encoding="utf-8")


def test_readme_contributors_lists_every_changelog_credit(tmp_path):
    """Every handle credited in a CHANGELOG `### Contributors` block belongs in
    the README list, whatever form the contribution took (issue report, test,
    PR). Hand-maintaining it drifts: redmine-mcp-server's equivalent list is
    missing three merged-PR authors."""
    _write_contributor_fixtures(tmp_path)

    release.update_readme_contributors(tmp_path, dry_run=False)

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "[@first](https://github.com/first)" in readme
    assert "[@second](https://github.com/second)" in readme
    assert "[@newest](https://github.com/newest)" in readme
    # A handle mentioned in prose is not a credit.
    assert "not-a-credit" not in readme
    # The stale hand-written entry is replaced, and the rest of the file stands.
    assert "@stale" not in readme
    assert "## License" in readme


def test_readme_contributors_are_oldest_first_and_deduped(tmp_path):
    """@first is credited in two releases: listed once, in the position of the
    earliest credit, so appending a new contributor never reshuffles the line."""
    _write_contributor_fixtures(tmp_path)

    release.update_readme_contributors(tmp_path, dry_run=False)

    line = release.render_contributors_line(
        release.collect_changelog_contributors(CHANGELOG_WITH_CREDITS)
    )
    assert line in (tmp_path / "README.md").read_text(encoding="utf-8")
    assert release.collect_changelog_contributors(CHANGELOG_WITH_CREDITS) == [
        "first",
        "second",
        "newest",
    ]


def test_readme_contributors_dry_run_writes_nothing(tmp_path):
    _write_contributor_fixtures(tmp_path)

    release.update_readme_contributors(tmp_path, dry_run=True)

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == README_WITH_MARKERS


def test_readme_contributors_fails_loudly_when_markers_go_missing(tmp_path):
    """If someone reformats the section away, the release must raise rather
    than silently stop crediting people."""
    import pytest

    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG_WITH_CREDITS, encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# Project\n\n## Contributors\n\nThanks:\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="contributors:start"):
        release.update_readme_contributors(tmp_path, dry_run=False)


def test_version_bump_stages_the_readme():
    """README must be in the staged file list, or the regenerated line never
    reaches the release commit."""
    source = inspect.getsource(release.commit_version_bump)
    assert '"README.md"' in source


def test_committed_readme_contributors_match_the_changelog():
    """Guards the checked-in files: a credit added to the CHANGELOG without a
    release cut would otherwise sit unlisted until the next bump."""
    expected = release.render_contributors_line(
        release.collect_changelog_contributors(
            (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        )
    )
    assert expected in (REPO_ROOT / "README.md").read_text(encoding="utf-8")


# --- release-notes headline -------------------------------------------------
#
# The v3.1.0 title ("Honest search over scanned PDFs, smarter excerpts")
# paraphrased the first changelog bullet because the prompt gave the
# headline no brief and one draft offered one title. These pin the fix.


def test_parse_titles_reads_three_candidates_and_strips_them_from_the_body():
    output = (
        "TITLE 1: Sharper corpus search\n"
        "TITLE 2: Honest results on scanned PDFs\n"
        "TITLE 3: Smarter excerpts\n"
        "## Highlights\n\nbody\n"
    )
    titles, body = release._parse_titles(output, "v3.1.0")
    assert titles == [
        "v3.1.0: Sharper corpus search",
        "v3.1.0: Honest results on scanned PDFs",
        "v3.1.0: Smarter excerpts",
    ]
    assert body == "## Highlights\n\nbody"


def test_parse_titles_accepts_the_old_single_title_line():
    titles, body = release._parse_titles("TITLE: One headline\nbody", "v1.0.0")
    assert titles == ["v1.0.0: One headline"]
    assert body == "body"


def test_parse_titles_falls_back_to_the_bare_tag():
    titles, body = release._parse_titles("## Highlights\nbody", "v1.0.0")
    assert titles == ["v1.0.0"]
    assert body == "## Highlights\nbody"


def test_titles_never_carry_an_em_dash():
    # The gh publish hook rejects em dashes; the old contract hardcoded one.
    assert "—" not in release.RELEASE_NOTES_PROMPT
    titles, _ = release._parse_titles("TITLE 1: v9 — dashed\nbody", "v9.0.0")
    assert "—" not in titles[0]


def test_prompt_briefs_the_headline_and_asks_for_three():
    prompt = release.RELEASE_NOTES_PROMPT
    assert "TITLE 1:" in prompt and "TITLE 3:" in prompt
    # The brief: weight by size of change, name the surface, plain words.
    assert "largest" in prompt
    assert "corpus" in prompt or "surface" in prompt


def test_split_headline_hint_reads_and_strips_the_comment():
    section = "<!-- headline: corpus search vs Bedrock -->\n" "### Added\n- **thing**\n"
    hint, rest = release.split_headline_hint(section)
    assert hint == "corpus search vs Bedrock"
    assert rest == "### Added\n- **thing**"
    assert release.split_headline_hint(rest) == (None, rest)


def test_update_changelog_strips_the_headline_hint(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n"
        "<!-- headline: sharper corpus search -->\n"
        "### Added\n- **thing**\n\n## [1.0.0] - 2026-01-01\n- old\n",
        encoding="utf-8",
    )
    release.update_changelog(tmp_path, "1.1.0", dry_run=False)
    text = changelog.read_text(encoding="utf-8")
    assert "headline:" not in text
    assert "## [1.1.0] - " in text
    assert "- **thing**" in text


def test_generate_release_notes_passes_context_flags_and_hint():
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs["input"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout="TITLE 1: a\nTITLE 2: b\nTITLE 3: c\nbody", stderr=""
        )

    with patch.object(release.subprocess, "run", fake_run):
        titles, body = release.generate_release_notes(
            "1.2.0", "### Added\n- x", headline_hint="lead with corpus"
        )
    assert titles == ["v1.2.0: a", "v1.2.0: b", "v1.2.0: c"]
    assert body == "body"
    for flag in release.NOTES_CONTEXT_FLAGS:
        assert flag in seen["cmd"]
    assert "lead with corpus" in seen["input"]


def test_notes_context_flags_match_the_judge_flags():
    # Same lesson, same flags: every `claude -p` here reloads both CLAUDE.md
    # files and every MCP schema unless told not to.
    from scripts.eval_financial_answerability import JUDGE_CONTEXT_FLAGS

    assert release.NOTES_CONTEXT_FLAGS == JUDGE_CONTEXT_FLAGS


def test_recovery_instructions_say_rerun_only_helps_when_flaky(tmp_path, capsys):
    release.print_recovery_instructions("1.2.0", "release/v1.2.0", tmp_path)
    out = capsys.readouterr().out
    assert "—" not in out
    assert "flaky" in out
    assert "git push origin :refs/tags/v1.2.0" in out


def test_prompt_forbids_victory_claims_in_headlines():
    # The context flags drop CLAUDE.md, so the benchmark-wording rule has
    # to live in the prompt: a hinted draft once offered "ahead of Bedrock".
    prompt = release.RELEASE_NOTES_PROMPT
    assert "measured against" in prompt
    assert '"ahead of"' in prompt
