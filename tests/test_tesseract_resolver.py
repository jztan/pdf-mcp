"""find_tesseract(): PATH first, then the standard GUI install folders."""

import pytest

from pdf_mcp import extractor


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(extractor, "_TESSERACT_EXE", None)


def _make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return str(path)


def test_path_hit_wins(monkeypatch, tmp_path):
    on_path = _make_exe(tmp_path / "bin" / "tesseract")
    _make_exe(tmp_path / "pf" / "Tesseract-OCR" / "tesseract.exe")
    monkeypatch.setattr(extractor.shutil, "which", lambda name: on_path)
    monkeypatch.setattr(extractor.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    assert extractor.find_tesseract() == on_path


@pytest.mark.parametrize("var", ["ProgramFiles", "ProgramFiles(x86)"])
def test_windows_install_folders(monkeypatch, tmp_path, var):
    for name in ("ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(var, str(tmp_path))
    exe = _make_exe(tmp_path / "Tesseract-OCR" / "tesseract.exe")
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    monkeypatch.setattr(extractor.sys, "platform", "win32")
    assert extractor.find_tesseract() == exe


def test_macos_homebrew_folders(monkeypatch, tmp_path):
    exe = _make_exe(tmp_path / "brew" / "tesseract")
    monkeypatch.setattr(extractor, "_MACOS_TESSERACT_DIRS", (str(tmp_path / "brew"),))
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    monkeypatch.setattr(extractor.sys, "platform", "darwin")
    assert extractor.find_tesseract() == exe


def test_windows_folders_ignored_on_linux(monkeypatch, tmp_path):
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    _make_exe(tmp_path / "Tesseract-OCR" / "tesseract.exe")
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    monkeypatch.setattr(extractor.sys, "platform", "linux")
    assert extractor.find_tesseract() is None


def test_miss_is_not_cached(monkeypatch, tmp_path):
    """Installing Tesseract mid-session must work without a restart."""
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    monkeypatch.setattr(extractor.sys, "platform", "win32")
    assert extractor.find_tesseract() is None
    exe = _make_exe(tmp_path / "Tesseract-OCR" / "tesseract.exe")
    assert extractor.find_tesseract() == exe


def test_check_tesseract_available_runs_resolved_binary(monkeypatch, tmp_path):
    exe = _make_exe(tmp_path / "Tesseract-OCR" / "tesseract.exe")
    monkeypatch.setattr(extractor, "find_tesseract", lambda: exe)
    monkeypatch.setattr(extractor, "_TESSDATA_PATH", "cached")
    calls = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: calls.append(cmd))
    extractor.check_tesseract_available()
    assert calls == [[exe, "--version"]]


def test_check_tesseract_available_raises_when_resolver_misses(monkeypatch):
    monkeypatch.setattr(extractor, "find_tesseract", lambda: None)
    with pytest.raises(RuntimeError, match="Tesseract not found"):
        extractor.check_tesseract_available()


def test_resolve_tessdata_uses_resolved_binary(monkeypatch, tmp_path):
    exe = _make_exe(tmp_path / "Tesseract-OCR" / "tesseract.exe")
    tessdata = tmp_path / "Tesseract-OCR" / "tessdata"
    tessdata.mkdir()
    (tessdata / "eng.traineddata").write_bytes(b"")
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    monkeypatch.setattr(extractor, "find_tesseract", lambda: exe)
    seen = []

    class Result:
        stdout = f'List of available languages in "{tessdata}" (1):\neng\n'
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append(cmd[0])
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    assert extractor._resolve_tessdata() == str(tessdata)
    assert seen == [exe]


@pytest.mark.parametrize(
    "platform, marker",
    [
        ("win32", "Windows installer"),
        ("darwin", "brew install tesseract"),
        ("linux", "apt install tesseract-ocr"),
    ],
)
def test_missing_message_names_one_os(platform, marker):
    msg = extractor.missing_tesseract_message(platform)
    assert msg.startswith("Tesseract not found")
    assert marker in msg and extractor.OCR_SETUP_URL in msg
    others = {"win32": "Windows installer", "darwin": "brew", "linux": "apt "}
    assert not any(m in msg for p, m in others.items() if p != platform)


def test_install_hint_uses_exact_winget_id():
    hint = extractor.tesseract_install_hint()
    assert "winget install -e --id UB-Mannheim.TesseractOCR" in hint
    assert "winget install Tesseract-OCR" not in hint


def test_check_tesseract_available_uses_os_message(monkeypatch):
    monkeypatch.setattr(extractor, "find_tesseract", lambda: None)
    monkeypatch.setattr(extractor.sys, "platform", "win32")
    with pytest.raises(RuntimeError) as info:
        extractor.check_tesseract_available()
    assert str(info.value) == extractor.missing_tesseract_message("win32")


def test_resolve_tessdata_skips_a_reported_dir_without_traineddata(
    monkeypatch, tmp_path
):
    """A portable macOS/Linux Tesseract has no compiled-in tessdata path and
    reports "./" (measured on the pdf-mcp-tesseract macOS builds). That is a
    directory but not tessdata: fall back to the folder beside the binary."""
    exe = _make_exe(tmp_path / "portable" / "tesseract")
    tessdata = tmp_path / "portable" / "tessdata"
    tessdata.mkdir()
    (tessdata / "eng.traineddata").write_bytes(b"")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    monkeypatch.setattr(extractor, "find_tesseract", lambda: exe)

    class Result:
        stdout = 'List of available languages in "./" (0):\n'
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **kwargs: Result())
    assert extractor._resolve_tessdata() == str(tessdata)


def test_pytesseract_path_resolves_tessdata_when_none_given(monkeypatch):
    """ocr_page_text(tessdata=None) on the pytesseract path must pass the
    resolved folder, as the tesserocr path does, instead of leaving a
    portable Tesseract to look in "./"."""
    from pdf_mcp.backend import raster

    monkeypatch.setenv("PDF_MCP_OCR", "pytesseract")
    monkeypatch.setattr(raster, "_text_layer", lambda *a: "")
    monkeypatch.setattr(raster, "_scan_native_dpi", lambda *a: None)
    monkeypatch.setattr(raster, "render_page", lambda *a, **k: object())
    monkeypatch.setattr(extractor, "_resolve_tessdata", lambda: "/portable/tessdata")
    seen = {}

    import pytesseract

    def fake_image_to_string(image, lang, config):
        seen["config"] = config
        return "text"

    monkeypatch.setattr(pytesseract, "image_to_string", fake_image_to_string)
    assert raster.ocr_page_text("x.pdf", 0) == "text"
    assert seen["config"] == "--tessdata-dir /portable/tessdata"
