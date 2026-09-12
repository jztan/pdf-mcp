"""Launch an unpacked pdf-mcp bundle the way Claude Desktop does (from its
manifest's mcp_config) and check the MCP handshake.

    python scripts/smoke_mcpb.py path/to/unpacked --timeout 900
    python scripts/smoke_mcpb.py path/to/unpacked --expect-fallback \
        --extra-env PDF_MCP_UV_DOWNLOAD_BASE=https://127.0.0.1:9/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
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


_STARTING = "STARTING:"
_UNAVAILABLE = "UNAVAILABLE:"


async def _run(params: StdioServerParameters, expect_fallback: bool):
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
    return init_seconds, time.monotonic() - started, tools, call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--expect-fallback", action="store_true")
    parser.add_argument("--extra-env", action="append", default=[])
    args = parser.parse_args(argv)
    extra = dict(item.split("=", 1) for item in args.extra_env)
    manifest = json.loads((args.bundle_dir / "manifest.json").read_text("utf-8"))
    expected = sorted(t["name"] for t in manifest["tools"])
    init_seconds, seconds, tools, call = asyncio.run(
        asyncio.wait_for(
            _run(launch_params(args.bundle_dir, extra), args.expect_fallback),
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
