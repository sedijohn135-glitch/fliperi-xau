"""Exception hierarchy for the MCP layer.

The engine reacts differently to each of these, so they are distinct types
rather than one generic error:

* :class:`AuthFailure`   -> alert the operator, pause trading, back off slowly.
* :class:`ToolUnavailable` -> a capability we depend on is missing; fail *safe*
  (block trading) rather than guessing at an equivalent.
* :class:`ToolCallError` -> the server rejected one call; retryable.
* :class:`NotConnected`  -> transport is down; the supervisor is already on it.
"""

from __future__ import annotations


class MCPError(RuntimeError):
    """Base class for every MCP-layer failure."""


class NotConnected(MCPError):
    """No live session is available right now."""


class AuthFailure(MCPError):
    """401/403 style rejection - the Bearer token is bad, expired or revoked."""


class ToolUnavailable(MCPError):
    """The upstream server does not expose a tool the bot requires."""


class ToolCallError(MCPError):
    """The server executed the call and returned an error result."""


class SchemaBindError(MCPError):
    """A tool's live schema could not be satisfied from the values we have.

    Raised instead of sending a half-built payload: an order with a silently
    dropped stop-loss field is far worse than an order that never gets sent.
    """
