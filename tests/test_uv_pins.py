"""The bundle's uv pins cover every supported target and match upstream."""

import json
import os
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import update_uv_pins  # noqa: E402

PINS = json.loads(update_uv_pins.PINS_PATH.read_text(encoding="utf-8"))


def test_pins_cover_all_six_targets():
    assert set(PINS["assets"]) == set(update_uv_pins.TARGETS)
    for key, asset in PINS["assets"].items():
        assert asset["file"] == update_uv_pins.TARGETS[key]
        assert len(asset["sha256"]) == 64


def test_build_pins_uses_first_token_of_sha_file():
    fake = {f: f"{'a' * 64}  {f}\n" for f in update_uv_pins.TARGETS.values()}
    pins = update_uv_pins.build_pins(
        "9.9.9", fetch=lambda url: fake[url.rsplit("/", 1)[1][: -len(".sha256")]]
    )
    assert pins["version"] == "9.9.9"
    assert pins["assets"]["win32-x64"]["sha256"] == "a" * 64


@pytest.mark.skipif(os.environ.get("CI_OFFLINE") == "1", reason="offline")
def test_pins_match_published_checksums():
    try:
        live = update_uv_pins.build_pins(PINS["version"])
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"network unavailable: {exc}")
    assert live == PINS
