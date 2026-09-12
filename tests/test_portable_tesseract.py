"""Zero-install OCR: the pinned, portable Tesseract fetched on first use."""

import hashlib
import http.server
import io
import threading
import time
import zipfile
from pathlib import Path

import pytest

from pdf_mcp import extractor, portable_tesseract as pt


def _zip_bytes(system: str = "darwin", evil: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(pt.exe_name(system), "#!/bin/sh\necho tesseract 5.5.2\n")
        zf.writestr("tessdata/eng.traineddata", "eng")
        zf.writestr("THIRD-PARTY-NOTICES.md", "notices")
        if evil:
            zf.writestr(evil, "boom")
    return buf.getvalue()


@pytest.fixture
def server():
    """A local HTTP server; set .routes[path] = bytes or ("redirect", url)."""
    routes: dict[str, object] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib name
            route = routes.get(self.path)
            if route is None:
                self.send_response(404)
                self.end_headers()
                return
            if isinstance(route, tuple):
                self.send_response(302)
                self.send_header("Location", route[1])
                self.end_headers()
                return
            if isinstance(route, float):  # a slow response
                time.sleep(route)
                route = b"late"
            self.send_response(200)
            self.send_header("Content-Length", str(len(route)))
            self.end_headers()
            self.wfile.write(route)

        def log_message(self, *args):  # keep test output quiet
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.routes = routes
    httpd.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()


def _pins(data: bytes, key: str = "darwin-arm64", tag: str = "t1") -> dict:
    return {
        "tag": tag,
        "assets": {
            key: {
                "file": f"pdf-mcp-tesseract-{key}.zip",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        },
    }


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "_cache_dir", None)
    monkeypatch.setattr(pt, "_thread", None)
    monkeypatch.setattr(pt, "_failed", None)
    monkeypatch.setattr(pt, "_last_error", None)
    monkeypatch.setattr(extractor, "_TESSERACT_EXE", None)


# -- platform and switch --------------------------------------------------


@pytest.mark.parametrize(
    "system, machine, key",
    [
        ("win32", "AMD64", "win32-x64"),
        ("darwin", "arm64", "darwin-arm64"),
        ("darwin", "x86_64", "darwin-x64"),
        ("linux", "x86_64", None),
        ("win32", "ARM64", None),
    ],
)
def test_platform_key(system, machine, key):
    assert pt.platform_key(system, machine) == key


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_bundle_env_enables(value):
    assert pt.enabled(None, {pt.ENV_VAR: value}) is True


def test_off_by_default_for_pip_installs():
    assert pt.enabled(None, {}) is False


def test_config_wins_over_env():
    assert pt.enabled(False, {pt.ENV_VAR: "1"}) is False
    assert pt.enabled(True, {}) is True


def test_shipped_pins_cover_the_released_platforms():
    pins = pt.load_pins()
    assert pins["tag"].startswith("tesseract-")
    assert set(pins["assets"]) == {"win32-x64", "darwin-arm64", "darwin-x64"}
    for asset in pins["assets"].values():
        assert len(asset["sha256"]) == 64 and asset["size"] > 1_000_000


# -- download, verify, install --------------------------------------------


def test_install_downloads_verifies_and_unpacks(server, tmp_path):
    data = _zip_bytes()
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = data
    pins = _pins(data)
    exe = pt.install(tmp_path, pins, "darwin-arm64", base=server.base, system="darwin")
    assert Path(exe) == tmp_path / "tesseract" / "t1" / "tesseract"
    assert (tmp_path / "tesseract" / "t1" / "tessdata" / "eng.traineddata").exists()
    assert pt.installed_binary(tmp_path, pins, "darwin") == exe


def test_install_follows_redirects(server, tmp_path):
    data = _zip_bytes()
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = ("redirect", "/cdn/x")
    server.routes["/cdn/x"] = data
    exe = pt.install(
        tmp_path, _pins(data), "darwin-arm64", base=server.base, system="darwin"
    )
    assert Path(exe).exists()


def test_hash_mismatch_installs_nothing(server, tmp_path):
    data = _zip_bytes()
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = data + b"tampered"
    pins = _pins(data)
    pins["assets"]["darwin-arm64"]["size"] += 100  # let the size cap pass
    with pytest.raises(pt.IntegrityError):
        pt.install(tmp_path, pins, "darwin-arm64", base=server.base, system="darwin")
    assert pt.installed_binary(tmp_path, pins, "darwin") is None
    assert not list((tmp_path / "tesseract").glob("*/tesseract"))


def test_size_cap_stops_an_oversized_download(server, tmp_path):
    data = _zip_bytes()
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = data * 5
    with pytest.raises(pt.IntegrityError, match="larger"):
        pt.install(
            tmp_path, _pins(data), "darwin-arm64", base=server.base, system="darwin"
        )


@pytest.mark.parametrize("evil", ["../outside.txt", "/abs.txt", "tessdata/../../x"])
def test_unsafe_zip_paths_are_rejected(server, tmp_path, evil):
    data = _zip_bytes(evil=evil)
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = data
    with pytest.raises(pt.IntegrityError, match="unsafe"):
        pt.install(
            tmp_path, _pins(data), "darwin-arm64", base=server.base, system="darwin"
        )
    assert not (tmp_path / "outside.txt").exists()


def test_a_concurrent_install_keeps_the_winner(server, tmp_path):
    """Claude Desktop starts two or three servers at once; the loser of the
    rename race uses the winner's copy."""
    data = _zip_bytes()
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = data
    pins = _pins(data)
    first = pt.install(
        tmp_path, pins, "darwin-arm64", base=server.base, system="darwin"
    )
    second = pt.install(
        tmp_path, pins, "darwin-arm64", base=server.base, system="darwin"
    )
    assert first == second


# -- ensure(): the budget the OCR call waits -------------------------------


def test_ensure_ready_when_already_installed(server, tmp_path):
    data = _zip_bytes()
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = data
    pins = _pins(data)
    pt.install(tmp_path, pins, "darwin-arm64", base=server.base, system="darwin")
    state, exe = pt.ensure(
        cache_dir=tmp_path, pins=pins, key="darwin-arm64", system="darwin"
    )
    assert state == "ready" and exe


def test_ensure_downloads_within_the_budget(server, tmp_path):
    data = _zip_bytes()
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = data
    state, exe = pt.ensure(
        wait=10,
        cache_dir=tmp_path,
        pins=_pins(data),
        key="darwin-arm64",
        base=server.base,
        system="darwin",
    )
    assert state == "ready" and Path(exe).exists()


def test_ensure_reports_downloading_when_slower_than_the_budget(server, tmp_path):
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = 2.0  # slow server
    state, exe = pt.ensure(
        wait=0.2,
        cache_dir=tmp_path,
        pins=_pins(b"x" * 10),
        key="darwin-arm64",
        base=server.base,
        system="darwin",
    )
    assert state == "downloading" and exe is None


def test_ensure_unavailable_without_a_pin_for_this_platform(tmp_path):
    state, reason = pt.ensure(cache_dir=tmp_path, pins=_pins(b"x"), key=None)
    assert state == "unavailable" and "this computer" in reason


def test_ensure_does_not_retry_after_a_hash_mismatch(server, tmp_path):
    data = _zip_bytes()
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = data + b"x"
    pins = _pins(data)
    pins["assets"]["darwin-arm64"]["size"] += 10
    kw = dict(
        cache_dir=tmp_path,
        pins=pins,
        key="darwin-arm64",
        base=server.base,
        system="darwin",
    )
    assert pt.ensure(wait=10, **kw)[0] == "unavailable"
    server.routes["/t1/pdf-mcp-tesseract-darwin-arm64.zip"] = data  # fixed upstream
    assert pt.ensure(wait=10, **kw)[0] == "unavailable"  # still refused until restart


# -- lookup order ------------------------------------------------------------


def test_find_tesseract_prefers_a_system_install(monkeypatch, tmp_path):
    system_exe = tmp_path / "bin" / "tesseract"
    system_exe.parent.mkdir()
    system_exe.write_text("")
    monkeypatch.setattr(extractor.shutil, "which", lambda name: str(system_exe))
    monkeypatch.setattr(pt, "installed_binary", lambda *a, **k: "/portable/tesseract")
    assert extractor.find_tesseract() == str(system_exe)


def test_find_tesseract_falls_back_to_the_portable_copy(monkeypatch, tmp_path):
    portable = tmp_path / "tesseract"
    portable.write_text("")
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    monkeypatch.setattr(extractor, "_tesseract_candidates", lambda *a: [])
    monkeypatch.setattr(pt, "installed_binary", lambda *a, **k: str(portable))
    assert extractor.find_tesseract() == str(portable)


# -- the OCR gate in pdf_read_pages -------------------------------------------


@pytest.fixture
def no_tesseract(monkeypatch):
    from pdf_mcp import server

    def missing():
        raise RuntimeError("Tesseract is not installed")

    monkeypatch.setattr(server, "check_tesseract_available", missing)
    return server


def test_gate_keeps_the_install_message_when_auto_install_is_off(
    no_tesseract, monkeypatch
):
    server = no_tesseract
    monkeypatch.setattr(server, "_OCR_AUTO_INSTALL", False)
    monkeypatch.setattr(pt, "ensure", lambda *a, **k: pytest.fail("downloaded"))
    result = server._ocr_unavailable("eng")
    assert "not installed" in result["error"] and "install_hint" in result


def test_gate_says_setting_up_while_the_download_runs(no_tesseract, monkeypatch):
    server = no_tesseract
    monkeypatch.setattr(server, "_OCR_AUTO_INSTALL", True)
    monkeypatch.setattr(pt, "ensure", lambda *a, **k: ("downloading", None))
    result = server._ocr_unavailable("eng")
    assert result["error"].startswith("Setting up OCR")
    assert "pdf_render_pages" in result["hint"]


def test_gate_falls_back_to_the_install_message_when_unavailable(
    no_tesseract, monkeypatch
):
    server = no_tesseract
    monkeypatch.setattr(server, "_OCR_AUTO_INSTALL", True)
    monkeypatch.setattr(pt, "ensure", lambda *a, **k: ("unavailable", "offline"))
    assert "install_hint" in server._ocr_unavailable("eng")


def test_gate_passes_once_the_portable_copy_is_ready(monkeypatch):
    from pdf_mcp import server

    calls = []

    def check():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Tesseract is not installed")

    monkeypatch.setattr(server, "check_tesseract_available", check)
    monkeypatch.setattr(server, "_OCR_AUTO_INSTALL", True)
    monkeypatch.setattr(pt, "ensure", lambda *a, **k: ("ready", "/c/tesseract"))
    monkeypatch.setattr(server, "_lang_available", lambda lang: True)
    assert server._ocr_unavailable("eng") is None
    assert len(calls) == 2


def test_portable_copy_reads_english_only(monkeypatch, tmp_path):
    from pdf_mcp import server

    (tmp_path / "eng.traineddata").write_text("")
    monkeypatch.setattr(server, "find_tesseract", lambda: "/c/tesseract")
    monkeypatch.setattr(pt, "installed_binary", lambda: "/c/tesseract")
    monkeypatch.setattr(extractor, "_TESSDATA_PATH", str(tmp_path))
    assert server._lang_available("eng") is True
    assert server._lang_available("eng+jpn") is False
    monkeypatch.setattr(server, "check_tesseract_available", lambda: None)
    assert "English only" in server._ocr_unavailable("jpn")["error"]


def test_a_system_tesseract_is_trusted_with_any_language(monkeypatch):
    from pdf_mcp import server

    monkeypatch.setattr(server, "find_tesseract", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(pt, "installed_binary", lambda: "/c/tesseract")
    assert server._lang_available("jpn") is True
