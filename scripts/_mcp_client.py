#!/usr/bin/env python
"""scripts/_mcp_client.py

Minimal stdio MCP client: newline-delimited JSON-RPC over a subprocess.
Product-neutral — the launch command is supplied by the caller. Shared by the
benchmark scripts that need to drive an external MCP server end-to-end.
"""

from __future__ import annotations

import json
import select
import subprocess
import time
from typing import Any


class MCPClient:
    """Minimal MCP stdio client: newline-delimited JSON-RPC over a subprocess."""

    def __init__(self, cmd: list[str]) -> None:
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._id = 0

    def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _read_response(self, want_id: int, timeout: float = 60.0) -> dict[str, Any]:
        """Read lines until the JSON-RPC response with id==want_id arrives."""
        assert self.proc.stdout is not None
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(f"no response for id={want_id}")
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready:
                raise TimeoutError(f"no response for id={want_id}")
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip banners / non-JSON stdout noise
            if msg.get("id") == want_id and ("result" in msg or "error" in msg):
                return msg

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        resp = self._read_response(rid)
        if "error" in resp:
            raise RuntimeError(f"{method} error: {resp['error']}")
        return resp["result"]

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pdf-mcp-bench", "version": "0"},
            },
        )
        self.notify("notifications/initialized")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
