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
# Queries osv.dev rather than pip-audit's default `pypi` service. The PyPI
# Advisory Database mirrors OSV records but can carry them incompletely: for
# authlib it held only the 1.6.x affected range of GHSA-r95x-qfjj-fjj2 and
# GHSA-w8p2-r796-3vmq, omitting the second range (introduced 1.7.0, fixed
# 1.7.1). The locked 1.7.0 was affected by both and the gate still reported
# clean. Same failure mode as the ambient-interpreter case above: green for the
# wrong reason. osv.dev carries both ranges.
#
# Keep any ignore list here, in one place, so local preflight and CI cannot
# drift. There are currently no ignored vulnerabilities.
set -eo pipefail

req="$(mktemp)"
trap 'rm -f "$req"' EXIT
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o "$req"

exec pip-audit \
  --vulnerability-service osv \
  --requirement "$req" "$@"
