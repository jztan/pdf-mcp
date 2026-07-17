#!/usr/bin/env bash
# Shared pip-audit invocation. Used by:
#   - .github/workflows/ci.yml
#   - .github/workflows/dependency-review.yml
#   - .github/workflows/publish-pypi.yml
#   - scripts/release.py (preflight)
#
# Audits the project's LOCKED runtime dependency tree (what `pip install pdf-mcp`
# actually ships), NOT the ambient Python environment. Auditing the live
# interpreter is fragile: a uv-managed .venv has no pip, so bare `pip-audit`
# silently falls back to whatever global/pyenv Python is on PATH and audits
# unrelated packages. Exporting the lockfile makes the gate deterministic and
# identical locally and in CI.
#
# Keep the ignore list here, in one place, so local preflight and CI cannot drift.
#
# Ignored vulnerabilities (no upstream fix, or false-positive for our usage):
#   PYSEC-2025-183  pyjwt  transitive via mcp; no fix version published
set -eo pipefail

req="$(mktemp)"
trap 'rm -f "$req"' EXIT
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o "$req"

exec pip-audit \
  --ignore-vuln PYSEC-2025-183 \
  --requirement "$req" "$@"
