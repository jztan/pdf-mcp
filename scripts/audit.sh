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
# drift.
#
# Ignored since 2026-10-25, until Python 3.10 support is dropped or the
# Pillow 13 bump lands on its own branch: every fix below needs Python >= 3.11
# (Pillow 13.0.0, filelock 4.2.1), and Pillow 13 also forces fastembed
# 0.8.0 -> 0.9.1, an embedder change that needs the semantic ratchet first.
#
# Pillow 12.3.0 (fixed in 13.0.0): image-format parsers and encoders. pdf-mcp
# never hands Pillow an untrusted file: pdfium decodes every page and embedded
# image, and Pillow only receives the raw bitmap and writes PNG or JPEG.
pillow_ignored=(
  CVE-2026-112390
  CVE-2026-112391
  CVE-2026-112392
  CVE-2026-112393
  CVE-2026-112394
  CVE-2026-112395
  CVE-2026-112396
  CVE-2026-112397
  CVE-2026-112398
  CVE-2026-112399
  CVE-2026-112400
  CVE-2026-112401
  CVE-2026-112402
  CVE-2026-112403
  CVE-2026-112404
  CVE-2026-112405
  CVE-2026-112406
  CVE-2026-112891
  CVE-2026-112892
  CVE-2026-112893
  CVE-2026-112894
  CVE-2026-112895
  CVE-2026-112896
  CVE-2026-112897
  CVE-2026-112898
  CVE-2026-112899
  CVE-2026-112900
  CVE-2026-112901
  CVE-2026-112902
  CVE-2026-114501
  CVE-2026-96636
  CVE-2026-96637
  CVE-2026-96638
  CVE-2026-96639
  CVE-2026-96640
  CVE-2026-96641
  CVE-2026-96642
  CVE-2026-96643
  CVE-2026-96644
  CVE-2026-96778
  GHSA-78g4-xqwv-q3g8
)
# filelock 3.32.7 (fixed in 4.2.1, via huggingface-hub): each needs another
# local user to plant files beside a lock. pdf-mcp is a single-user server.
filelock_ignored=(
  CVE-2026-112907
  CVE-2026-112913
  CVE-2026-112940
)
set -eo pipefail

req="$(mktemp)"
trap 'rm -f "$req"' EXIT
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o "$req"

ignore_args=()
for id in "${pillow_ignored[@]}" "${filelock_ignored[@]}"; do
  ignore_args+=(--ignore-vuln "$id")
done

exec pip-audit \
  --vulnerability-service osv \
  "${ignore_args[@]}" \
  --requirement "$req" "$@"
