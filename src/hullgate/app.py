"""ASGI applications of the two listeners: public and administration (BR-033, BR-034, BR-068).

Public: `/healthz`, `/readyz`, and the organisation-scoped routes under `/o/<id>/` (F-014): the
endpoints `/o/<id>/p/<path>/mcp`, the authorization servers `/o/<id>/oauth/p/<path>/…` and the
two families of metadata `/.well-known/…/o/<id>/…`, read on the raw path (BR-161); any other
path, the forms in force before F-014 and `/admin` included, 404.

Administration (F-016, BR-068 v2): `hullgate.admin.app.build_admin_app`, re-exported
here — `/healthz`, the console shell and the sign-in and enrolment ceremonies without a session,
the rest of `/admin/api/v1` behind an administrator session.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Sequence

from anyio import to_thread
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from hullgate import config, oauth, routes
from hullgate.access_tokens import JwtResolver
from hullgate.admin import barrier
from hullgate.admin.app import build_admin_app
from hullgate.admin.client_address import scope_address
from hullgate.authorization import Resolver
from hullgate.cimd import Fetcher
from hullgate.cipher import ConnectionsKey
from hullgate.db import Database
from hullgate.keys import SigningKey, TotpSecret
from hullgate.limits import CLIENT_ADDRESS, Limits
from hullgate.mcp_endpoint import DEFAULT_RESOLVERS, RoleEndpoint, build_mcp_server
from hullgate.outbound import Outbound
from hullgate.relay import Relay
from hullgate.tree import Clock
from hullgate.upstream_mcp import Transport, UpstreamClient
from hullgate.vault import Vault

log = logging.getLogger("hullgate.public")

STATE_ADDRESS = "client_address"  # the request's client address, BR-208's function (BR-300)
UNDECLARED_LOG_S = 3600.0  # BR-302: one line per hour for a forwarding peer outside the list


class _UndeclaredForwarder:
    """BR-302: a peer outside `TRUSTED_PROXIES` that carries a forwarded-for header is logged, once
    per hour, so that a wrong or stale value shows."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self.clock = clock
        self._last: float | None = None
        self._lock = threading.Lock()

    def seen(self, peer: str) -> None:
        with self._lock:
            now = self.clock()
            if self._last is not None and now - self._last < UNDECLARED_LOG_S:
                return
            self._last = now
        log.warning(
            "forwarded-for header from %s, outside TRUSTED_PROXIES: ignored (BR-302, check it)",
            peer,
        )


async def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


PROBES = ("/healthz", "/readyz")  # the instance's own routes, outside `/o/<id>` (BR-068 v2)


def _not_found_response() -> JSONResponse:
    """Any path that is no route of the public listener: the JSON 404 of an unknown role, with no
    hint of a right form and no `Location` (BR-161, BR-068 v2)."""
    return JSONResponse({"error": "not_found"}, status_code=404)


async def _not_found(_: Request, __: Exception) -> JSONResponse:
    return _not_found_response()


def build_public_app(
    db: Database,
    public_url: str,
    *,
    key: SigningKey | None = None,
    totp: TotpSecret | None = None,
    resolvers: Sequence[Resolver] | None = None,
    clock: Clock = time.time,
    fetcher: Fetcher | None = None,
    connections_key: ConnectionsKey | None = None,
    relay: Relay | None = None,
    proxies: Sequence[config.Network] = (),
    limits: Limits | None = None,
    admin_origin: config.AdminOrigin | None = None,
) -> ASGIApp:
    """Public listener: probes, role endpoints and their authorization servers.

    Without `key`, the key is not loaded: `/readyz` 503 `key_unavailable` (BR-033 v2) and every
    protected route 503 (BR-064). Default `resolvers`: operator tokens then OAuth JWT.
    Default `relay`: built here, once for the listener, with one single outbound OAuth client
    and one single upstream client (BR-125); the tests inject their own.
    `proxies`: `TRUSTED_PROXIES` (BR-208, BR-300). `limits`: the listener's in-memory limits
    (F-020), built here once by default; the tests that make many refusals inject theirs.
    """
    vault = Vault(db, key, totp)
    limits = limits or Limits()
    forwarders = _UndeclaredForwarder()

    async def readyz(_: Request) -> JSONResponse:
        state = await to_thread.run_sync(vault.readiness)
        if state.ok:
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "unavailable", "reason": state.reason}, status_code=503)

    if resolvers is None:
        jwt_resolvers: tuple[Resolver, ...] = (
            (JwtResolver(key, public_url, clock),) if key is not None else ()
        )
        resolvers = (*DEFAULT_RESOLVERS, *jwt_resolvers)
    if relay is None:
        key_file = connections_key or ConnectionsKey(config.DEFAULT_CONNECTIONS_KEY_FILE)
        relay = Relay(
            db, key_file, Outbound(), UpstreamClient(Transport()), clock=clock, limits=limits
        )
    server = build_mcp_server(relay)
    sessions = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
    # lot C (BR-120): streamed responses for the only `tools/call` carrying a `progressToken`
    streaming = StreamableHTTPSessionManager(server, json_response=False, stateless=True)
    endpoint = RoleEndpoint(
        db, public_url, sessions, resolvers, clock, vault.readiness, relay, streaming, limits
    )
    servers = oauth.AuthorizationServers(
        vault,
        public_url,
        clock,
        fetcher or Fetcher(),
        limits,
        barrier_origin=barrier.origin(public_url, admin_origin),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with sessions.run(), streaming.run():
            yield

    probes = Starlette(
        routes=[
            Route("/healthz", _healthz, methods=["GET"]),
            Route("/readyz", readyz, methods=["GET"]),
        ],
        lifespan=lifespan,
        exception_handlers={404: _not_found},
    )

    # Starlette's router would answer `/healthz/` with a 307 whose `Location` is built from the
    # request's Host (review 587, B1): no slash redirect on this listener.
    probes.router.redirect_slashes = False

    def address(scope: Scope) -> None:
        """Once per request, before anything else; probes never call it (F-020 §3.1)."""
        found, undeclared = scope_address(scope, proxies)
        if undeclared is not None:
            forwarders.seen(undeclared)
        scope.setdefault("state", {})[STATE_ADDRESS] = found
        CLIENT_ADDRESS.set(found)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        route = routes.route_of(scope)  # raw path only (BR-161)
        if route is not None and route.action == routes.MCP:
            address(scope)
            await endpoint(scope, receive, send)
            return
        if route is not None or (scope["type"] == "http" and oauth.oauth_family(scope)):
            # every authorization route, of the instance's identifier or not, and every unknown
            # form of one: BR-306's budget before BR-161's 404 (F-020 BR-161 clarification)
            address(scope)
            await servers(scope, receive, send)
            return
        if scope["type"] == "http" and routes.raw_request_path(scope) not in PROBES:
            # the probes too are matched on the raw path, exactly: `/healthz/`, `/%68ealthz` and
            # any other path answer the JSON 404, with no redirect (BR-068 v2)
            await _not_found_response()(scope, receive, send)
            return
        await probes(scope, receive, send)

    return app


__all__ = ["build_admin_app", "build_public_app"]
