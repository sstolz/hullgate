"""MCP endpoint of a role: `/o/<id>/p/<client>/<project>/<env>/<role>/mcp` (BR-055, BR-064, BR-067,
BR-161). The route is read on the raw path by `hullgate.routes`; step 1 checks that the identifier
is the instance's own and the role exists, under the store, so an outage answers 503 (BR-064).

The authorization order is played in full before the SDK's MCP entry (stateless mode, JSON
responses). Since F-008 (BR-067 v2), the MCP server serves the tools of the connections placed on
the URL's role through the relay (`hullgate.relay.Relay`); the authorized role and actor reach
the SDK's server through the ASGI request state, never through a client parameter.

Lot C (BR-120): a `tools/call` carrying a `progressToken`, from a client that accepts
`text/event-stream`, is served as a stream (SSE) by a second SDK manager, to return the upstream's
progress to it; any other request stays JSON. When the client closes the connection of a
`tools/call` in progress (streamed or not), the upstream call is cancelled (`Cancel`).

F-020: a request body is read in one place, `_read_body`; a POST is read under BR-311's 2 MiB
bound (413 `request_too_large`, audited `access.body_refused`). HULL-85 adds the fixed 30-second
whole-body deadline (408 `request_timeout`) and repeats authorization before dispatch. An unknown
credential beyond BR-308's budget gets its unchanged answer and writes no entry of its own;
refusal entries carry the client address (BR-313).
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
import anyio.lowlevel
import mcp_types as types
from anyio import CapacityLimiter, from_thread, to_thread
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http import check_accept_headers
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import progress_token_from_params
from mcp_types.version import KNOWN_PROTOCOL_VERSIONS
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from hullgate import __version__, audit, limits, routes, tokens
from hullgate import body as body_reader
from hullgate.authorization import Outcome, Resolver, authorize
from hullgate.body import TOO_LARGE as TOO_LARGE
from hullgate.body import read_body
from hullgate.db import Database, Readiness, StoreUnavailable, transaction
from hullgate.limits import Limits
from hullgate.relay import Caller, Relay, UnknownTool
from hullgate.tree import Clock, Node, Tree
from hullgate.upstream_mcp import Cancel, Progress

log = logging.getLogger("hullgate.endpoint")

INVALID_PARAMS = -32602
UNSUPPORTED_PROTOCOL_VERSION = -32022
VERSION_ERROR_BODY_MAX = 64 * 1024
BODY_MAX = 2 * 1024 * 1024  # BR-311: no option lifts it
A_BODY_REFUSED = "access.body_refused"
REQUEST_TOO_LARGE = "request_too_large"
REQUEST_TIMEOUT = "request_timeout"
UNKNOWN_CREDENTIAL = ("missing_token", "invalid_token")  # BR-308's closed list, access point
# Keep the F-020 reader name available for its existing callers and instrumentation.
_read_body = read_body
DEFAULT_RESOLVERS: tuple[Resolver, ...] = (tokens.resolve_operator,)
STATE_KEY = "hullgate.caller"  # authorized role and actor, set by the endpoint
STATE_CANCEL = "hullgate.cancel"  # cancellation of a `tools/call` by the client (BR-120)
STATE_STREAM = "hullgate.stream"  # `tools/call` served as a stream: progress returned (BR-120)
RELAY_THREADS = 96  # blocking relay calls, kept apart from the threads of other requests
PROGRESS_SEND_S = 1.0  # a progress notification not written to the client is dropped


def _state(ctx: ServerRequestContext[Any], key: str) -> object:
    scope = getattr(ctx.request, "scope", None)
    state = scope.get("state") if isinstance(scope, dict) else None
    return state.get(key) if isinstance(state, dict) else None


def _caller(ctx: ServerRequestContext[Any]) -> Caller | None:
    found = _state(ctx, STATE_KEY)
    return found if isinstance(found, Caller) else None


async def _run_relay(
    call: Callable[[], dict[str, Any]],
    limiter: CapacityLimiter,
    unbounded: CapacityLimiter,
    cancel: Cancel,
) -> dict[str, Any]:
    """Blocking relay call in a thread, under the `RELAY_THREADS` ceiling held **by the thread**.

    When the SDK cancels the handling (the client closed the stream), the wait is abandoned and
    the cancellation set (BR-120): the relay cuts its exchange. The abandoned thread keeps its slot
    among the `RELAY_THREADS` until it ends and returns it itself (review 470, m2: a cancelled
    thread does not free its slot before it has finished)."""
    borrower = object()
    await limiter.acquire_on_behalf_of(borrower)
    lock = threading.Lock()
    # "started": the thread has begun the call; "abandoned": the wait is gone. The slot is
    # returned exactly once: by the wait if the thread has not started (anyio may never start it,
    # review 473 B3) or if it has finished, by the thread otherwise.
    state = {"started": False, "done": False, "abandoned": False}

    def release() -> None:
        limiter.release_on_behalf_of(borrower)

    def work() -> dict[str, Any]:
        with lock:
            if state["abandoned"]:  # the wait left before the start: nothing to do
                return {}
            state["started"] = True
        try:
            return call()
        finally:
            with lock:
                state["done"] = True
                abandoned = state["abandoned"]
            if abandoned:  # the wait is gone: the thread returns its slot when it ends
                with contextlib.suppress(Exception):  # loop stopped: nothing to return
                    from_thread.run_sync(release)

    try:
        result = await to_thread.run_sync(work, limiter=unbounded, abandon_on_cancel=True)
    except anyio.get_cancelled_exc_class():
        cancel.set()
        with lock:
            state["abandoned"] = True
            mine = not state["started"] or state["done"]
        if mine:
            release()
        raise
    except BaseException:
        release()
        raise
    release()
    return result


def _progress(ctx: ServerRequestContext[Any]) -> Progress:
    """Returns to the client, under its own token and in the stream of its response, a progress
    read by the relay in a thread (BR-120); a write that does not complete within 1 s is dropped."""
    loop = anyio.lowlevel.current_token()

    async def emit(value: float, total: float | None, message: str | None) -> None:
        with anyio.move_on_after(PROGRESS_SEND_S):
            await ctx.session.report_progress(value, total, message)

    def progress(value: float, total: float | None, message: str | None) -> None:
        from_thread.run(emit, value, total, message, token=loop)

    return progress


def build_mcp_server(relay: Relay | None = None) -> Server[Any]:
    """The SDK's MCP server; `tools/list` and `tools/call` go through the relay (BR-067 v2).
    With no relay (or no authorized role set by the endpoint): empty list, every call
    "unknown tool", as in F-005. Announced capability: `tools` only (BR-121)."""
    limiter: list[CapacityLimiter] = []

    def threads() -> CapacityLimiter:
        if not limiter:
            limiter.append(CapacityLimiter(RELAY_THREADS))
            # call threads: their ceiling is `threads()`, held by each thread (m2)
            limiter.append(CapacityLimiter(math.inf))
        return limiter[0]

    async def list_tools(
        ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        caller = _caller(ctx)
        if relay is None or caller is None:
            return types.ListToolsResult(tools=[])
        # one page, with no `nextCursor`; a client cursor is ignored (BR-109). Upstream
        # definitions validated by the relay, serialized by the SDK per the client's version.
        tools = await to_thread.run_sync(relay.list_tools, caller, limiter=threads())
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in tools])

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        caller = _caller(ctx)
        if relay is None or caller is None:
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {params.name}")
        found = _state(ctx, STATE_CANCEL)
        cancel = found if isinstance(found, Cancel) else Cancel()
        # progress returned only when streamed, for a client that carries a token (BR-120)
        streamed = _state(ctx, STATE_STREAM) is True
        token = progress_token_from_params(ctx.params) if streamed else None
        call = functools.partial(
            relay.call_tool, caller, params.name, params.arguments,
            progress=_progress(ctx) if token is not None else None, cancel=cancel,
        )  # fmt: skip
        try:
            result: dict[str, Any] = await _run_relay(call, threads(), limiter[1], cancel)
        except UnknownTool:
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {params.name}") from None
        # upstream result relayed as is (BR-126, validated by the relay), serialized by the SDK
        return types.CallToolResult.model_validate(result)

    return Server("hullgate", version=__version__, on_list_tools=list_tools, on_call_tool=call_tool)


def _json(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(body, status_code=status, headers=headers)


UNAVAILABLE = {"error": "temporarily_unavailable"}


@dataclass(slots=True)
class RoleEndpoint:
    """ASGI application of the `/o/<id>/p/…/mcp` routes; one single instance per public listener.

    The route is read again on the raw path here (`hullgate.routes.route_of`), never on
    `scope["path"]`."""

    db: Database
    public_url: str
    sessions: StreamableHTTPSessionManager
    resolvers: Sequence[Resolver] = DEFAULT_RESOLVERS
    clock: Clock = time.time
    ready: Callable[[], Readiness] | None = None  # vault predicate (database and key), BR-064
    relay: Relay | None = None  # F-008: checked against the database at each request served
    streaming: StreamableHTTPSessionManager | None = None  # lot C: streamed responses (BR-120)
    limits: Limits | None = None  # F-020: the listener's limits (BR-308)

    def urls(self, org_id: str, path: str) -> tuple[str, str]:
        return (
            routes.resource_url(self.public_url, org_id, path),
            routes.resource_metadata_url(self.public_url, org_id, path),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not await self._access(scope, receive, send):
            return
        body = b""
        version = Request(scope).headers.get("mcp-protocol-version")
        unsupported = version is not None and version not in KNOWN_PROTOCOL_VERSIONS
        if scope.get("method") == "POST" or unsupported:
            try:
                read = await _read_body(
                    scope, receive, VERSION_ERROR_BODY_MAX if unsupported else BODY_MAX
                )
            except TimeoutError:
                await self._body_refused(
                    scope, receive, send, timeout=body_reader.BODY_READ_TIMEOUT
                )
                return
            if read is None:
                return
            if not isinstance(read, bytes):
                if not unsupported:
                    await self._body_refused(scope, receive, send)
                    return
            else:
                body = read
            # Repeat the full access decision with fresh database state and time. No SDK or
            # upstream dispatch can use the caller captured before the body arrived.
            if not await self._access(scope, receive, send):
                return
        if self.relay is not None:
            try:
                await to_thread.run_sync(self.relay.reconcile)
            except StoreUnavailable:
                await _json(503, UNAVAILABLE)(scope, receive, send)
                return
        await self._serve_mcp(scope, receive, send, body)

    async def _access(self, scope: Scope, receive: Receive, send: Send) -> bool:
        route = routes.route_of(scope)
        if route is None or route.action != routes.MCP:
            await _json(404, {"error": "not_found"})(scope, receive, send)
            return False
        path = route.path
        # (1) the instance's identifier and an existing role; an unreadable database answers
        # 503, never 404 (BR-064, BR-161); a foreign identifier answers as an unknown role
        try:
            found = await to_thread.run_sync(
                self.db.call, lambda c: _find_role(c, route.org_id, path)
            )
        except StoreUnavailable:
            await _json(503, UNAVAILABLE)(scope, receive, send)
            return False
        if found is None:
            await _json(404, {"error": "not_found"})(scope, receive, send)
            return False
        role, org_id = found
        # (2) vault and key available: the /readyz predicate, at each request (BR-064)
        state = await to_thread.run_sync(self.ready or self.db.readiness)
        if not state.ok:
            await _json(503, UNAVAILABLE)(scope, receive, send)
            return False
        # (3) to (7)
        endpoint_url, metadata_url = self.urls(org_id, path)
        bearer = _bearer(Request(scope).headers.get("authorization"))
        try:
            outcome = await to_thread.run_sync(
                self.db.call,
                lambda c: _authorize(c, path, endpoint_url, bearer, self.resolvers, self.clock()),
            )
        except StoreUnavailable:
            await _json(503, UNAVAILABLE)(scope, receive, send)
            return False
        if outcome is None:  # the role disappeared between step 1 and step 3
            await _json(404, {"error": "not_found"})(scope, receive, send)
            return False
        if not outcome.ok:
            await to_thread.run_sync(self._audit_refusal, role, path, outcome, self.clock())
            await _refusal(outcome, metadata_url)(scope, receive, send)
            return False
        scope.setdefault("state", {})[STATE_KEY] = Caller(
            role.id, path, outcome.actor, outcome.token_key
        )
        return True

    def _audit_refusal(self, role: Node, path: str, outcome: Outcome, now: float) -> None:
        """A refusal is served even if its audit entry could not be written (BR-066).

        BR-308: an unknown credential counts against its limit key; beyond 30 in 60 s it writes
        no `access.refused`, only BR-312's coalesced entry. An authentic credential's refusal is
        always written."""
        entry: dict[str, Any] = {
            "actor": outcome.actor,
            "action": "access.refused",
            "result": "refused",
            "node_id": role.id,
            "path": path,
            "reason": outcome.reason,
            "detail": {"step": outcome.step, **limits.address_detail()},
        }
        if self.limits is not None and outcome.reason in UNKNOWN_CREDENTIAL:
            key = limits.limit_key(limits.CLIENT_ADDRESS.get() or "")
            if not self.limits.unknown_credential(key):
                suppressed = self.limits.coalescer.refused(limits.CREDENTIAL, key)
                if suppressed is None:
                    return
                entry = limits.refusal_entry(
                    limits.CREDENTIAL, key, suppressed, actor=outcome.actor,
                    node_id=role.id, path=path,
                )  # fmt: skip
        self._write(entry, now)

    def _write(self, entry: dict[str, Any], now: float) -> None:
        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                audit.write(conn, now=now, **entry)

        try:
            self.db.call(write)
        except StoreUnavailable as exc:
            log.error("refusal audit not written (%s, %s): %s", entry["path"], entry["reason"], exc)

    async def _body_refused(
        self, scope: Scope, receive: Receive, send: Send, *, timeout: float | None = None
    ) -> None:
        """Body size/deadline refusal, audited with the actor that passed the header check."""
        reason = REQUEST_TOO_LARGE if timeout is None else REQUEST_TIMEOUT
        detail = {"limit_bytes": BODY_MAX} if timeout is None else {"timeout_seconds": timeout}
        found = scope.get("state", {}).get(STATE_KEY)
        if isinstance(found, Caller):
            entry = {
                "actor": found.actor,
                "action": A_BODY_REFUSED,
                "result": "refused",
                "node_id": found.role_id,
                "path": found.role_path,
                "reason": reason,
                "detail": {**detail, **limits.address_detail()},
            }
            await to_thread.run_sync(self._write, entry, self.clock())
        await _json(
            413 if timeout is None else 408,
            {"error": reason},
            {"Connection": "close"} if timeout is not None else None,
        )(scope, receive, send)

    async def _serve_mcp(self, scope: Scope, receive: Receive, send: Send, body: bytes) -> None:
        request = Request(scope, receive)
        version = request.headers.get("mcp-protocol-version")
        if version is None or version in KNOWN_PROTOCOL_VERSIONS:
            await self._dispatch(scope, receive, send, body)
            return
        # Explicit extension: the SDK lists only the modern versions in `supported`; the gateway
        # enumerates the five versions served (BR-067). Body read under a bound, for the `id`.
        try:
            parsed = json.loads(body)
            rid = parsed.get("id") if isinstance(parsed, dict) else None
        except ValueError:
            rid = None
        error = {
            "code": UNSUPPORTED_PROTOCOL_VERSION,
            "message": "Unsupported protocol version",
            "data": {"supported": list(KNOWN_PROTOCOL_VERSIONS), "requested": version},
        }
        await _json(400, {"jsonrpc": "2.0", "id": rid, "error": error})(scope, receive, send)

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send, body: bytes) -> None:
        """A `tools/call` is read before the SDK (body replayed as is): streamed if it carries a
        `progressToken` and the client accepts SSE, JSON otherwise; in both cases, the client
        closing the connection during the call cancels it (BR-120)."""
        if scope.get("method") != "POST":
            await self.sessions.handle_request(scope, receive, send)
            return
        replay = _replay(body, receive)
        params = _tools_call(body)
        if params is None:
            await self.sessions.handle_request(scope, replay, send)
            return
        cancel = Cancel()
        _, sse = check_accept_headers(Request(scope))
        manager = self.sessions
        if self.streaming is not None and sse and progress_token_from_params(params) is not None:
            manager = self.streaming
        state = scope.setdefault("state", {})
        state[STATE_CANCEL] = cancel
        state[STATE_STREAM] = manager is self.streaming
        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch_disconnect, receive, cancel)
            await manager.handle_request(scope, replay, send)
            tg.cancel_scope.cancel()


def _replay(body: bytes, receive: Receive) -> Receive:
    """A `receive` that first returns the body already read, then delegates (client disconnect)."""
    pending: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]

    async def wrapped() -> Message:
        if pending:
            return pending.pop()
        return await receive()

    return wrapped


def _tools_call(body: bytes) -> dict[str, Any] | None:
    """Parameters of a `tools/call` request (single JSON-RPC object), otherwise None."""
    try:
        doc = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or doc.get("method") != "tools/call" or "id" not in doc:
        return None
    params = doc.get("params")
    return params if isinstance(params, dict) else {}


async def _watch_disconnect(receive: Receive, cancel: Cancel) -> None:
    """Sets the cancellation when the client closes the connection; after the response, no
    effect (the relay no longer holds a hook)."""
    while (await receive())["type"] != "http.disconnect":
        pass
    cancel.set()


def _find_role(conn: sqlite3.Connection, org_id: str, path: str) -> tuple[Node, str] | None:
    """Step 1 (BR-055, BR-161): the role and the stored identifier, when the route's identifier
    is the instance's own and the path names a live role; None otherwise."""
    tree = Tree.load(conn)
    if not tree.public_id or org_id != tree.public_id:
        return None
    node = tree.find(path)
    return (node, tree.public_id) if node is not None and node.level == "role" else None


def _authorize(
    conn: sqlite3.Connection,
    path: str,
    endpoint_url: str,
    bearer: str | None,
    resolvers: Sequence[Resolver],
    now: float,
) -> Outcome | None:
    tree = Tree.load(conn)
    role = tree.find(path)
    if role is None or role.level != "role":
        return None
    return authorize(conn, tree, role, endpoint_url, bearer, resolvers, now)


def _bearer(header: str | None) -> str | None:
    """Value of an `Authorization: Bearer <token>` header; None if absent or of another scheme."""
    if header is None:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip()


def _refusal(outcome: Outcome, metadata_url: str) -> Response:
    if outcome.step == 3:
        challenge = f'Bearer resource_metadata="{metadata_url}"'
        return _json(401, {"error": "unauthorized"}, {"WWW-Authenticate": challenge})
    if outcome.status == 403:
        challenge = (
            f'Bearer error="insufficient_scope", scope="mcp", resource_metadata="{metadata_url}"'
        )
        return _json(403, {"error": "insufficient_scope"}, {"WWW-Authenticate": challenge})
    challenge = f'Bearer error="invalid_token", resource_metadata="{metadata_url}"'
    return _json(401, {"error": "invalid_token"}, {"WWW-Authenticate": challenge})
