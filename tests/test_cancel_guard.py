"""A cancelled tool call that finishes late must not kill the server (#82)."""

import json
import os
import subprocess
import sys
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock

import anyio
from mcp.shared.session import RequestResponder
from mcp.types import ErrorData, JSONRPCRequest

from pdf_mcp import cancel_guard


def _responder():
    session = MagicMock()
    session._send_response = AsyncMock()
    request = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/call")
    responder = RequestResponder(
        request_id=1,
        request_meta=None,
        request=request,
        session=session,
        on_complete=lambda r: None,
    )
    return responder, session


def test_responder_still_tracks_completion():
    """The guard reads this private flag; an SDK rename must fail here, not
    silently leave the crash in place."""

    async def run():
        responder, _ = _responder()
        assert responder._completed is False

    anyio.run(run)


def test_late_respond_after_cancel_is_dropped():
    cancel_guard.install()

    async def run():
        responder, session = _responder()
        with responder:
            await responder.cancel()
            await responder.respond(ErrorData(code=0, message="late result"))
        return session

    session = anyio.run(run)
    sent = [c.kwargs["response"] for c in session._send_response.await_args_list]
    assert [r.message for r in sent] == ["Request cancelled"]


def test_respond_without_cancel_is_sent():
    cancel_guard.install()

    async def run():
        responder, session = _responder()
        with responder:
            await responder.respond(ErrorData(code=0, message="result"))
        return session

    session = anyio.run(run)
    session._send_response.assert_awaited_once()


def test_install_is_idempotent():
    cancel_guard.install()
    patched = RequestResponder.respond
    cancel_guard.install()
    assert RequestResponder.respond is patched


_SLOW_SERVER = textwrap.dedent("""
    import time
    from fastmcp import FastMCP
    import pdf_mcp.server  # noqa: F401  installs the guard

    app = FastMCP("slow")

    @app.tool
    def slow(seconds: float) -> str:
        time.sleep(seconds)
        return "done"

    app.run(transport="stdio", show_banner=False)
""")


def test_server_survives_a_cancelled_slow_tool(tmp_path):
    """End to end over stdio: cancel a sync tool mid-run, let it finish, and
    the next request still gets an answer. Without the guard the process
    exits with AssertionError: Request already responded to."""
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
        cancelled = recv()
        assert cancelled["id"] == 2 and "error" in cancelled
        time.sleep(2.0)  # the tool finishes; its late result is dropped
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        listed = recv()
        assert listed is not None and listed["id"] == 3, proc.stderr.read()
        assert proc.poll() is None
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()
