"""Launch an unpacked pdf-mcp bundle the way Claude Desktop does (from its
manifest's mcp_config) and check the MCP handshake.

    python scripts/smoke_mcpb.py path/to/unpacked --timeout 900
    python scripts/smoke_mcpb.py path/to/unpacked --expect-fallback \
        --extra-env PDF_MCP_UV_DOWNLOAD_BASE=https://127.0.0.1:9/
    python scripts/smoke_mcpb.py path/to/unpacked --ocr   # no system Tesseract
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def launch_params(bundle_dir: Path, extra_env: dict[str, str]) -> StdioServerParameters:
    manifest = json.loads((bundle_dir / "manifest.json").read_text("utf-8"))
    cfg = manifest["server"]["mcp_config"]
    dirname = str(bundle_dir.resolve())
    args = [a.replace("${__dirname}", dirname) for a in cfg["args"]]
    env = {**os.environ}
    for key, value in cfg.get("env", {}).items():
        # CI must not phone PyPI; the check itself is unit-tested.
        env[key] = value.replace("${user_config.update_check}", "false")
    env.update(extra_env)
    return StdioServerParameters(command=cfg["command"], args=args, env=env)


_OCR_WORDS = "The quick brown fox reads a scanned page"
_OCR_SETUP_SECONDS = 300.0


def scanned_pdf(dest: Path) -> Path:
    """A one-page PDF holding only a picture of text, so OCR must run."""
    import pymupdf

    src = pymupdf.open()
    src.new_page().insert_text((72, 144), _OCR_WORDS, fontsize=24)
    pix = src[0].get_pixmap(dpi=300)
    out = pymupdf.open()
    page = out.new_page(width=src[0].rect.width, height=src[0].rect.height)
    page.insert_image(page.rect, pixmap=pix)
    out.save(dest)
    return dest


async def _ocr(session: ClientSession, pdf: Path) -> tuple[float, dict, dict]:
    """First OCR call on a machine with no Tesseract: retry through the
    "setting up" reply until the portable copy is in place."""
    start = time.monotonic()
    while True:
        result = await session.call_tool(
            "pdf_read_pages", {"path": str(pdf), "pages": "1", "ocr": True}
        )
        body = json.loads(result.content[0].text)
        if not str(body.get("error", "")).startswith("Setting up OCR"):
            break
        if time.monotonic() - start > _OCR_SETUP_SECONDS:
            raise TimeoutError("OCR was still being set up")
        await asyncio.sleep(5.0)
    info = await session.call_tool("server_info", {})
    ocr = json.loads(info.content[0].text)["features"]["extraction"]["ocr"]
    return time.monotonic() - start, body, ocr


_STARTING = "STARTING:"
_UNAVAILABLE = "UNAVAILABLE:"


async def _timed(session: ClientSession, name: str, args: dict):
    start = time.monotonic()
    result = await session.call_tool(name, args)
    return time.monotonic() - start, result


async def _run(
    params: StdioServerParameters,
    expect_fallback: bool,
    pdf: Path | None = None,
    ocr_pdf: Path | None = None,
):
    started = time.monotonic()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            init_seconds = time.monotonic() - started
            # Early init answers at once with placeholder tools, so a listing
            # alone proves nothing: poll until the real server has taken over
            # (or the launcher gave up and switched to its fallback).
            while True:
                tools = (await session.list_tools()).tools
                if not any((t.description or "").startswith(_STARTING) for t in tools):
                    break
                await asyncio.sleep(1.0)
            name = tools[0].name if expect_fallback else "server_info"
            call = await session.call_tool(name, {})
            # First real calls after a fresh venv: pdf_info reads the file
            # only; pdf_search also loads the embedding model (downloaded on
            # first use). A 45 s first call was seen once on Windows.
            first: dict[str, tuple[float, object]] = {}
            if pdf is not None and not expect_fallback:
                path = str(pdf.resolve())
                first["pdf_info"] = await _timed(session, "pdf_info", {"path": path})
                first["search"] = await _timed(
                    session, "pdf_search", {"path": path, "query": "cloud security"}
                )
            ocr = await _ocr(session, ocr_pdf) if ocr_pdf is not None else None
    return init_seconds, time.monotonic() - started, tools, call, first, ocr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--expect-fallback", action="store_true")
    parser.add_argument("--extra-env", action="append", default=[])
    parser.add_argument("--pdf", type=Path, default=None)
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="OCR a scanned page; expects no system Tesseract on the machine",
    )
    args = parser.parse_args(argv)
    extra = dict(item.split("=", 1) for item in args.extra_env)
    manifest = json.loads((args.bundle_dir / "manifest.json").read_text("utf-8"))
    expected = sorted(t["name"] for t in manifest["tools"])
    ocr_pdf = None
    if args.ocr:
        ocr_pdf = scanned_pdf(Path(tempfile.mkdtemp()) / "scanned.pdf")
    init_seconds, seconds, tools, call, first, ocr = asyncio.run(
        asyncio.wait_for(
            _run(
                launch_params(args.bundle_dir, extra),
                args.expect_fallback,
                args.pdf,
                ocr_pdf,
            ),
            args.timeout,
        )
    )
    names = sorted(t.name for t in tools)
    # initialize_seconds is what Claude Desktop's 60 s limit applies to;
    # startup_seconds is until the real server answered a tool call.
    print(
        f"initialize_seconds={init_seconds:.1f} "
        f"startup_seconds={seconds:.1f} tools={len(names)}"
    )
    if names != expected:
        print(f"tool mismatch: expected {expected}, got {names}", file=sys.stderr)
        return 1
    descriptions = [t.description or "" for t in tools]
    if args.expect_fallback:
        ok = all(d.startswith(_UNAVAILABLE) for d in descriptions)
        if not ok or not call.isError:
            print("fallback responder did not report the failure", file=sys.stderr)
            return 1
        print(f"fallback_message={call.content[0].text}")
        return 0
    if any(d.startswith((_STARTING, _UNAVAILABLE)) for d in descriptions):
        print("real server never took over from the launcher", file=sys.stderr)
        return 1
    if call.isError:
        print(f"server_info failed: {call.content}", file=sys.stderr)
        return 1
    if first:
        # Measurement only, no threshold: CI reports these on every push.
        print(
            f"first_pdf_info_seconds={first['pdf_info'][0]:.1f} "
            f"first_search_seconds={first['search'][0]:.1f}"
        )
        for label, (_, result) in first.items():
            if getattr(result, "isError", False):
                print(f"{label} failed: {result.content}", file=sys.stderr)
                return 1
    if ocr is not None:
        ocr_seconds, body, feature = ocr
        text = " ".join(p.get("text", "") for p in body.get("pages", []))
        print(f"first_ocr_seconds={ocr_seconds:.1f} ocr_source={feature['source']}")
        if "quick brown fox" not in text.lower():
            print(f"OCR did not read the page: {body}", file=sys.stderr)
            return 1
        if feature["source"] != "portable":
            print("OCR did not use the downloaded Tesseract", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
