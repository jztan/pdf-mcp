"""Bundle-only daily update check (pdf_mcp.updates)."""

import json

import pytest

from pdf_mcp import updates

DAY = updates.CHECK_INTERVAL_SECONDS


def _files(yanked=False):
    return [{"filename": "x.whl", "yanked": yanked}]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_truthy_enables(value):
    assert updates.check_enabled(None, {updates.ENV_VAR: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_env_falsy_disables(value):
    assert updates.check_enabled(None, {updates.ENV_VAR: value}) is False


def test_unset_env_disables():
    """pip/uvx installs: no env, no config key, no check."""
    assert updates.check_enabled(None, {}) is False


def test_config_false_beats_bundle_env():
    assert updates.check_enabled(False, {updates.ENV_VAR: "1"}) is False


def test_config_true_enables_without_env():
    assert updates.check_enabled(True, {}) is True


def test_latest_stable_skips_prerelease_and_yanked():
    releases = {
        "3.1.0": _files(),
        "3.2.0": _files(yanked=True),
        "3.3.0rc1": _files(),
        "3.1.5": _files(),
        "3.0.0": [],
        "not-a-version": _files(),
    }
    assert updates.latest_stable(releases) == "3.1.5"


def test_latest_stable_none_when_nothing_usable():
    assert updates.latest_stable({"1.0.0rc1": _files()}) is None


def test_fresh_cache_makes_no_request(tmp_path):
    (tmp_path / updates.CACHE_FILENAME).write_text(
        json.dumps({"latest": "3.1.0", "checked_at": 1000.0})
    )

    def boom():
        raise AssertionError("must not fetch")

    updates.refresh_if_stale(tmp_path, now=1000.0 + DAY - 1, fetch=boom)


def test_stale_cache_fetches_once_and_writes(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return "3.2.0"

    updates.refresh_if_stale(tmp_path, now=5000.0, fetch=fetch)
    assert calls == [1]
    assert updates.read_cached(tmp_path) == {"latest": "3.2.0", "checked_at": 5000.0}


def test_failed_fetch_keeps_old_latest_and_waits_a_day(tmp_path, capsys):
    (tmp_path / updates.CACHE_FILENAME).write_text(
        json.dumps({"latest": "3.1.0", "checked_at": 0.0})
    )

    def offline():
        raise OSError("offline")

    updates.refresh_if_stale(tmp_path, now=DAY + 1, fetch=offline)
    assert updates.read_cached(tmp_path) == {"latest": "3.1.0", "checked_at": DAY + 1}
    assert capsys.readouterr().out == ""  # STDIO transport: stdout is protocol


def test_corrupt_cache_is_treated_as_missing(tmp_path):
    (tmp_path / updates.CACHE_FILENAME).write_text("{not json")
    assert updates.read_cached(tmp_path) is None


def test_status_none_when_disabled(tmp_path):
    assert updates.update_status("3.1.0", tmp_path, enabled=False) is None


def test_status_reports_available_update(tmp_path):
    (tmp_path / updates.CACHE_FILENAME).write_text(
        json.dumps({"latest": "3.2.0", "checked_at": 0.0})
    )
    assert updates.update_status("3.1.0", tmp_path, enabled=True) == {
        "current": "3.1.0",
        "latest": "3.2.0",
        "update_available": True,
        "checked_at": "1970-01-01T00:00:00+00:00",
        "download_url": updates.DOWNLOAD_URL,
    }


def test_status_before_first_check(tmp_path):
    status = updates.update_status("3.1.0", tmp_path, enabled=True)
    assert status["latest"] is None and status["update_available"] is False
    assert status["checked_at"] is None


def test_notice_text_only_when_newer():
    base = {"current": "3.1.0", "download_url": updates.DOWNLOAD_URL}
    assert updates.notice_text(None) == ""
    assert (
        updates.notice_text({**base, "latest": "3.1.0", "update_available": False})
        == ""
    )
    text = updates.notice_text({**base, "latest": "3.2.0", "update_available": True})
    assert "3.2.0" in text and "3.1.0" in text and updates.DOWNLOAD_URL in text
    assert "double-click" in text
