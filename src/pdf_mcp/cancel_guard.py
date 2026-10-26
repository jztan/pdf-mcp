"""Keep the server alive when a cancelled tool call finishes late (issue #82).

fastmcp runs a sync tool with ``anyio.to_thread.run_sync``, which shields
the thread: when the client sends ``notifications/cancelled``, the SDK
answers the cancel at once and marks the request completed, but the tool
keeps running. When it returns, nothing between the handler and
``RequestResponder.respond`` gives the cancel scope a checkpoint, so the
SDK tries to send the real result and trips ``assert not self._completed``.
That AssertionError escapes into the server's task group and the process
exits. Claude Desktop cancels any call still running after 4 minutes, so
one slow corpus warm took the whole server down, along with every call
queued behind it.

The upstream bug is open (modelcontextprotocol/python-sdk#2416). Until it
is fixed, ``install`` wraps ``respond`` to drop a response for a request
that is already completed. The client already has its answer (the cancel
error), so dropping the late one is the only correct move. Any other
request goes through the original method unchanged, so the wrapper goes
inert once upstream guards ``respond`` itself.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.shared.session import RequestResponder

logger = logging.getLogger(__name__)

_MARKER = "_pdf_mcp_cancel_guard"


def install() -> None:
    """Patch ``RequestResponder.respond`` once per process."""
    original = RequestResponder.respond
    if getattr(original, _MARKER, False):
        return

    async def respond(self: Any, response: Any) -> None:
        if getattr(self, "_completed", False):
            logger.info(
                "dropped the late result of request %s: the client "
                "cancelled it and has already been answered",
                self.request_id,
            )
            return
        await original(self, response)

    setattr(respond, _MARKER, True)
    respond.__doc__ = original.__doc__
    RequestResponder.respond = respond  # type: ignore[method-assign]
