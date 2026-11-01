"""A cancelled tool call that finishes late must not kill the server (#82)."""

import json
import os
import subprocess
import sys
import textwrap
import time

_SLOW_SERVER = textwrap.dedent("""
    import time
    from fastmcp import FastMCP
    import pdf_mcp.server  # noqa: F401

    app = FastMCP("slow")

    @app.tool
    def slow(seconds: float) -> str:
        time.sleep(seconds)
        return "done"

    app.run(transport="stdio", show_banner=False)
""")


def test_server_survives_a_cancelled_slow_tool(tmp_path):
    """End to end over stdio: cancel a sync tool mid-run, let it finish, and
    the next request still gets an answer. On SDK v1 the late result raised
    AssertionError: Request already responded to, and the process exited; SDK
    v2 answers neither the cancel nor the late result."""
    script = tmp_path / "slow_server.py"
    script.write_text(_SLOW_SERVER, encoding="utf-8")
    env = dict(os.environ, PDF_MCP_CACHE_DIR=str(tmp_path / "cache"))
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        return json.loads(line) if line else None

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        assert recv()["id"] == 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "slow", "arguments": {"seconds": 1.5}},
            }
        )
        time.sleep(0.5)
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 2, "reason": "timeout"},
            }
        )
        time.sleep(2.0)  # the tool finishes; its late result is dropped
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        listed = recv()  # the first message since the cancel: nothing for id 2
        assert listed is not None and listed["id"] == 3, proc.stderr.read()
        assert proc.poll() is None
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()
