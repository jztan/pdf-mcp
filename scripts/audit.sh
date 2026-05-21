#!/usr/bin/env bash
# Shared pip-audit invocation. Used by:
#   - .github/workflows/ci.yml
#   - .github/workflows/dependency-review.yml
#   - .github/workflows/publish-pypi.yml
#   - scripts/release.py (preflight)
#
# Keep the ignore list here, in one place, so local preflight and CI cannot drift.
#
# Ignored vulnerabilities (no upstream fix, or false-positive for our usage):
#   CVE-2026-4539   pygments  dev-only transitive (pytest/rich); not shipped
#   CVE-2026-3219   pip       build-time only; not a runtime dep of pdf-mcp
#   PYSEC-2025-183  pyjwt     transitive via mcp; no fix version published
set -e
exec pip-audit \
  --ignore-vuln CVE-2026-4539 \
  --ignore-vuln CVE-2026-3219 \
  --ignore-vuln PYSEC-2025-183 \
  "$@"
