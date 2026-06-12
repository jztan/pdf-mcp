#!/usr/bin/env python3
"""
Release automation script for pdf-mcp using gitflow.

Usage:
    python scripts/release.py [patch|minor|major] [--dry-run]

Examples:
    python scripts/release.py patch           # 1.1.1 -> 1.1.2
    python scripts/release.py minor           # 1.1.1 -> 1.2.0
    python scripts/release.py major           # 1.1.1 -> 2.0.0
    python scripts/release.py patch --dry-run # Preview changes

Gitflow:
    1. Start from develop branch
    2. Pre-flight: clean tree, tests pass, dependency audit clean
    3. Create release/vX.Y.Z branch and bump versions
    4. Draft + approve release notes via claude -p (persisted for recovery)
    5. Merge to master, push tag (triggers publish-pypi.yml)
    6. Wait for publish-pypi workflow to finish (poll gh run status)
    7. Wait for the version to appear on PyPI
    8. Create GitHub release (only after `pip install` will actually work)
    9. Publish to MCP Registry
    10. Merge back to develop, delete release branch, remove notes drafts
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class ReleaseConfig:
    """Configuration for release automation."""

    bump_type: str
    dry_run: bool
    project_root: Path


def run_command(
    cmd: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    dry_run: bool = False,
    dry_run_msg: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command with optional dry-run support."""
    if dry_run and dry_run_msg:
        print(f"  [DRY-RUN] Would run: {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = subprocess.run(cmd, capture_output=capture_output, text=True, check=False)
    if check and result.returncode != 0:
        print(f"Error running command: {' '.join(cmd)}")
        print(f"  stdout: {result.stdout}")
        print(f"  stderr: {result.stderr}")
        sys.exit(1)
    return result


def get_current_version(project_root: Path) -> str:
    """Read current version from pyproject.toml."""
    pyproject = project_root / "pyproject.toml"
    content = pyproject.read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if not match:
        print("Error: Could not find version in pyproject.toml")
        sys.exit(1)
    return match.group(1)


def calculate_new_version(current: str, bump_type: str) -> str:
    """Calculate new version based on bump type."""
    parts = current.split(".")
    if len(parts) != 3:
        print(f"Error: Invalid version format: {current}")
        sys.exit(1)

    major, minor, patch = map(int, parts)

    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    # patch
    return f"{major}.{minor}.{patch + 1}"


def preflight_checks() -> None:
    """Verify prerequisites for release."""
    print("\n=== Pre-flight Checks ===\n")

    # Check git status is clean
    print("Checking git status...")
    result = run_command(["git", "status", "--porcelain"])
    if result.stdout.strip():
        print("Error: Working directory is not clean. Please commit or stash changes.")
        print(result.stdout)
        sys.exit(1)
    print("  ✓ Working directory is clean")

    # Check we're on develop branch
    print("Checking current branch...")
    result = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    branch = result.stdout.strip()
    if branch != "develop":
        print(
            "Error: Must be on 'develop' branch to start release, "
            f"currently on '{branch}'"
        )
        sys.exit(1)
    print("  ✓ On develop branch")

    # Pull latest changes
    print("Pulling latest changes...")
    run_command(["git", "pull", "origin", "develop"])
    print("  ✓ Up to date with origin/develop")

    # Check tests pass
    print("Running tests...")
    result = run_command(
        ["pytest", "tests/", "-v", "--tb=short"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        print("Error: Tests failed. Please fix before releasing.")
        print(result.stdout)
        print(result.stderr)
        sys.exit(1)
    print("  ✓ All tests pass")

    # Dependency audit — same script CI runs, so a green preflight means a
    # green publish-pypi job. Catches the failure mode where pip-audit blocks
    # the release after the tag has already been pushed.
    print("Auditing dependencies...")
    project_root = Path(__file__).parent.parent.resolve()
    audit_script = project_root / "scripts" / "audit.sh"
    result = run_command(
        ["bash", str(audit_script)],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            "Error: Dependency audit failed. "
            "Fix or update the ignore list before releasing."
        )
        print(result.stdout)
        print(result.stderr)
        sys.exit(1)
    print("  ✓ No vulnerabilities (or all are in the ignore list)")

    # Check gh CLI is available and authenticated
    print("Checking gh CLI...")
    result = run_command(["which", "gh"], check=False)
    if result.returncode != 0:
        print("Error: 'gh' CLI not found. Install with: brew install gh")
        sys.exit(1)
    # Check gh is authenticated, prompt login if not
    result = run_command(["gh", "auth", "status"], check=False, capture_output=True)
    if result.returncode != 0:
        print("  ⚠ gh CLI not authenticated. Starting login...")
        login_result = subprocess.run(["gh", "auth", "login"], check=False)
        if login_result.returncode != 0:
            print("Error: gh authentication failed")
            sys.exit(1)
    print("  ✓ gh CLI available and authenticated")

    # Check mcp-publisher is available and authenticated
    print("Checking mcp-publisher...")
    result = run_command(["which", "mcp-publisher"], check=False)
    if result.returncode != 0:
        print(
            "Warning: 'mcp-publisher' not found. MCP Registry publish will be skipped."
        )
        print("         Install with: brew install mcp-publisher")
    else:
        # Check if mcp-publisher is authenticated, prompt login if not
        result = run_command(
            ["mcp-publisher", "validate"],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            print("  ⚠ mcp-publisher not authenticated. Starting login...")
            login_result = subprocess.run(
                ["mcp-publisher", "login", "github"], check=False
            )
            if login_result.returncode != 0:
                print("Warning: mcp-publisher authentication failed.")
                print("         MCP Registry publish may be skipped.")
            else:
                print("  ✓ mcp-publisher authenticated")
        else:
            print("  ✓ mcp-publisher available")


def create_release_branch(new_version: str, dry_run: bool) -> str:
    """Create release branch from develop."""
    print("\n=== Create Release Branch ===\n")

    branch_name = f"release/v{new_version}"

    if dry_run:
        print(f"  [DRY-RUN] Would create branch: {branch_name}")
    else:
        run_command(["git", "checkout", "-b", branch_name])
        print(f"  ✓ Created and switched to: {branch_name}")

    return branch_name


def update_pyproject_toml(project_root: Path, new_version: str, dry_run: bool) -> None:
    """Update version in pyproject.toml."""
    pyproject = project_root / "pyproject.toml"
    content = pyproject.read_text()
    new_content = re.sub(
        r'^(version\s*=\s*)"[^"]+"',
        f'\\1"{new_version}"',
        content,
        flags=re.MULTILINE,
    )

    if dry_run:
        print(f"  [DRY-RUN] Would update pyproject.toml version to {new_version}")
    else:
        pyproject.write_text(new_content)
        print("  ✓ Updated pyproject.toml")


def update_server_json(project_root: Path, new_version: str, dry_run: bool) -> None:
    """Update version in server.json (both occurrences)."""
    server_json = project_root / "server.json"
    content = json.loads(server_json.read_text())

    content["version"] = new_version
    if "packages" in content and len(content["packages"]) > 0:
        content["packages"][0]["version"] = new_version

    if dry_run:
        print(f"  [DRY-RUN] Would update server.json version to {new_version}")
    else:
        server_json.write_text(json.dumps(content, indent=2) + "\n")
        print("  ✓ Updated server.json")


def update_init_py(project_root: Path, new_version: str, dry_run: bool) -> None:
    """Update __version__ in __init__.py."""
    init_py = project_root / "src" / "pdf_mcp" / "__init__.py"
    content = init_py.read_text()
    new_content = re.sub(
        r'^(__version__\s*=\s*)"[^"]+"',
        f'\\1"{new_version}"',
        content,
        flags=re.MULTILINE,
    )

    if dry_run:
        print(f"  [DRY-RUN] Would update __init__.py version to {new_version}")
    else:
        init_py.write_text(new_content)
        print("  ✓ Updated __init__.py")


def update_changelog(project_root: Path, new_version: str, dry_run: bool) -> None:
    """Update CHANGELOG.md: add new version header with today's date."""
    changelog = project_root / "CHANGELOG.md"
    content = changelog.read_text()
    today = date.today().strftime("%Y-%m-%d")

    # Require an [Unreleased] section with real content. Auto-stamping a
    # "Version bump" placeholder produces vacuous GitHub release notes
    # (see v1.11.0 incident); fail loud instead so the user fills it in.
    unreleased_pattern = r"## \[Unreleased\]\s*\n(.*?)(?=^## \[|\Z)"
    match = re.search(
        unreleased_pattern, content, re.IGNORECASE | re.DOTALL | re.MULTILINE
    )
    if not match:
        print(
            "Error: CHANGELOG.md has no [Unreleased] section.\n"
            "Add one with the changes for this release before running the script."
        )
        sys.exit(1)

    unreleased_body = match.group(1).strip()
    if not unreleased_body or unreleased_body in ("### Changed", "### Added"):
        print(
            "Error: [Unreleased] section in CHANGELOG.md is empty.\n"
            "Document the changes for this release before running the script."
        )
        sys.exit(1)

    new_content = re.sub(
        r"## \[Unreleased\]\s*\n",
        f"## [{new_version}] - {today}\n",
        content,
        count=1,
        flags=re.IGNORECASE,
    )

    if dry_run:
        print(f"  [DRY-RUN] Would update CHANGELOG.md with version {new_version}")
    else:
        changelog.write_text(new_content)
        print("  ✓ Updated CHANGELOG.md")


def extract_changelog_section(project_root: Path, version: str) -> str:
    """Extract the changelog section for a specific version."""
    changelog = project_root / "CHANGELOG.md"
    content = changelog.read_text()

    # Find the section for this version
    pattern = rf"## \[{re.escape(version)}\][^\n]*\n(.*?)(?=\n## \[|\Z)"
    match = re.search(pattern, content, re.DOTALL)

    if match:
        return match.group(1).strip()
    return "Release " + version


def extract_unreleased_section(project_root: Path) -> str:
    """Extract the [Unreleased] body (dry-run preview source)."""
    content = (project_root / "CHANGELOG.md").read_text()
    match = re.search(
        r"## \[Unreleased\]\s*\n(.*?)(?=^## \[|\Z)",
        content,
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def notes_file_path(project_root: Path, new_version: str) -> Path:
    """Path where approved release notes are persisted for crash recovery."""
    return project_root / f"release_notes_v{new_version}.md"


def _install_and_links_section(new_version: str) -> str:
    """Deterministic Installation/Links tail shared by all notes formats."""
    return f"""## Installation

```bash
pip install pdf-mcp=={new_version}
```

## Links
- [PyPI Package](https://pypi.org/project/pdf-mcp/{new_version}/)
- [MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=pdf-mcp)
- [Full Changelog](https://github.com/jztan/pdf-mcp/blob/master/CHANGELOG.md)
"""


def build_release_body(generated: str, new_version: str) -> str:
    """Compose final notes: Claude-generated sections + deterministic tail."""
    return f"{generated.strip()}\n\n{_install_and_links_section(new_version)}"


def build_raw_release_body(changelog_section: str, new_version: str) -> str:
    """Today's raw-changelog notes format (fallback path)."""
    tag = f"v{new_version}"
    return f"""## What's New in {tag}

{changelog_section}

{_install_and_links_section(new_version)}"""


NOTES_GENERATION_TIMEOUT = 120

# Deny the tools `claude -p` could plausibly reach for (a pure text
# transform needs none). The prompt also instructs it not to use tools.
NOTES_DENIED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite"

# .format() template -- must contain no literal braces (a brace-containing
# example added later would raise KeyError at release time).
RELEASE_NOTES_PROMPT = """\
You are writing the GitHub release notes for pdf-mcp {tag}, an MCP server
that gives AI agents tools to read, search, and extract content from PDFs.

Rewrite the changelog section below into product-style release notes for
users deciding whether to upgrade. Respond directly with the notes only --
do not use any tools.

Output contract (follow exactly):
- First line: `TITLE: {tag} — <short headline, at most 8 words>`
- Then a `## Highlights` section: 2-4 short paragraphs, at most 150 words
  total. Lead with what is new and why a user would care; keep measurable
  wins (speedups, accuracy numbers).
- Then a `## Changes` section: condensed one-line bullets grouped under
  `### Added` / `### Fixed` / `### Changed` / `### Security` (omit empty
  groups). No internal mechanics: no PRAGMA names, no source file paths,
  no helper function names.

Hard rules:
- Use only facts present in the changelog section. Never invent features,
  benefits, or numbers.
- Keep every number exactly as written in the changelog.
- Plain markdown only. No H1 headings. Do not add Installation or Links
  sections (they are appended separately).

Changelog section for {tag}:

{changelog_section}
"""


def _parse_title(output: str, tag: str) -> tuple[str, str]:
    """Split the TITLE: first-line contract off the draft.

    Malformed or missing title falls back to the bare tag with the full
    output as body.
    """
    lines = output.splitlines()
    first = lines[0].strip() if lines else ""
    if first.upper().startswith("TITLE:"):
        title = first[len("TITLE:") :].strip()
        if title:
            body = "\n".join(lines[1:]).strip()
            return title, body
    return tag, output


def generate_release_notes(
    new_version: str,
    changelog_section: str,
    steering: str | None = None,
) -> tuple[str, str]:
    """Draft release notes with `claude -p`. Returns (title, generated_body).

    Raises RuntimeError on any failure (missing CLI, timeout, non-zero
    exit, empty output) so callers can decide between retry and fallback.
    """
    tag = f"v{new_version}"
    prompt = RELEASE_NOTES_PROMPT.format(tag=tag, changelog_section=changelog_section)
    if steering:
        prompt += f"\nAdditional guidance from the maintainer: {steering}\n"

    try:
        result = subprocess.run(
            ["claude", "-p", "--disallowedTools", NOTES_DENIED_TOOLS],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=NOTES_GENERATION_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("'claude' CLI not found on PATH") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to launch 'claude' CLI: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"claude -p timed out after {NOTES_GENERATION_TIMEOUT}s"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {result.returncode}: {result.stderr.strip()}"
        )
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("claude -p returned empty output")
    return _parse_title(output, tag)


def write_notes_file(path: Path, title: str, body: str) -> None:
    """Persist approved notes; first line is an invisible title comment."""
    path.write_text(f"<!-- title: {title} -->\n{body.strip()}\n")


def read_notes_file(path: Path) -> tuple[str | None, str]:
    """Read persisted notes back. Returns (title or None, body)."""
    content = path.read_text()
    match = re.match(r"<!-- title: (.*?) -->\n", content)
    if match:
        return match.group(1), content[match.end() :].strip()
    return None, content.strip()


def _edit_notes_in_editor(path: Path) -> None:
    """Open the notes file in $VISUAL/$EDITOR (fallback vi). Blocking."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.run([*shlex.split(editor), str(path)], check=False)


def approve_release_notes(
    new_version: str, changelog_section: str, notes_path: Path
) -> None:
    """Generate, approve, and persist release notes to notes_path.

    Interactive (TTY): [y]es / [e]dit in $EDITOR (edit is final) /
    [r]egenerate with optional steering / [f]allback to raw changelog.
    Non-TTY: auto-accept the draft. Generation failure: prompt
    retry/fallback on a TTY, silent fallback otherwise. Never blocks the
    release on the LLM.
    """
    tag = f"v{new_version}"
    interactive = sys.stdin.isatty()
    steering: str | None = None

    while True:
        print("  Generating release notes draft (claude -p)...")
        try:
            title, generated = generate_release_notes(
                new_version, changelog_section, steering
            )
        except RuntimeError as exc:
            print(f"  ⚠ Notes generation failed: {exc}")
            if interactive:
                while True:
                    choice = (
                        input("  [r]etry / [f]all back to raw changelog? ")
                        .strip()
                        .lower()
                    )
                    if choice in ("r", "f"):
                        break
                    print("  Please answer r or f.")
                if choice == "r":
                    continue
            print("  Falling back to raw changelog notes.")
            write_notes_file(
                notes_path,
                tag,
                build_raw_release_body(changelog_section, new_version),
            )
            return

        body = build_release_body(generated, new_version)
        print("\n" + "-" * 60)
        print(f"  Title: {title}")
        print("-" * 60)
        print(body)
        print("-" * 60)

        if not interactive:
            print("  ⚠ Non-interactive run: auto-accepting generated notes.")
            write_notes_file(notes_path, title, body)
            return

        while True:
            choice = (
                input("  [y]es publish / [e]dit / [r]egenerate / [f]allback? ")
                .strip()
                .lower()
            )
            if choice == "y":
                write_notes_file(notes_path, title, body)
                return
            if choice == "e":
                # Saving in the editor IS the approval -- no re-confirm.
                write_notes_file(notes_path, title, body)
                try:
                    _edit_notes_in_editor(notes_path)
                except (ValueError, OSError) as exc:
                    print(f"  ⚠ Could not launch editor: {exc}")
                    print("  Using the unedited draft as approved.")
                else:
                    print(f"  ✓ Using edited notes from {notes_path}")
                return
            if choice == "r":
                steering = input("  Any guidance? (enter to skip) ").strip() or None
                break
            if choice == "f":
                write_notes_file(
                    notes_path,
                    tag,
                    build_raw_release_body(changelog_section, new_version),
                )
                return
            print("  Please answer y, e, r, or f.")


def preview_release_notes(config: ReleaseConfig, new_version: str) -> None:
    """Dry-run: draft real notes from [Unreleased] and print them."""
    print("\n=== Release Notes (Preview) ===\n")
    section = extract_unreleased_section(config.project_root)
    try:
        title, generated = generate_release_notes(new_version, section)
    except RuntimeError as exc:
        print(f"  ⚠ Notes generation failed: {exc}")
        print("  [DRY-RUN] Release would fall back to raw changelog notes.")
        return
    body = build_release_body(generated, new_version)
    print(f"  [DRY-RUN] Title: {title}")
    print("  [DRY-RUN] Notes preview:\n")
    print(body)


def regenerate_uv_lock(config: ReleaseConfig) -> None:
    """Regenerate uv.lock after version bump."""
    if config.dry_run:
        print("  [DRY-RUN] Would run: uv lock")
    else:
        run_command(["uv", "lock"])
        print("  ✓ Regenerated uv.lock")


def commit_version_bump(config: ReleaseConfig, new_version: str) -> None:
    """Commit version bump changes on release branch."""
    print("\n=== Commit Version Bump ===\n")

    regenerate_uv_lock(config)

    # Stage changes
    files = [
        "pyproject.toml",
        "server.json",
        "src/pdf_mcp/__init__.py",
        "CHANGELOG.md",
        "uv.lock",
    ]
    for f in files:
        run_command(
            ["git", "add", f],
            dry_run=config.dry_run,
            dry_run_msg=f"git add {f}",
        )
    if config.dry_run:
        print(f"  [DRY-RUN] Would stage: {', '.join(files)}")
    else:
        print("  ✓ Staged changes")

    # Commit
    commit_msg = f"chore: bump version to v{new_version}"
    run_command(
        ["git", "commit", "-m", commit_msg],
        dry_run=config.dry_run,
        dry_run_msg=f"git commit -m '{commit_msg}'",
    )
    if config.dry_run:
        print(f"  [DRY-RUN] Would commit: {commit_msg}")
    else:
        print("  ✓ Committed changes")


def merge_to_master_and_tag(
    config: ReleaseConfig, new_version: str, release_branch: str
) -> None:
    """Merge release branch to master and create tag."""
    print("\n=== Merge to Master & Tag ===\n")

    tag = f"v{new_version}"

    if config.dry_run:
        print("  [DRY-RUN] Would checkout master")
        print(f"  [DRY-RUN] Would merge {release_branch} into master")
        print(f"  [DRY-RUN] Would create tag: {tag}")
        print("  [DRY-RUN] Would push master with tags")
    else:
        # Checkout master and pull
        run_command(["git", "checkout", "master"])
        run_command(["git", "pull", "origin", "master"])
        print("  ✓ Checked out master")

        # Merge release branch
        run_command(["git", "merge", release_branch, "--no-edit"])
        print(f"  ✓ Merged {release_branch}")

        # Create annotated tag
        run_command(["git", "tag", "-a", tag, "-m", f"Release {tag}"])
        print(f"  ✓ Created tag: {tag}")

        # Push master with tags (triggers PyPI workflow)
        run_command(["git", "push", "origin", "master", "--tags"])
        print("  ✓ Pushed master with tags")


def create_github_release(config: ReleaseConfig, new_version: str) -> None:
    """Create GitHub release from approved notes (fallback: raw changelog)."""
    print("\n=== GitHub Release ===\n")

    tag = f"v{new_version}"
    notes_path = notes_file_path(config.project_root, new_version)
    if notes_path.exists():
        stored_title, notes = read_notes_file(notes_path)
        title = stored_title or tag
    else:
        # Dry-run, or a real run where the notes step was somehow skipped.
        if not config.dry_run:
            print("  ⚠ Approved notes file missing; falling back to raw changelog")
        title = tag
        changelog_section = extract_changelog_section(config.project_root, new_version)
        notes = build_raw_release_body(changelog_section, new_version)

    if config.dry_run:
        print(f"  [DRY-RUN] Would create GitHub release: {tag}")
        if notes_path.exists():
            print(f"  [DRY-RUN] Title: {title}")
            print("  [DRY-RUN] Release notes preview:")
            for line in notes.split("\n")[:10]:
                print(f"    {line}")
            print("    ...")
        else:
            print(
                "  [DRY-RUN] Title and notes would come from the approved "
                "notes file (see the Release Notes preview above)."
            )
    else:
        result = run_command(
            ["gh", "release", "create", tag, "--title", title, "--notes", notes],
            check=False,
        )
        if result.returncode != 0:
            print("  ✗ gh release create failed:")
            print(f"    {result.stderr.strip()}")
            print_recovery_instructions(
                new_version, f"release/v{new_version}", config.project_root
            )
            sys.exit(1)
        print(f"  ✓ Created GitHub release: {tag} — {title}")


def wait_for_publish_workflow(new_version: str, max_wait: int = 900) -> bool:
    """Poll the publish-pypi.yml run triggered by the tag push.

    Returns True only if the workflow run on the tag's SHA completes with
    conclusion=success. Returns False on any other terminal conclusion or
    on timeout. Prints the run URL so a failure is easy to investigate.
    """
    tag = f"v{new_version}"
    print("\n=== Publish Workflow ===\n")

    # Annotated tags have their own object SHA; GitHub Actions reports the
    # commit SHA in headSha. Dereference with ^{commit} so the match works.
    sha_result = run_command(["git", "rev-parse", f"{tag}^{{commit}}"])
    tag_sha = sha_result.stdout.strip()
    print(f"  Watching publish-pypi.yml for tag {tag} ({tag_sha[:7]})...")

    start_time = time.time()
    check_interval = 15
    last_status: str | None = None

    while time.time() - start_time < max_wait:
        elapsed = int(time.time() - start_time)
        result = run_command(
            [
                "gh",
                "run",
                "list",
                "--workflow=publish-pypi.yml",
                "--limit",
                "10",
                "--json",
                "databaseId,status,conclusion,headSha,url",
            ],
            check=False,
        )
        if result.returncode != 0:
            print(f"  ⚠ gh run list failed: {result.stderr.strip()}; retrying...")
            time.sleep(check_interval)
            continue

        try:
            runs = json.loads(result.stdout)
        except json.JSONDecodeError:
            time.sleep(check_interval)
            continue

        match = next((r for r in runs if r.get("headSha") == tag_sha), None)
        if match is None:
            print(f"  Run not yet registered... ({elapsed}s elapsed)")
            time.sleep(10)
            continue

        status = match.get("status")
        conclusion = match.get("conclusion")
        url = match.get("url", "")
        run_id = match.get("databaseId")

        if status == "completed":
            if conclusion == "success":
                print(f"  ✓ publish-pypi run succeeded ({url})")
                return True
            print(f"  ✗ publish-pypi run finished with conclusion={conclusion}")
            print(f"    URL: {url}")
            print(f"    Logs: gh run view {run_id} --log-failed")
            return False

        if status != last_status:
            print(f"  Run {run_id} status={status}... ({elapsed}s elapsed)")
            last_status = status
        else:
            print(f"  Still {status}... ({elapsed}s elapsed)")
        time.sleep(check_interval)

    print(f"  ⚠ Timeout after {max_wait}s waiting for publish-pypi workflow")
    return False


def print_recovery_instructions(
    new_version: str, release_branch: str, project_root: Path
) -> None:
    """Print actionable recovery steps when the publish step fails after tagging."""
    tag = f"v{new_version}"
    print("\n" + "=" * 60)
    print(f"  RELEASE {tag} INCOMPLETE")
    print("=" * 60)
    print()
    print("  Current state:")
    print(f"    - Tag {tag} is pushed to origin")
    print(f"    - master has the version-bump commit for {tag}")
    print("    - GitHub release was NOT created")
    print("    - MCP Registry was NOT updated")
    print(f"    - develop is unchanged; {release_branch} still exists locally")
    print()
    print("  Investigate:")
    print("    gh run list --workflow=publish-pypi.yml --limit 5")
    print("    gh run view <run-id> --log-failed")
    print()
    print("  After diagnosing, choose one:")
    print("    A) Fix the cause on develop, then rerun the existing tag's workflow:")
    print("         gh run rerun <run-id>")
    print("       On success, finish the remaining steps manually:")
    notes_path = notes_file_path(project_root, new_version)
    if notes_path.exists():
        stored_title, _ = read_notes_file(notes_path)
        title = stored_title or tag
        print(f"         gh release create {tag} --title {shlex.quote(title)} \\")
        print(f'           --notes-file "{notes_path}"')
        print(f"         (your approved notes are saved at {notes_path})")
    else:
        print(
            f"         gh release create {tag} --title {tag} " "--notes-file <notes.md>"
        )
    print("         mcp-publisher publish")
    print(f"         git checkout develop && git merge {release_branch} && git push")
    print(f"         git branch -d {release_branch}")
    print("    B) Burn this version and ship the next patch from develop")
    print("       (requires deleting tag + reverting the master commit — destructive,")
    print(f"        only do this if nobody has pulled {tag}).")
    print("=" * 60)


def wait_for_pypi(new_version: str, max_wait: int = 600) -> bool:
    """Wait for package to be available on PyPI.

    Checks the JSON API endpoint that the MCP Registry actually validates
    against. `pip index versions` can return false positives because the
    simple index updates before the JSON API does.
    """
    import urllib.request
    import urllib.error

    print("\n=== Waiting for PyPI ===\n")

    url = f"https://pypi.org/pypi/pdf-mcp/{new_version}/json"
    start_time = time.time()
    check_interval = 15

    while time.time() - start_time < max_wait:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                if resp.status == 200:
                    print(f"  ✓ Version {new_version} is available on PyPI")
                    return True
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"  Unexpected status {e.code}, retrying...")
        except urllib.error.URLError as e:
            print(f"  Network error: {e.reason}, retrying...")

        elapsed = int(time.time() - start_time)
        print(f"  Waiting for PyPI... ({elapsed}s elapsed)")
        time.sleep(check_interval)

    print("  ⚠ Timeout waiting for PyPI. Package may not be available yet.")
    return False


def publish_mcp_registry(config: ReleaseConfig) -> None:
    """Publish to MCP Registry."""
    print("\n=== MCP Registry ===\n")

    # Check if mcp-publisher is available
    result = run_command(["which", "mcp-publisher"], check=False)
    if result.returncode != 0:
        print("  ⚠ mcp-publisher not found. Skipping MCP Registry publish.")
        print("  Install with: brew install mcp-publisher")
        return

    if config.dry_run:
        print("  [DRY-RUN] Would run: mcp-publisher publish")
    else:
        result = run_command(
            ["mcp-publisher", "publish"],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            print("  ✓ Published to MCP Registry")
        else:
            print(f"  ⚠ MCP Registry publish failed: {result.stderr}")
            print("  You may need to run 'mcp-publisher login github' first")


def merge_back_to_develop(config: ReleaseConfig, release_branch: str) -> None:
    """Merge release branch back to develop and cleanup."""
    print("\n=== Merge Back to Develop ===\n")

    if config.dry_run:
        print("  [DRY-RUN] Would checkout develop")
        print(f"  [DRY-RUN] Would merge {release_branch} into develop")
        print("  [DRY-RUN] Would push develop")
        print(f"  [DRY-RUN] Would delete branch {release_branch}")
    else:
        # Checkout develop and pull
        run_command(["git", "checkout", "develop"])
        run_command(["git", "pull", "origin", "develop"])
        print("  ✓ Checked out develop")

        # Merge release branch
        run_command(["git", "merge", release_branch, "--no-edit"])
        print(f"  ✓ Merged {release_branch}")

        # Push develop
        run_command(["git", "push", "origin", "develop"])
        print("  ✓ Pushed develop")

        # Delete release branch locally and remotely
        run_command(["git", "branch", "-d", release_branch])
        run_command(["git", "push", "origin", "--delete", release_branch], check=False)
        print(f"  ✓ Deleted branch: {release_branch}")


def main() -> None:
    """Main entry point for release automation."""
    parser = argparse.ArgumentParser(
        description="Release automation for pdf-mcp (gitflow)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/release.py patch           # 1.1.1 -> 1.1.2
  python scripts/release.py minor           # 1.1.1 -> 1.2.0
  python scripts/release.py major           # 1.1.1 -> 2.0.0
  python scripts/release.py patch --dry-run # Preview changes

Gitflow:
  develop -> release/vX.Y.Z -> master (tagged) -> merge back to develop
        """,
    )
    parser.add_argument(
        "bump_type",
        choices=["patch", "minor", "major"],
        help="Version bump type",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without executing",
    )

    args = parser.parse_args()

    # Determine project root (parent of scripts directory)
    project_root = Path(__file__).parent.parent.resolve()

    config = ReleaseConfig(
        bump_type=args.bump_type,
        dry_run=args.dry_run,
        project_root=project_root,
    )

    print("=" * 60)
    print("  pdf-mcp Release Automation (Gitflow)")
    print("=" * 60)

    if config.dry_run:
        print("\n  ⚠️  DRY-RUN MODE - No changes will be made\n")

    # Step 1: Pre-flight checks (must be on develop, tests pass, tools available)
    preflight_checks()

    # Step 2: Calculate new version
    current_version = get_current_version(config.project_root)
    new_version = calculate_new_version(current_version, config.bump_type)

    # Step 3: Create release branch
    release_branch = create_release_branch(new_version, config.dry_run)

    # Step 4: Bump version in files
    print("\n=== Version Bump ===\n")
    print(f"Version: {current_version} -> {new_version}")
    print()
    update_pyproject_toml(config.project_root, new_version, config.dry_run)
    update_server_json(config.project_root, new_version, config.dry_run)
    update_init_py(config.project_root, new_version, config.dry_run)
    update_changelog(config.project_root, new_version, config.dry_run)

    # Step 5: Commit version bump on release branch
    commit_version_bump(config, new_version)

    # Step 5b: Draft + approve release notes NOW, so the long unattended
    # waits (publish workflow, PyPI) happen after the human interaction.
    notes_path = notes_file_path(config.project_root, new_version)
    if config.dry_run:
        preview_release_notes(config, new_version)
    else:
        print("\n=== Release Notes ===\n")
        changelog_section = extract_changelog_section(config.project_root, new_version)
        approve_release_notes(new_version, changelog_section, notes_path)
        print(f"  ✓ Approved notes saved to {notes_path}")

    # Step 6: Merge to master and tag (this push triggers publish-pypi.yml)
    merge_to_master_and_tag(config, new_version, release_branch)

    # Step 7: Watch the publish-pypi workflow. The GitHub release is created
    # AFTER PyPI confirms — otherwise the release notes ship a `pip install`
    # line that does not yet (or may never) work.
    if not config.dry_run:
        if not wait_for_publish_workflow(new_version):
            print_recovery_instructions(
                new_version, release_branch, config.project_root
            )
            sys.exit(1)

        if not wait_for_pypi(new_version):
            print_recovery_instructions(
                new_version, release_branch, config.project_root
            )
            sys.exit(1)
    else:
        print("\n=== Publish Workflow ===\n")
        print("  [DRY-RUN] Would poll publish-pypi.yml run to completion")
        print("\n=== Waiting for PyPI ===\n")
        print("  [DRY-RUN] Would wait for PyPI availability")

    # Step 8: Create GitHub release — now safe to advertise the PyPI install
    create_github_release(config, new_version)

    # Step 9: Publish to MCP Registry
    if not config.dry_run:
        publish_mcp_registry(config)
    else:
        print("\n=== MCP Registry ===\n")
        print("  [DRY-RUN] Would run: mcp-publisher publish")

    # Step 10: Merge back to develop and cleanup
    merge_back_to_develop(config, release_branch)

    # Release fully succeeded; the persisted notes drafts (including any
    # orphans from previously burned versions) are no longer needed.
    if not config.dry_run:
        for stale_notes in config.project_root.glob("release_notes_v*.md"):
            stale_notes.unlink()

    # Done!
    print("\n" + "=" * 60)
    if config.dry_run:
        print("  DRY-RUN COMPLETE - No changes were made")
    else:
        print(f"  RELEASE v{new_version} COMPLETE!")
        print()
        print("  Verify at:")
        print(f"    - PyPI: https://pypi.org/project/pdf-mcp/{new_version}/")
        gh_url = f"https://github.com/jztan/pdf-mcp/releases/tag/v{new_version}"
        print(f"    - GitHub: {gh_url}")
        mcp_url = "https://registry.modelcontextprotocol.io/v0/servers?search=pdf-mcp"
        print(f"    - MCP Registry: {mcp_url}")
    print("=" * 60)


if __name__ == "__main__":
    main()
