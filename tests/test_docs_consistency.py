"""Docs-vs-code consistency checks.

These pin the enumerable facts that committed docs restate about the code:
the tool list, the released version, and internal link anchors. Each one
here corresponds to a drift that actually shipped. `pdf_extract_chart` was
absent from the tool-reference index for three releases despite having a
documented section, and ROADMAP's "Current version" trailed a release.

Prose drift is out of scope; only mechanically checkable claims belong here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pdf_mcp.server import mcp

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"

# Docs that are checked for resolvable internal anchors.
LINKED_DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "SECURITY.md",
    *sorted(DOCS.glob("*.md")),
]

# ](target#anchor) or ](#anchor), skipping external URLs.
_LINK_RE = re.compile(r"\]\((?!https?://)([^)\s#]*)#([^)\s]+)\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.M)


def _slugify(heading: str) -> str:
    """Approximate GitHub's heading-to-anchor slug."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)  # drop punctuation, keep word chars
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    return {_slugify(h) for h in _HEADING_RE.findall(path.read_text())}


def _tool_names() -> set[str]:
    """Probe FastMCP's tool registry across known v3 layouts.

    Mirrors the probe in test_tool_descriptions.py; pytest's import mode
    here does not allow importing one test module from another.
    """
    for attr in ("_tool_manager", "tool_manager"):
        mgr = getattr(mcp, attr, None)
        if mgr is not None and hasattr(mgr, "_tools"):
            return set(mgr._tools)

    providers = getattr(mcp, "providers", None)
    if providers:
        components = getattr(providers[0], "_components", None)
        if components is not None:
            names = {
                k[len("tool:") : k.index("@")]
                for k in components
                if k.startswith("tool:")
            }
            if names:
                return names

    raise AssertionError("Could not locate FastMCP tool registry")


def _project_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return str(data["project"]["version"])


def test_tool_reference_indexes_every_tool() -> None:
    """The category table at the top of tool-reference.md must list every
    tool that has a section in the same file, and nothing else."""
    text = (DOCS / "tool-reference.md").read_text()

    indexed: set[str] = set()
    for line in text.splitlines():
        if line.startswith("|") and "](#" in line:
            indexed |= set(re.findall(r"`([a-z_]+)`", line))

    documented = set(re.findall(r"^### `([a-z_]+)`", text, re.M))

    assert indexed, "no category-table rows found in tool-reference.md"
    assert indexed == documented, (
        "tool-reference.md index is out of sync with its own sections. "
        f"Indexed but undocumented: {sorted(indexed - documented)}. "
        f"Documented but unindexed: {sorted(documented - indexed)}."
    )


def test_tool_reference_documents_every_registered_tool() -> None:
    """Every tool the server registers must have a section, and no section
    may describe a tool that no longer exists."""
    text = (DOCS / "tool-reference.md").read_text()
    documented = set(re.findall(r"^### `([a-z_]+)`", text, re.M))
    registered = _tool_names()

    assert documented == registered, (
        "tool-reference.md does not match the registered tools. "
        f"Registered but undocumented: {sorted(registered - documented)}. "
        f"Documented but unregistered: {sorted(documented - registered)}."
    )


def test_roadmap_current_version_matches_pyproject() -> None:
    """ROADMAP's Project Status must name the version actually in
    pyproject.toml, so a release bump cannot leave it stale."""
    text = (DOCS / "ROADMAP.md").read_text()
    match = re.search(r"\*\*Current version:\*\*\s*v?([0-9]+\.[0-9]+\.[0-9]+)", text)

    assert match, "ROADMAP.md has no '**Current version:**' line to check"
    assert match.group(1) == _project_version(), (
        f"ROADMAP.md says v{match.group(1)} but pyproject.toml is "
        f"{_project_version()}. Sync Project Status when cutting a release."
    )


def test_documented_tool_counts_match_the_registry() -> None:
    """README and ROADMAP both hardcode the number of tools in prose."""
    expected = len(_tool_names())

    readme = (REPO_ROOT / "README.md").read_text()
    counts = {int(n) for n in re.findall(r"(\d+) specialized tools", readme)}
    assert counts == {expected}, (
        f"README.md claims {sorted(counts)} specialized tools, "
        f"but {expected} are registered."
    )

    roadmap = (DOCS / "ROADMAP.md").read_text()
    match = re.search(r"\*\*Tools:\*\*\s*(\d+) released \(([^)]*)\)", roadmap)
    assert match, "ROADMAP.md has no '**Tools:** N released (...)' line"
    assert (
        int(match.group(1)) == expected
    ), f"ROADMAP.md claims {match.group(1)} tools, {expected} are registered."
    listed = set(re.findall(r"`([a-z_]+)`", match.group(2)))
    assert listed == _tool_names(), (
        "ROADMAP.md's tool list is out of sync. "
        f"Missing: {sorted(_tool_names() - listed)}. "
        f"Unexpected: {sorted(listed - _tool_names())}."
    )


def test_internal_doc_anchors_resolve() -> None:
    """Every ](file.md#anchor) and ](#anchor) in the public docs must point
    at a heading that exists, so moving a section cannot silently break a
    cross-reference."""
    broken: list[str] = []

    for doc in LINKED_DOCS:
        for target, anchor in _LINK_RE.findall(doc.read_text()):
            dest = doc if not target else (doc.parent / target).resolve()
            rel = doc.relative_to(REPO_ROOT)
            if not dest.is_file():
                broken.append(f"{rel}: link target missing: {target}")
                continue
            if anchor.lower() not in _anchors(dest):
                broken.append(f"{rel}: no heading for #{anchor} in {target or rel}")

    assert not broken, "unresolvable doc links:\n  " + "\n  ".join(broken)
