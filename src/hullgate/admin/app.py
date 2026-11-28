"""The admin listener: `/healthz`, the console shell under `/admin`, the JSON API under
`/admin/api/v1` (F-016 lot A).

Middleware order, stated here only (§9): **Host** (BR-206, 421, `/healthz` excepted), **allow-list**
(BR-209, 403, before any other check), **security headers** (BR-212, BR-213: CSP, `nosniff`,
`no-referrer`, `no-store` on the API), **session** (BR-204, 401), **CSRF** (BR-205, 403
`csrf_refused` on every non-`GET`), then **freshness** for security acts (BR-210). The Host check,
the allow-list and the headers are one ASGI wrapper around everything; session, CSRF and freshness
are applied by `_endpoint` to each API route from its declaration, so a route added later gets
them by construction.

Without a session the listener serves `/healthz`, the data-free shell and the sign-in and enrolment
ceremonies, nothing else (BR-068 v2). No CORS header is ever sent.
"""

from __future__ import annotations

import importlib.metadata
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from anyio import to_thread
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hullgate import audit, connections
from hullgate.admin import accounts, auth, sessions
from hullgate.admin.base import ApiError, ApiRoute, Ctx, problem
from hullgate.admin.client_address import in_networks, scope_address
from hullgate.admin.console import ConsoleApi
from hullgate.admin.history import HistoryApi
from hullgate.admin.organisation import OrganisationApi
from hullgate.admin.policies import PolicyApi
from hullgate.body import TOO_LARGE, read_body
from hullgate.cipher import ConnectionsKey
from hullgate.config import AdminOrigin, Network
from hullgate.db import (
    Database,
    Readiness,
    StoreUnavailable,
    transaction,
    transaction_precondition,
)
from hullgate.outbound import Outbound
from hullgate.tree import Clock, Refused, Tree
from hullgate.upstream_mcp import UpstreamClient

API_PREFIX = "/admin/api/v1"
BODY_MAX = 64 * 1024
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = (
    (b"content-security-policy", CSP.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)
SIGN_IN_FAILED = "Sign-in failed. Try again."
RATE_LIMITED = "Too many attempts from your address. Wait, then try again."

T = TypeVar("T")


async def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _version() -> str:
    try:
        return importlib.metadata.version("hullgate")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# Routes without a session, named here once: BR-068 v2 and BR-205 read this list.
CEREMONIES = frozenset({"auth/options", "auth/assert", "enrol/options", "enrol/verify"})


def _host_port(value: str, default_port: int) -> tuple[str, int] | None:
    text = value.strip().lower()
    if not text:
        return None
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port_text = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port_text = text.partition(":")
    if port_text:
        if not (port_text.isascii() and port_text.isdigit()):
            return None
        return host, int(port_text)
    return host, default_port


class Guard:
    """Host check, allow-list and security headers (BR-206, BR-209, BR-212, BR-213)."""

    def __init__(
        self,
        app: ASGIApp,
        db: Database,
        origin: AdminOrigin,
        proxies: Sequence[Network],
    ) -> None:
        self.app = app
        self.db = db
        self.origin = origin
        self.proxies = tuple(proxies)
        # The API's route declarations, read by the sweeps of BR-068 v2, BR-205, BR-213, BR-234.
        self.api_routes: list[ApiRoute] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if path == "/healthz":
            await self.app(scope, receive, send)
            return
        headers = [(k.lower(), v) for k, v in scope.get("headers", [])]
        host = next((v.decode("latin-1") for k, v in headers if k == b"host"), "")
        default_port = 443 if self.origin.secure else 80
        if _host_port(host, default_port) != (self.origin.host, self.origin.port):
            await PlainTextResponse("", status_code=421)(scope, receive, send)
            return
        if path != "/admin" and not path.startswith("/admin/"):
            await self.app(scope, receive, send)
            return
        address, _ = scope_address(scope, self.proxies)
        scope.setdefault("state", {})["client_address"] = address
        api = path.startswith(API_PREFIX + "/") or path == API_PREFIX
        try:
            cidrs = await to_thread.run_sync(self.db.call, accounts.allowlist)
        except StoreUnavailable:
            refusal: Response = (
                problem(503, "store_unavailable", "The gateway's store is unavailable.")
                if api
                else PlainTextResponse("Service unavailable", status_code=503)
            )
            await self._send(refusal, api, scope, receive, send)
            return
        if cidrs and not in_networks(address, accounts.networks(cidrs)):
            refusal = (
                problem(403, "forbidden", "Forbidden.")
                if api
                else PlainTextResponse("Forbidden", status_code=403)
            )
            await self._send(refusal, api, scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = self._headers(list(message.get("headers", [])), api)
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _headers(current: list[tuple[bytes, bytes]], api: bool) -> list[tuple[bytes, bytes]]:
        names = {name for name, _ in SECURITY_HEADERS} | {b"cache-control"}
        kept = [(k, v) for k, v in current if k.lower() not in names]
        extra = [*SECURITY_HEADERS]
        if api:
            extra.append((b"cache-control", b"no-store"))
        else:
            keep_cache = [(k, v) for k, v in current if k.lower() == b"cache-control"]
            extra.extend(keep_cache or [(b"cache-control", b"no-store")])
        return kept + extra

    async def _send(
        self, response: Response, api: bool, scope: Scope, receive: Receive, send: Send
    ) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = self._headers(list(message.get("headers", [])), api)
            await send(message)

        await response(scope, receive, send_with_headers)


# -- the application -------------------------------------------------------------------------------


class AdminApi:
    """Handlers of `/admin/api/v1`; every write goes through `hullgate.admin.accounts`."""

    def __init__(
        self,
        db: Database,
        origin: AdminOrigin,
        public_url: str,
        readiness: Callable[[], Readiness],
        clock: Clock,
    ) -> None:
        self.db = db
        self.origin = origin
        self.public_url = public_url
        self.readiness = readiness
        self.clock = clock

    async def run(self, fn: Callable[[Any], T]) -> T:
        return await to_thread.run_sync(self.db.call, fn)

    def routes(self) -> list[ApiRoute]:
        return [
            ApiRoute("POST", "auth/options", self.auth_options, session=False),
            ApiRoute("POST", "auth/assert", self.auth_assert, session=False),
            ApiRoute("POST", "enrol/options", self.enrol_options, session=False),
            ApiRoute("POST", "enrol/verify", self.enrol_verify, session=False),
            ApiRoute("GET", "session", self.session_get),
            ApiRoute("POST", "session/signout", self.session_signout),
            ApiRoute("POST", "session/fresh/options", self.fresh_options),
            ApiRoute("POST", "session/fresh", self.fresh),
            ApiRoute("GET", "passkeys", self.passkeys_get),
            ApiRoute("POST", "passkeys/options", self.passkeys_options, fresh=True),
            ApiRoute("POST", "passkeys", self.passkeys_add, fresh=True),
            ApiRoute("DELETE", "passkeys/{credential_id}", self.passkeys_delete, fresh=True),
            ApiRoute("GET", "allowlist", self.allowlist_get),
            ApiRoute("PUT", "allowlist", self.allowlist_put, fresh=True),
            ApiRoute("GET", "meta", self.meta),
        ]

    # -- sign-in (BR-202, BR-207) -----------------------------------------------------------------

    def _limited_refusal(self, conn: Any, action: str, address: str, now: int) -> JSONResponse:
        audit.write(
            conn,
            actor=audit.ANONYMOUS,
            action=action,
            result="refused",
            reason="rate_limited",
            detail={"address": address},
            now=now,
        )
        return problem(429, "rate_limited", RATE_LIMITED)

    async def auth_options(self, ctx: Ctx) -> Response:
        def op(conn: Any) -> Response:
            with transaction(conn):
                if accounts.limited(conn, ctx.address, ctx.now):
                    return self._limited_refusal(conn, "admin.sign_in", ctx.address, ctx.now)
                try:
                    challenge = auth.new_challenge(conn, "sign_in", ctx.address, ctx.now)
                except auth.TooManyChallenges:
                    return self._limited_refusal(conn, "admin.sign_in", ctx.address, ctx.now)
            return JSONResponse({"publicKey": auth.authentication_options(self.origin, challenge)})

        return await self.run(op)

    def _credential(self, ctx: Ctx) -> dict[str, Any]:
        credential = ctx.body.get("credential")
        if not isinstance(credential, dict):
            raise ApiError(400, "invalid_request", "A credential object is expected.")
        return credential

    def _signed_in(
        self, admin: accounts.Administrator, value: str, token: str, now: int
    ) -> JSONResponse:
        response = JSONResponse(
            {
                "administrator": admin.as_dict(),
                "csrf_token": token,
                "expires_at": audit.utc(now + sessions.IDLE_S),
            }
        )
        response.set_cookie(
            sessions.COOKIE,
            value,
            path="/admin",
            httponly=True,
            samesite="strict",
            secure=self.origin.secure,
        )
        return response

    def _fail(
        self, conn: Any, action: str, reason: str, address: str, now: int, actor: str
    ) -> None:
        accounts.record_failure(conn, address, now)
        audit.write(
            conn,
            actor=actor,
            action=action,
            result="refused",
            reason=reason,
            detail={"address": address},
            now=now,
        )

    async def auth_assert(self, ctx: Ctx) -> Response:
        credential = self._credential(ctx)

        def op(conn: Any) -> Response:
            with transaction(conn):
                accounts.purge(conn, ctx.now)
                sessions.purge(conn, ctx.now)
                if accounts.limited(conn, ctx.address, ctx.now):
                    return self._limited_refusal(conn, "admin.sign_in", ctx.address, ctx.now)
                challenge = auth.take_challenge(conn, credential, "sign_in", ctx.now)
                if challenge is None:
                    self._fail(conn, "admin.sign_in", "unknown_challenge", ctx.address, ctx.now,
                               audit.ANONYMOUS)  # fmt: skip
                    return problem(401, "sign_in_failed", SIGN_IN_FAILED)
                try:
                    admin, key = auth.verify_assertion(
                        conn, credential, challenge, self.origin, ctx.now
                    )
                except auth.CeremonyFailed as exc:
                    self._fail(conn, "admin.sign_in", exc.reason, ctx.address, ctx.now,
                               audit.ANONYMOUS)  # fmt: skip
                    return problem(401, "sign_in_failed", SIGN_IN_FAILED)
                value, token = sessions.open_session(conn, admin.id, ctx.now)
                audit.write(
                    conn,
                    actor=accounts.actor(admin.id),
                    action="admin.sign_in",
                    result="ok",
                    detail={"passkey": key.credential_id, "address": ctx.address},
                    now=ctx.now,
                )
            return self._signed_in(admin, value, token, ctx.now)

        return await self.run(op)

    # -- enrolment (BR-201, BR-203) ---------------------------------------------------------------

    def _link(self, ctx: Ctx) -> str:
        link = ctx.body.get("link")
        if not isinstance(link, str):
            raise ApiError(400, "invalid_request", "An enrolment link is expected.")
        return link

    async def enrol_options(self, ctx: Ctx) -> Response:
        token = self._link(ctx)

        def op(conn: Any) -> Response:
            with transaction(conn):
                if accounts.limited(conn, ctx.address, ctx.now):
                    return self._limited_refusal(
                        conn, "admin.enrolment.refused", ctx.address, ctx.now
                    )
                link = accounts.find_link(conn, token, ctx.now)
                if link is None:
                    self._fail(conn, "admin.enrolment.refused", "invalid_link", ctx.address,
                               ctx.now, audit.ANONYMOUS)  # fmt: skip
                    return problem(404, "enrolment_invalid", ENROLMENT_INVALID)
                try:
                    challenge = auth.new_challenge(
                        conn,
                        "enrol",
                        ctx.address,
                        ctx.now,
                        administrator_id=link.administrator.id,
                        enrolment_digest=link.digest,
                    )
                except auth.TooManyChallenges:
                    return self._limited_refusal(
                        conn, "admin.enrolment.refused", ctx.address, ctx.now
                    )
                exclude = [p.credential_id for p in accounts.passkeys(conn, link.administrator.id)]
            admin = link.administrator
            return JSONResponse(
                {
                    "email": admin.email,
                    "name": admin.name,
                    "publicKey": auth.registration_options(self.origin, admin, challenge, exclude),
                }
            )

        return await self.run(op)

    async def enrol_verify(self, ctx: Ctx) -> Response:
        token = self._link(ctx)
        credential = self._credential(ctx)
        label = self._label(ctx)

        def op(conn: Any) -> Response:
            with transaction(conn):
                if accounts.limited(conn, ctx.address, ctx.now):
                    return self._limited_refusal(
                        conn, "admin.enrolment.refused", ctx.address, ctx.now
                    )
                challenge = auth.take_challenge(conn, credential, "enrol", ctx.now)
                link = accounts.find_link(conn, token, ctx.now)
                if link is None:
                    self._fail(conn, "admin.enrolment.refused", "invalid_link", ctx.address,
                               ctx.now, audit.ANONYMOUS)  # fmt: skip
                    return problem(404, "enrolment_invalid", ENROLMENT_INVALID)
                admin = link.administrator
                if challenge is None or challenge.enrolment_digest != link.digest:
                    self._fail(conn, "admin.enrolment.refused", "unknown_challenge",
                               ctx.address, ctx.now, audit.ANONYMOUS)  # fmt: skip
                    return problem(400, "enrolment_failed", ENROLMENT_FAILED)
                try:
                    new = auth.verify_registration(credential, challenge, self.origin)
                except auth.CeremonyFailed as exc:
                    self._fail(conn, "admin.enrolment.refused", exc.reason, ctx.address,
                               ctx.now, audit.ANONYMOUS)  # fmt: skip
                    return problem(400, "enrolment_failed", ENROLMENT_FAILED)
                if accounts.passkey(conn, new.credential_id) is not None:
                    # an authenticator replaying a registered credential: a failure, not a 500
                    self._fail(conn, "admin.enrolment.refused", "credential_exists", ctx.address,
                               ctx.now, audit.ANONYMOUS)  # fmt: skip
                    return problem(400, "enrolment_failed", ENROLMENT_FAILED)
                if not accounts.consume_link(conn, link, ctx.now):
                    return problem(404, "enrolment_invalid", ENROLMENT_INVALID)
                audit.write(
                    conn,
                    actor=accounts.actor(admin.id),
                    action="admin.enrolment.use",
                    result="ok",
                    detail={"kind": link.kind},
                    now=ctx.now,
                )
                accounts.add_passkey(
                    conn, admin, new.credential_id, new.public_key, new.sign_count, label, ctx.now
                )
                accounts.activate(conn, admin, ctx.now)
                value, csrf = sessions.open_session(conn, admin.id, ctx.now)
                audit.write(
                    conn,
                    actor=accounts.actor(admin.id),
                    action="admin.sign_in",
                    result="ok",
                    reason="enrolment",
                    detail={"passkey": new.credential_id, "address": ctx.address},
                    now=ctx.now,
                )
                active = accounts.get(conn, admin.id)
            return self._signed_in(active or admin, value, csrf, ctx.now)

        return await self.run(op)

    def _label(self, ctx: Ctx) -> str:
        label = ctx.body.get("label")
        if not isinstance(label, str):
            raise ApiError(422, "invalid_request", "A passkey label is expected.")
        try:
            return accounts.check_label(label)
        except Refused as exc:
            raise ApiError(422, "invalid_request", str(exc)) from exc

    # -- session (BR-204, BR-205, BR-210) ---------------------------------------------------------

    def _admin(self, conn: Any, ctx: Ctx) -> accounts.Administrator:
        admin = accounts.get(conn, ctx.administrator_id)
        if admin is None:
            raise ApiError(401, "unauthenticated", "Sign in again.")
        return admin

    async def session_get(self, ctx: Ctx) -> Response:
        session = ctx.live
        admin = await self.run(lambda conn: self._admin(conn, ctx))
        return JSONResponse(
            {
                "administrator": admin.as_dict(),
                "csrf_token": sessions.csrf_token(ctx.cookie),
                "expires_at": audit.utc(session.expires_at()),
                "fresh_until": audit.utc(session.fresh_until) if session.fresh_until else None,
            }
        )

    async def session_signout(self, ctx: Ctx) -> Response:
        session = ctx.live

        def op(conn: Any) -> None:
            with transaction(conn):
                sessions.close(conn, session.digest)
                audit.write(
                    conn,
                    actor=accounts.actor(session.administrator_id),
                    action="admin.sign_out",
                    result="ok",
                    now=ctx.now,
                )

        await self.run(op)
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(
            sessions.COOKIE,
            path="/admin",
            httponly=True,
            samesite="strict",
            secure=self.origin.secure,
        )
        return response

    async def fresh_options(self, ctx: Ctx) -> Response:
        def op(conn: Any) -> Response:
            with transaction(conn):
                admin = self._admin(conn, ctx)
                if accounts.limited(conn, ctx.address, ctx.now):
                    return self._limited_refusal(conn, "admin.fresh", ctx.address, ctx.now)
                try:
                    challenge = auth.new_challenge(
                        conn, "fresh", ctx.address, ctx.now, administrator_id=admin.id
                    )
                except auth.TooManyChallenges:
                    return self._limited_refusal(conn, "admin.fresh", ctx.address, ctx.now)
                allow = [p.credential_id for p in accounts.passkeys(conn, admin.id)]
            return JSONResponse(
                {"publicKey": auth.authentication_options(self.origin, challenge, allow)}
            )

        return await self.run(op)

    async def fresh(self, ctx: Ctx) -> Response:
        credential = self._credential(ctx)
        session = ctx.live

        def op(conn: Any) -> Response:
            with transaction(conn):
                admin = self._admin(conn, ctx)
                actor = accounts.actor(admin.id)
                if accounts.limited(conn, ctx.address, ctx.now):
                    return self._limited_refusal(conn, "admin.fresh", ctx.address, ctx.now)
                challenge = auth.take_challenge(conn, credential, "fresh", ctx.now)
                if challenge is None or challenge.administrator_id != admin.id:
                    self._fail(conn, "admin.fresh", "unknown_challenge", ctx.address, ctx.now,
                               actor)  # fmt: skip
                    return problem(403, "fresh_auth_failed", FRESH_FAILED)
                try:
                    auth.verify_assertion(conn, credential, challenge, self.origin, ctx.now)
                except auth.CeremonyFailed as exc:
                    self._fail(conn, "admin.fresh", exc.reason, ctx.address, ctx.now, actor)
                    return problem(403, "fresh_auth_failed", FRESH_FAILED)
                until = sessions.mark_fresh(conn, session.digest, ctx.now)
                audit.write(conn, actor=actor, action="admin.fresh", result="ok", now=ctx.now)
            return JSONResponse({"fresh_until": audit.utc(until)})

        return await self.run(op)

    # -- passkeys (BR-210) ------------------------------------------------------------------------

    async def passkeys_get(self, ctx: Ctx) -> Response:
        rows = await self.run(lambda conn: accounts.passkeys(conn, ctx.administrator_id))
        return JSONResponse({"passkeys": [p.as_dict() for p in rows]})

    async def passkeys_options(self, ctx: Ctx) -> Response:
        def op(conn: Any) -> Response:
            with transaction(conn):
                admin = self._admin(conn, ctx)
                try:
                    challenge = auth.new_challenge(
                        conn, "passkey", ctx.address, ctx.now, administrator_id=admin.id
                    )
                except auth.TooManyChallenges:
                    return problem(429, "rate_limited", RATE_LIMITED)
                exclude = [p.credential_id for p in accounts.passkeys(conn, admin.id)]
            return JSONResponse(
                {"publicKey": auth.registration_options(self.origin, admin, challenge, exclude)}
            )

        return await self.run(op)

    async def passkeys_add(self, ctx: Ctx) -> Response:
        credential = self._credential(ctx)
        label = self._label(ctx)

        def op(conn: Any) -> Response:
            with transaction(conn):
                admin = self._admin(conn, ctx)
                challenge = auth.take_challenge(conn, credential, "passkey", ctx.now)
                if challenge is None or challenge.administrator_id != admin.id:
                    return problem(400, "passkey_failed", PASSKEY_FAILED)
                try:
                    new = auth.verify_registration(credential, challenge, self.origin)
                except auth.CeremonyFailed:
                    return problem(400, "passkey_failed", PASSKEY_FAILED)
                try:
                    key = accounts.add_passkey(conn, admin, new.credential_id, new.public_key,
                                               new.sign_count, label, ctx.now)  # fmt: skip
                except Refused as exc:
                    return problem(409, "passkey_exists", str(exc))
            return JSONResponse({"passkey": key.as_dict()}, status_code=201)

        return await self.run(op)

    async def passkeys_delete(self, ctx: Ctx) -> Response:
        credential_id = str(ctx.request.path_params["credential_id"])
        session = ctx.live

        def op(conn: Any) -> Response:
            with transaction(conn):
                admin = self._admin(conn, ctx)
                try:
                    ended = accounts.delete_passkey(
                        conn, admin, credential_id, session.digest, ctx.now
                    )
                except accounts.LastPasskey as exc:
                    return problem(409, "last_passkey", str(exc))
            if ended is None:
                return problem(404, "not_found", "No such passkey on this account.")
            return JSONResponse({"deleted": credential_id, "sessions_ended": ended})

        return await self.run(op)

    # -- allow-list (BR-209, BR-235) --------------------------------------------------------------

    async def allowlist_get(self, ctx: Ctx) -> Response:
        cidrs = await self.run(accounts.allowlist)
        return JSONResponse({"cidrs": cidrs, "client_address": ctx.address})

    async def allowlist_put(self, ctx: Ctx) -> Response:
        values = ctx.body.get("cidrs")
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ApiError(422, "invalid_request", "A list of CIDRs is expected.")

        def op(conn: Any) -> Response:
            with transaction(conn):
                try:
                    cidrs = accounts.set_allowlist(
                        conn, values, ctx.address, accounts.actor(ctx.administrator_id), ctx.now
                    )
                except accounts.AllowlistSelf as exc:
                    return problem(409, "allowlist_self", str(exc))
                except Refused as exc:
                    return problem(422, "invalid_request", str(exc))
            return JSONResponse({"cidrs": cidrs, "client_address": ctx.address})

        return await self.run(op)

    # -- meta (§3.4, §3.15) -----------------------------------------------------------------------

    async def meta(self, ctx: Ctx) -> Response:
        state = await to_thread.run_sync(self.readiness)
        return JSONResponse(
            {
                "organisation_name": await self.run(lambda c: Tree.load(c).org.name),
                "version": _version(),
                "public_url": self.public_url,
                "admin_url": self.origin.origin,
                "rp_id": self.origin.rp_id,
                "readiness": {"ok": state.ok, "reason": state.reason or None},
                "features": {"web_return": True},
                "web_redirect_uri": connections.web_redirect_uri(self.origin.origin),
            }
        )


ENROLMENT_INVALID = "This enrolment link is used, expired or replaced."
ENROLMENT_FAILED = "The passkey could not be registered. Try again."
FRESH_FAILED = "The passkey was not confirmed. Try again."
PASSKEY_FAILED = "The passkey could not be added. Try again."


async def _read_json(request: Request) -> dict[str, Any]:
    # JSON escaping can expand each YAML byte up to six bytes. The domain enforces
    # the decoded document's independent 1 MiB ceiling. Other routes keep 64 KiB.
    maximum = (
        6 * 1024 * 1024 + 4096
        if request.url.path in (API_PREFIX + "/import/preview", API_PREFIX + "/import/apply")
        else BODY_MAX
    )
    raw = await read_body(request.scope, request.receive, maximum)
    if raw is TOO_LARGE:
        raise ApiError(413, "too_large", "The request body is too large.")
    if not isinstance(raw, bytes):
        raise ClientDisconnect()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ApiError(400, "invalid_request", "A JSON object is expected.") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_request", "A JSON object is expected.")
    try:
        # a lone surrogate ("\ud800") decodes from JSON but cannot be stored or echoed
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise ApiError(400, "invalid_request", "A JSON object of valid text is expected.") from None
    return body


def _endpoint(api: AdminApi, spec: ApiRoute) -> Callable[[Request], Awaitable[Response]]:
    """Session (401), CSRF (403 `csrf_refused`) and freshness (403) for one route."""

    async def endpoint(request: Request) -> Response:
        now = int(api.clock())
        address = str(request.scope.get("state", {}).get("client_address", ""))
        cookie = request.cookies.get(sessions.COOKIE, "")
        try:
            session: sessions.Session | None = None
            if spec.session:
                activity = request.headers.get(sessions.ACTIVITY_HEADER) == "1"
                session = await api.run(
                    lambda conn: sessions.resolve(conn, cookie, now, activity=activity)
                )
                if session is None:
                    return problem(401, "unauthenticated", "Sign in again.")
            if request.method != "GET":
                if request.headers.get("origin") != api.origin.origin:
                    return problem(403, "csrf_refused", "Request refused (origin).")
                if session is not None:
                    token = request.headers.get(sessions.CSRF_HEADER, "")
                    if not token or not session.csrf_matches(token):
                        return problem(403, "csrf_refused", "Request refused (CSRF token).")
            if spec.fresh and (session is None or not session.fresh(now)):
                return problem(403, "fresh_auth_required", "Confirm with your passkey first.")
            body = {}
            if request.method != "GET":
                try:
                    body = await _read_json(request)
                except TimeoutError:
                    response = problem(
                        408, "request_timeout", "The request body did not arrive in time."
                    )
                    response.headers["Connection"] = "close"
                    return response
            ctx = Ctx(request, int(api.clock()), address, body, session, cookie)

            def recheck(conn: Any) -> None:
                # Called again *after* BEGIN IMMEDIATE: no sign-out can slip between the
                # check and admission of the mutation after a worker/lock wait.
                ctx.now = int(api.clock())
                if spec.session:
                    ctx.session = sessions.resolve(conn, cookie, ctx.now, activity=False)
                    if ctx.session is None:
                        raise ApiError(401, "unauthenticated", "Sign in again.")
                if spec.fresh and (ctx.session is None or not ctx.session.fresh(ctx.now)):
                    raise ApiError(403, "fresh_auth_required", "Confirm with your passkey first.")

            await api.run(recheck)
            with transaction_precondition(recheck):
                return await spec.handler(ctx)
        except ClientDisconnect:
            return Response(status_code=400, headers={"Connection": "close"})
        except ApiError as exc:
            return problem(exc.status, exc.code, exc.message, **exc.extra)
        except StoreUnavailable:
            return problem(503, "store_unavailable", "The gateway's store is unavailable.")

    return endpoint


def _shell(static_dir: Path | None) -> Callable[[Request], Awaitable[Response]]:
    """The console's static bundle (BR-212): assets by name, `index.html` for any other path.

    Without a built bundle (unit and integration tests) every shell path is 404.
    """
    root = static_dir.resolve() if static_dir is not None else None

    async def shell(request: Request) -> Response:
        rest = str(request.path_params.get("rest", "")).lstrip("/")
        if root is None or not (root / "index.html").is_file():
            return PlainTextResponse("Not Found", status_code=404)
        if rest.startswith("api/") or rest == "api":
            return problem(404, "not_found", "No such route.")
        if rest.startswith("assets/"):
            target = (root / rest).resolve()
            if not target.is_relative_to(root / "assets") or not target.is_file():
                return PlainTextResponse("Not Found", status_code=404)
            return FileResponse(target, headers={"cache-control": "public, max-age=31536000"})
        return FileResponse(root / "index.html", media_type="text/html")

    return shell


def build_admin_app(
    db: Database,
    origin: AdminOrigin,
    *,
    public_url: str = "",
    proxies: Sequence[Network] = (),
    readiness: Callable[[], Readiness] | None = None,
    clock: Clock = time.time,
    static_dir: Path | None = None,
    connections_key: ConnectionsKey | None = None,
    outbound: Outbound | None = None,
    upstream: UpstreamClient | None = None,
) -> Guard:
    """The admin listener's application. `/healthz` answers whatever the store's state."""
    api = AdminApi(db, origin, public_url, readiness or db.readiness, clock)
    core = ConsoleApi(
        db,
        origin,
        public_url,
        clock,
        key=connections_key,
        outbound=outbound,
        upstream=upstream,
    )
    declared = [
        *api.routes(),
        *core.routes(),
        *PolicyApi(core).routes(),
        *OrganisationApi(core).routes(),
        *HistoryApi(core).routes(),
    ]
    api_routes: list[BaseRoute] = [
        Route(f"/{spec.path}", _endpoint(api, spec), methods=[spec.method]) for spec in declared
    ]
    shell = _shell(static_dir)
    routes: list[BaseRoute] = [
        Route("/healthz", _healthz, methods=["GET"]),
        Mount(API_PREFIX, routes=api_routes),
        Route("/admin", shell, methods=["GET"]),
        Route("/admin/{rest:path}", shell, methods=["GET"]),
    ]
    guard = Guard(Starlette(routes=routes), db, origin, proxies)
    guard.api_routes = declared
    return guard
