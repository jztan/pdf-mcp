"""Launch an unpacked pdf-mcp bundle the way Claude Desktop does and prove
it answers an MCP handshake with every tool its manifest declares.

    python scripts/smoke_mcpb.py path/to/unpacked --timeout 900
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


async def _handshake(bundle_dir: Path) -> tuple[float, list[str]]:
    params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", str(bundle_dir), "src/server.py"],
        # CI must not phone PyPI; the check itself is unit-tested.
        env={**os.environ, "PDF_MCP_UPDATE_CHECK": "0"},
    )
    started = time.monotonic()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
    return time.monotonic() - started, sorted(t.name for t in tools.tools)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)
    manifest = json.loads((args.bundle_dir / "manifest.json").read_text("utf-8"))
    expected = sorted(t["name"] for t in manifest["tools"])
    seconds, names = asyncio.run(
        asyncio.wait_for(_handshake(args.bundle_dir), args.timeout)
    )
    print(f"startup_seconds={seconds:.1f} tools={len(names)}")
    if names != expected:
        print(f"tool mismatch: expected {expected}, got {names}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
