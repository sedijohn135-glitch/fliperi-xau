"""Supervised Streamable-HTTP connection to the cTrader MCP server.

Responsibilities
----------------
* Speak **Streamable HTTP** (JSON-RPC 2.0 over POST, responses either JSON or
  ``text/event-stream``) - not SSE-only.
* Perform the real ``initialize`` handshake against the upstream and let the
  *server* mint the ``Mcp-Session-Id``; the transport then echoes that header on
  every subsequent request.  We never fabricate a local initialize response - a
  faked session id breaks continuity and every later call is rejected.
* Discover the tool set with ``tools/list`` at start-up.
* Survive anything: drops, timeouts and auth failures are handled by a
  supervisor loop with exponential backoff.  A 401 raises an emergency Telegram
  alert and parks the bot instead of crash-looping the Railway container.

The official SDK renamed its transport between 1.x (``streamablehttp_client``,
``timedelta`` timeouts, 3-tuple yield) and 2.x (``streamable_http_client``,
float timeouts, 2-tuple yield, headers via a supplied HTTP client), so a small
compatibility shim sits at the top of this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import random
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any, AsyncIterator, Callable, Optional

from mcp import ClientSession
from mcp.client import streamable_http as _sh

from config import Config
from mcp_client.errors import AuthFailure, NotConnected, ToolCallError
from mcp_client.schema import ToolCatalog, ToolSpec, describe_tool
from utils.logging import get_logger
from utils.telegram import TelegramNotifier, esc

log = get_logger("mcp.transport")

MCP_SESSION_HEADER = "mcp-session-id"

# --- SDK compatibility -----------------------------------------------------
_IS_V2 = hasattr(_sh, "streamable_http_client")
_CLIENT_FACTORY = getattr(_sh, "streamable_http_client", None) or getattr(
    _sh, "streamablehttp_client", None
)
if _CLIENT_FACTORY is None:  # pragma: no cover - guards a broken install
    raise ImportError(
        "The installed 'mcp' SDK exposes neither streamable_http_client (>=2.x) "
        "nor streamablehttp_client (1.x). Install a supported version."
    )

_READ_TIMEOUT_IS_TIMEDELTA = "timedelta" in str(
    inspect.signature(ClientSession.__init__).parameters["read_timeout_seconds"].annotation
)


def _timeout_arg(seconds: float) -> Any:
    """Timeouts are ``timedelta`` on SDK 1.x and plain floats on 2.x."""
    return timedelta(seconds=seconds) if _READ_TIMEOUT_IS_TIMEDELTA else float(seconds)


def flatten_exception(exc: BaseException) -> list[BaseException]:
    """Flatten ExceptionGroups (anyio task groups raise them) into a flat list."""
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for sub in exc.exceptions:
            out.extend(flatten_exception(sub))
        return out
    return [exc]


#: Exception type names that mean "the network failed", never "you are not
#: authorised".  A proxy that refuses CONNECT often surfaces as a 403, so type
#: information is checked before any message text.
_NETWORK_TYPES = (
    "connecterror", "connecttimeout", "connecttimeouterror", "proxyerror",
    "readtimeout", "writetimeout", "pooltimeout", "readerror", "writeerror",
    "remoteprotocolerror", "localprotocolerror", "networkerror", "closedresourceerror",
    "brokenresourceerror", "connectionreseterror", "connectionrefusederror", "oserror",
    "timeouterror", "sslerror", "certificateerror",
)

#: Message fragments that unambiguously indicate a rejected credential.
_AUTH_TEXT = (
    "unauthorized", "unauthenticated", "invalid token", "token expired",
    "invalid_token", "expired token", "invalid bearer", "authentication failed",
)


def describe_exception(exc: BaseException) -> str:
    """Readable one-liner for an exception tree.

    ``anyio`` task groups surface as ``ExceptionGroup``, whose ``str()`` is the
    famously unhelpful "unhandled errors in a TaskGroup (1 sub-exception)".  The
    operator needs the actual cause, so the leaves are unwrapped and named.
    """
    leaves = flatten_exception(exc)
    if len(leaves) == 1 and leaves[0] is exc:
        return f"{type(exc).__name__}: {exc}"
    parts = []
    for leaf in leaves:
        detail = str(leaf).strip() or "(no detail)"
        status = _status_of(leaf)
        suffix = f" [HTTP {status}]" if status else ""
        parts.append(f"{type(leaf).__name__}: {detail}{suffix}")
    return " | ".join(parts) or f"{type(exc).__name__}: {exc}"


def _status_of(exc: BaseException) -> Optional[int]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def classify_failure(exc: BaseException) -> str:
    """``"auth"``, ``"network"`` or ``"other"``.

    The distinction drives very different behaviour: an auth failure parks the
    bot for minutes and pages the operator, while a network blip is retried
    within seconds.  Misclassifying a dropped connection as an auth failure
    would idle the bot straight through a trading window.
    """
    leaves = flatten_exception(exc)

    # A genuine HTTP status is the strongest signal available.
    for leaf in leaves:
        if _status_of(leaf) in (401, 403):
            return "auth"

    if any(type(leaf).__name__.lower() in _NETWORK_TYPES for leaf in leaves):
        return "network"

    haystack = " ".join(str(leaf).lower() for leaf in leaves)
    if any(marker in haystack for marker in _AUTH_TEXT):
        return "auth"
    if "401" in haystack or "403" in haystack:
        return "auth"
    return "other"


def is_auth_error(exc: BaseException) -> bool:
    """True for 401/403-shaped failures anywhere in an exception tree."""
    return classify_failure(exc) == "auth"


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    AUTH_FAILED = "auth_failed"
    STOPPED = "stopped"


@dataclass
class ConnectionStats:
    connects: int = 0
    failures: int = 0
    auth_failures: int = 0
    calls: int = 0
    call_errors: int = 0
    timeouts: int = 0
    last_error: Optional[str] = None
    session_id: Optional[str] = None
    server_info: dict[str, Any] = field(default_factory=dict)


class MCPConnection:
    """A self-healing MCP client session.

    The transport context manager must be entered and exited by one and the same
    task, so a dedicated supervisor task owns the connection lifecycle while
    callers merely borrow the live ``ClientSession``.
    """

    def __init__(self, cfg: Config, notifier: TelegramNotifier) -> None:
        self._cfg = cfg
        self._notifier = notifier
        self._session: Optional[ClientSession] = None
        self._catalog: Optional[ToolCatalog] = None
        self._state = ConnectionState.DISCONNECTED
        self._ready = asyncio.Event()
        self._cycle = asyncio.Event()      # set -> tear the session down and reconnect
        self._stopping = False
        self._supervisor: Optional[asyncio.Task[None]] = None
        self.stats = ConnectionStats()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._supervisor is None:
            self._stopping = False
            self._supervisor = asyncio.create_task(self._supervise(), name="mcp-supervisor")

    async def stop(self) -> None:
        self._stopping = True
        self._cycle.set()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None
        self._state = ConnectionState.STOPPED
        self._ready.clear()

    async def wait_ready(self, timeout: float = 60.0) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- introspection -----------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state is ConnectionState.READY and self._session is not None

    @property
    def catalog(self) -> ToolCatalog:
        if self._catalog is None:
            raise NotConnected("Tool catalogue is not available yet (no successful tools/list).")
        return self._catalog

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "session_id": self.stats.session_id,
            "connects": self.stats.connects,
            "failures": self.stats.failures,
            "auth_failures": self.stats.auth_failures,
            "calls": self.stats.calls,
            "call_errors": self.stats.call_errors,
            "timeouts": self.stats.timeouts,
            "last_error": self.stats.last_error,
            "tools": self._catalog.names if self._catalog else [],
            "server": self.stats.server_info,
        }

    # -- supervisor --------------------------------------------------------

    async def _supervise(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                self._state = ConnectionState.CONNECTING
                await self._run_session()
                attempt = 0  # a clean cycle resets the backoff ladder
                delay = self._cfg.reconnect_base_delay
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - supervisor must not die
                attempt += 1
                self.stats.failures += 1
                detail = describe_exception(exc)
                self.stats.last_error = detail[:400]
                if classify_failure(exc) == "auth":
                    self.stats.auth_failures += 1
                    self._state = ConnectionState.AUTH_FAILED
                    delay = self._cfg.auth_failure_delay
                    log.error("MCP AUTH FAILURE: %s", detail[:400])
                    await self._notifier.send(
                        "🚨 <b>AUTH FAILURE</b>\n"
                        "cTrader MCP rejected the Bearer token (401/403).\n"
                        f"Trading is <b>paused</b>; retrying in {int(delay)}s.\n"
                        f"<code>{esc(detail[:300])}</code>",
                        dedup_key="auth-failure",
                    )
                else:
                    self._state = ConnectionState.DISCONNECTED
                    delay = min(
                        self._cfg.reconnect_base_delay * (2 ** (attempt - 1)),
                        self._cfg.reconnect_max_delay,
                    )
                    log.warning("MCP connection lost (attempt %s, %s): %s",
                                attempt, classify_failure(exc), detail[:400])
                    if attempt == 3:
                        await self._notifier.send(
                            "⚠️ <b>MCP connection unstable</b>\n"
                            f"{esc(detail[:250])}\nRetrying with backoff.",
                            dedup_key="conn-unstable",
                        )
            finally:
                self._session = None
                self._ready.clear()

            if self._stopping:
                break
            # Full jitter avoids synchronised reconnect storms after an outage.
            await asyncio.sleep(delay * (0.5 + random.random() * 0.5))

    @contextlib.asynccontextmanager
    async def _open_streams(self) -> AsyncIterator[tuple[Any, Any, Callable[[], Optional[str]]]]:
        """Open the Streamable HTTP transport across both SDK generations."""
        headers = {
            "Authorization": f"Bearer {self._cfg.mcp_token}",
            "User-Agent": "ict-silver-bullet-bot/1.0",
        }
        captured: dict[str, Optional[str]] = {"session_id": None}

        if _IS_V2:
            import httpx2
            from mcp.shared._httpx_utils import create_mcp_http_client

            async def _capture(response: "httpx2.Response") -> None:
                # The session id is minted by the real server on initialize; we
                # only observe it here so it can be logged and surfaced on /status.
                value = response.headers.get(MCP_SESSION_HEADER)
                if value and value != captured["session_id"]:
                    captured["session_id"] = value
                    self.stats.session_id = value
                    log.info("MCP session established by upstream: %s", value)

            http_client = create_mcp_http_client(
                headers=headers,
                timeout=httpx2.Timeout(
                    self._cfg.mcp_call_timeout,
                    connect=self._cfg.mcp_connect_timeout,
                    read=self._cfg.mcp_call_timeout,
                    # SSE responses stay open between messages; the read budget
                    # for the stream itself is intentionally generous.
                    pool=self._cfg.mcp_connect_timeout,
                ),
            )
            http_client.event_hooks["response"].append(_capture)
            async with http_client:
                async with _CLIENT_FACTORY(self._cfg.mcp_upstream, http_client=http_client) as streams:
                    read_stream, write_stream = streams[0], streams[1]
                    yield read_stream, write_stream, lambda: captured["session_id"]
        else:
            async with _CLIENT_FACTORY(
                self._cfg.mcp_upstream,
                headers=headers,
                timeout=_timeout_arg(self._cfg.mcp_connect_timeout),
                sse_read_timeout=_timeout_arg(max(60.0, self._cfg.mcp_call_timeout * 4)),
            ) as streams:
                read_stream, write_stream = streams[0], streams[1]
                get_session_id = streams[2] if len(streams) > 2 else (lambda: None)
                yield read_stream, write_stream, get_session_id

    async def _run_session(self) -> None:
        """One connection lifetime: handshake, discover, then hold open."""
        self._cycle.clear()
        log.info("Connecting to MCP upstream %s", self._cfg.mcp_upstream)

        async with self._open_streams() as (read_stream, write_stream, get_session_id):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=_timeout_arg(self._cfg.mcp_call_timeout),
            ) as session:
                # 1) Real handshake against the real upstream.
                init = await asyncio.wait_for(
                    session.initialize(), timeout=self._cfg.mcp_connect_timeout
                )
                server_info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
                self.stats.server_info = {
                    "name": getattr(server_info, "name", None),
                    "version": getattr(server_info, "version", None),
                    "protocol": getattr(init, "protocol_version", None)
                    or getattr(init, "protocolVersion", None),
                }
                session_id = get_session_id()
                if session_id:
                    self.stats.session_id = session_id
                log.info(
                    "MCP initialize OK - server=%s v=%s protocol=%s session=%s",
                    self.stats.server_info.get("name"),
                    self.stats.server_info.get("version"),
                    self.stats.server_info.get("protocol"),
                    self.stats.session_id or "(not exposed)",
                )

                # 2) Discover tools - names and schemas are never assumed.
                self._catalog = await self._discover(session)
                self._session = session
                self._state = ConnectionState.READY
                self.stats.connects += 1
                self._ready.set()

                # 3) Hold the session open until asked to cycle or stop.  The
                #    transport context must be exited by this same task.
                await self._cycle.wait()
                log.info("MCP session cycle requested; closing cleanly.")

    async def _discover(self, session: ClientSession) -> ToolCatalog:
        result = await asyncio.wait_for(
            session.list_tools(), timeout=self._cfg.mcp_call_timeout
        )
        specs: list[ToolSpec] = []
        for tool in getattr(result, "tools", []) or []:
            schema = (
                getattr(tool, "input_schema", None)
                or getattr(tool, "inputSchema", None)
                or {}
            )
            if hasattr(schema, "model_dump"):
                schema = schema.model_dump(by_alias=True, exclude_none=True)
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=(getattr(tool, "description", "") or "")[:400],
                    input_schema=dict(schema) if isinstance(schema, dict) else {},
                )
            )
        catalog = ToolCatalog(specs)
        log.info("Discovered %d MCP tools: %s", len(specs), catalog.names)
        for spec in specs:
            log.info("  tool | %s", describe_tool(spec))
        return catalog

    def request_reconnect(self, reason: str) -> None:
        """Ask the supervisor to rebuild the session (used after a hung call)."""
        if self._state is ConnectionState.READY:
            log.warning("Forcing MCP reconnect: %s", reason)
        self._ready.clear()
        self._session = None
        self._cycle.set()

    # -- calls -------------------------------------------------------------

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout: Optional[float] = None
    ) -> Any:
        """Invoke a tool with an explicit timeout so a hung upstream can't stall the bot."""
        session = self._session
        if session is None or self._state is not ConnectionState.READY:
            raise NotConnected(f"MCP session not ready (state={self._state.value}); cannot call {name}")

        budget = timeout or self._cfg.mcp_call_timeout
        self.stats.calls += 1
        try:
            return await asyncio.wait_for(
                session.call_tool(name, arguments, read_timeout_seconds=_timeout_arg(budget)),
                timeout=budget + 5.0,
            )
        except asyncio.TimeoutError as exc:
            self.stats.timeouts += 1
            self.stats.call_errors += 1
            # A timed-out request leaves the stream in an unknown state: cycle it.
            self.request_reconnect(f"timeout calling {name}")
            raise ToolCallError(f"Tool {name} timed out after {budget}s") from exc
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self.stats.call_errors += 1
            detail = describe_exception(exc)
            if classify_failure(exc) == "auth":
                self.request_reconnect(f"auth failure calling {name}")
                raise AuthFailure(f"Auth rejected while calling {name}: {detail}") from exc
            self.request_reconnect(f"transport error calling {name}: {detail[:120]}")
            raise ToolCallError(f"Tool {name} failed: {detail}") from exc

    async def ping(self) -> bool:
        """Cheap liveness probe used by the idle heartbeat."""
        session = self._session
        if session is None:
            return False
        try:
            await asyncio.wait_for(session.send_ping(), timeout=self._cfg.mcp_call_timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("MCP ping failed: %s", str(exc)[:200])
            self.request_reconnect("ping failed")
            return False
