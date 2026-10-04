"""Every locked runtime pin installs from a wheel on Intel macOS.

The Claude Desktop bundle installs uv.lock's runtime pins on the user's
machine. When a pin has no wheel for that machine, uv falls back to the sdist
and compiles it. cryptography dropped Intel-Mac wheels at 49.0.0, so the
locked 50.0.0 compiled from source on every Intel-Mac first start: 185 s of a
244 s cold start on the CI runner, and a failed install on any Mac without a
Rust toolchain (issue #81). `required-environments` in pyproject.toml does not
catch this, because it only rejects pins that have no sdist either.

This test reads the lock offline: it asks uv which pins apply to Intel macOS
for each bundle Python, then checks each pin has a wheel whose tags that
interpreter accepts.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags, mac_platforms
from packaging.utils import canonicalize_name, parse_wheel_filename

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPO = Path(__file__).resolve().parent.parent

# The bundle caps Python below 3.14 on Intel Macs (pyproject [tool.uv]).
INTEL_MAC_PYTHONS = [(3, 10), (3, 11), (3, 12), (3, 13)]

# Oldest macOS the current GitHub Intel runner and supported Intel Macs run.
_PLATFORMS = list(mac_platforms(version=(13, 0), arch="x86_64"))


def _env(py: tuple[int, int]) -> dict[str, str]:
    version = f"{py[0]}.{py[1]}"
    return {
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "python_version": version,
        "python_full_version": f"{version}.0",
        "extra": "",
    }


def _supported_tags(py: tuple[int, int]) -> set:
    return set(cpython_tags(python_version=py, platforms=_PLATFORMS)) | set(
        compatible_tags(python_version=py, platforms=_PLATFORMS)
    )


def _locked_wheels() -> dict[tuple[str, str], list[str]]:
    with (REPO / "uv.lock").open("rb") as fh:
        lock = tomllib.load(fh)
    out = {}
    for pkg in lock["package"]:
        files = [w["url"].rsplit("/", 1)[-1] for w in pkg.get("wheels", [])]
        out[(canonicalize_name(pkg["name"]), pkg["version"])] = files
    return out


def _runtime_pins() -> list[Requirement]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    out = subprocess.run(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
        ],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    reqs = []
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")):
            reqs.append(Requirement(line))
    return reqs


@pytest.mark.parametrize("py", INTEL_MAC_PYTHONS, ids=lambda p: f"py{p[0]}{p[1]}")
def test_every_runtime_pin_has_an_intel_mac_wheel(py):
    env = _env(py)
    tags = _supported_tags(py)
    wheels = _locked_wheels()
    missing = []
    for req in _runtime_pins():
        if req.marker is not None and not Marker(str(req.marker)).evaluate(env):
            continue
        (spec,) = req.specifier
        files = wheels[(canonicalize_name(req.name), spec.version)]
        if not any(tags & set(parse_wheel_filename(f)[3]) for f in files):
            missing.append(f"{req.name}=={spec.version}")
    assert not missing, (
        f"no Intel-macOS wheel for Python {py[0]}.{py[1]}: {missing}. The "
        "bundle would compile these from source on an Intel Mac; cap them "
        "for that platform in pyproject.toml (see the cryptography entry)."
    )
