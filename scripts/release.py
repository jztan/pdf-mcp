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
    2. Create release/vX.Y.Z branch
    3. Bump versions on release branch
    4. Merge to master and tag
    5. Create GitHub release (triggers PyPI publish)
    6. Publish to MCP Registry
    7. Merge back to develop
    8. Delete release branch
"""

import argparse
import json
import re
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
        print(f"Error: Must be on 'develop' branch to start release, currently on '{branch}'")
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
        print("Warning: 'mcp-publisher' not found. MCP Registry publish will be skipped.")
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
            login_result = subprocess.run(["mcp-publisher", "login", "github"], check=False)
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


def update_changelog(project_root: Path, new_version: str, dry_run: bool) -> None:
    """Update CHANGELOG.md: add new version header with today's date."""
    changelog = project_root / "CHANGELOG.md"
    content = changelog.read_text()
    today = date.today().strftime("%Y-%m-%d")

    # Check if there's an [Unreleased] section to convert
    unreleased_pattern = r"## \[Unreleased\]\s*\n"
    if re.search(unreleased_pattern, content, re.IGNORECASE):
        # Replace [Unreleased] with new version
        new_content = re.sub(
            unreleased_pattern,
            f"## [{new_version}] - {today}\n",
            content,
            flags=re.IGNORECASE,
        )
    else:
        # No Unreleased section - add new version section after header
        # Find the first version entry and insert before it
        first_version_match = re.search(r"^## \[\d+\.\d+\.\d+\]", content, re.MULTILINE)
        if first_version_match:
            insert_pos = first_version_match.start()
            new_section = f"## [{new_version}] - {today}\n\n### Changed\n- Version bump\n\n"
            new_content = content[:insert_pos] + new_section + content[insert_pos:]
        else:
            print("Error: Could not find where to insert new version in CHANGELOG.md")
            sys.exit(1)

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


def bump_version(config: ReleaseConfig) -> tuple[str, str]:
    """Update version in all files."""
    print("\n=== Version Bump ===\n")

    current_version = get_current_version(config.project_root)
    new_version = calculate_new_version(current_version, config.bump_type)

    print(f"Version: {current_version} -> {new_version}")
    print()

    update_pyproject_toml(config.project_root, new_version, config.dry_run)
    update_server_json(config.project_root, new_version, config.dry_run)
    update_changelog(config.project_root, new_version, config.dry_run)

    return current_version, new_version


def commit_version_bump(config: ReleaseConfig, new_version: str) -> None:
    """Commit version bump changes on release branch."""
    print("\n=== Commit Version Bump ===\n")

    # Stage changes
    files = ["pyproject.toml", "server.json", "CHANGELOG.md"]
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


def merge_to_master_and_tag(config: ReleaseConfig, new_version: str, release_branch: str) -> None:
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
    """Create GitHub release with changelog notes."""
    print("\n=== GitHub Release ===\n")

    tag = f"v{new_version}"
    changelog_section = extract_changelog_section(config.project_root, new_version)

    # Build release notes
    notes = f"""## What's New in {tag}

{changelog_section}

## Installation

```bash
pip install pdf-mcp=={new_version}
```

## Links
- [PyPI Package](https://pypi.org/project/pdf-mcp/{new_version}/)
- [MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=pdf-mcp)
- [Full Changelog](https://github.com/jztan/pdf-mcp/blob/master/CHANGELOG.md)
"""

    if config.dry_run:
        print(f"  [DRY-RUN] Would create GitHub release: {tag}")
        print("  [DRY-RUN] Release notes preview:")
        for line in notes.split("\n")[:10]:
            print(f"    {line}")
        print("    ...")
    else:
        run_command(
            ["gh", "release", "create", tag, "--title", tag, "--notes", notes],
        )
        print(f"  ✓ Created GitHub release: {tag}")


def wait_for_pypi(new_version: str, max_wait: int = 300) -> bool:
    """Wait for package to be available on PyPI."""
    print("\n=== Waiting for PyPI ===\n")

    start_time = time.time()
    check_interval = 15

    while time.time() - start_time < max_wait:
        result = run_command(
            ["pip", "index", "versions", "pdf-mcp"],
            check=False,
            capture_output=True,
        )
        if new_version in result.stdout:
            print(f"  ✓ Version {new_version} is available on PyPI")
            return True

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
    update_changelog(config.project_root, new_version, config.dry_run)

    # Step 5: Commit version bump on release branch
    commit_version_bump(config, new_version)

    # Step 6: Merge to master and tag
    merge_to_master_and_tag(config, new_version, release_branch)

    # Step 7: Create GitHub release
    create_github_release(config, new_version)

    # Step 8: Wait for PyPI and publish to MCP Registry
    if not config.dry_run:
        if wait_for_pypi(new_version):
            publish_mcp_registry(config)
        else:
            print("\n  ⚠ Skipping MCP Registry (PyPI not ready)")
            print("  Run manually later: mcp-publisher publish")
    else:
        print("\n=== Waiting for PyPI ===\n")
        print("  [DRY-RUN] Would wait for PyPI availability")
        print("\n=== MCP Registry ===\n")
        print("  [DRY-RUN] Would run: mcp-publisher publish")

    # Step 9: Merge back to develop and cleanup
    merge_back_to_develop(config, release_branch)

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
