"""OAuth 2.1 authorization server, one per access point, under `/o/<id>/oauth/p/<path>` (SCR-04,
F-014 §3.1). Routes are read on the raw path by `hullgate.routes` (BR-161); step 1 checks the
identifier and the role under the store.

Routes (spec l. 749-753): RFC 9728 metadata of the access point and RFC 8414 metadata of its
authorization server, `authorize`, `consent` (TOTP authorization barrier), `token`, `revoke`,
`register` (DCR).

Handlers of our own, not those of the SDK's `mcp.server.auth`: v1 must read `resource` in full
(the SDK reads it through `dict()`), refuse a revocation from an unknown client with
`invalid_client` (the SDK answers `unauthorized_client`), admit a public client at `/revoke` (the
SDK requires `client_secret`; v1 admits public clients only), answer 503 during an outage (the
SDK turns a provider exception into a 302 `server_error`) and serve one issuer per access point.
Fixing them one by one would have been glue on the SDK's internal API; no object of the module is
replaced (E-02). The SDK remains the one of the MCP entry. Availability (BR-064): each route
evaluates the vault predicate on every request; an unavailability or a write failure answers 503
`temporarily_unavailable` in JSON, never a redirect nor a 500, and nothing is issued.

F-020. Per limit key (BR-301), before route parsing or any database read: 60 requests a minute on
every route but `token` (BR-306), 20 `register` an hour (BR-307), 429 with `Retry-After`. At
`token`, an unknown client or grant counts under BR-308 and stops writing beyond its budget. The
barrier's window is checked, the code evaluated and a failure counted in one write transaction
(BR-073); a request approved from the host passes without a factor (BR-310). Refusal entries
carry the client address (BR-313).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from anyio import to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import Receive, Scope, Send

from hullgate import access_tokens, audit, limits, routes, tokens
from hullgate import body as request_body
from hullgate import tree as tree_mod
from hullgate.access_tokens import KeyUnavailable, issuer_for, resource_for
from hullgate.admin import accounts, auth, barrier
from hullgate.cimd import CimdError, Fetcher, is_cimd_client_id, valid_redirect_uri
from hullgate.config import AdminOrigin
from hullgate.db import StoreUnavailable, transaction
from hullgate.keys import totp_match
from hullgate.limits import Limits
from hullgate.tree import Clock, Node, Refused, Tree
from hullgate.vault import Vault

log = logging.getLogger("hullgate.oauth")


CODE_TTL = 120  # BR-052
PENDING_TTL = 600  # BR-052, BR-060
LOCK_WINDOW = 15 * 60  # BR-073
LOCK_THRESHOLD = 5
BODY_MAX = 64 * 1024
RS_SCOPES = ["mcp"]
AS_SCOPES = ["mcp", "offline_access"]
AUTH_METHODS = ["none"]  # public clients only (operator decision)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PAGE_CSP = "frame-ancestors 'none'; default-src 'none'; style-src 'unsafe-inline'"
PAGE_HEADERS = {
    **NO_STORE,
    "Content-Security-Policy": f"{PAGE_CSP}; form-action 'self'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# Host expressible as is in a CSP source (CSP3 host-part): no wildcard, no IPv6 literal,
# no character that would change the policy (`;`, `,`, space, quote).
_CSP_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*$")
EXPIRED_REQUEST = (
    "Authorization request unknown, expired or already handled (a request is used only once). "
    "Start the connection again from the client."
)
UNAVAILABLE = {"error": "temporarily_unavailable"}
CONSENT_COOKIE = "hullgate_consent"
PAGE_ROUTES = ("authorize", "consent")  # a limit refusal there is a page (F-020 §3.4)
LOCKED = "locked"  # BR-073: the barrier's window is full
NO_TOTP = "no_totp"  # no TOTP secret configured
LIMITED_PAGE = "Too many requests from your address. Wait, then try again."
A_APPROVED = "consent.approved"
REFERENCE_LEN = 8  # BR-310: hexadecimal digits of the request reference, shown as xxxx-xxxx


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def s256(verifier: str) -> str:
    raw = hashlib.sha256(verifier.encode("ascii", errors="replace")).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def oauth_family(scope: Scope) -> bool:
    """An unknown form of an authorization route (BR-161's 404), counted under BR-306 like a known
    one so that known and unknown paths behave alike (F-020 BR-161 clarification): the raw or the
    decoded path, case folded, names an OAuth segment or a well-known metadata prefix. Decided on
    the path alone, never on stored state."""
    raw = routes.raw_request_path(scope)
    for path in (raw, str(scope.get("path", ""))):
        folded = (path or "").lower()
        if folded.startswith("/.well-known/oauth-") or "/oauth/" in folded:
            return True
    return False


def reference(stored_hash: str) -> str:
    """BR-310: the request reference, a short non-secret value derived from the request's stored
    hash (itself the digest of the cookie's value): `xxxx-xxxx`."""
    short = hashlib.sha256(f"reference:{stored_hash}".encode()).hexdigest()[:REFERENCE_LEN]
    return f"{short[:4]}-{short[4:]}"


def _refusal_detail(detail: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A refusal entry's detail with the client address (BR-313)."""
    merged = {**(detail or {}), **limits.address_detail()}
    return merged or None


# ---------------------------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------------------------


def _json(
    status: int, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
) -> Response:
    return JSONResponse(dict(body), status_code=status, headers={**NO_STORE, **(headers or {})})


def oauth_error(status: int, error: str, description: str = "") -> Response:
    body = {"error": error}
    if description:
        body["error_description"] = description
    extra = {"WWW-Authenticate": 'Basic realm="oauth"'} if status == 401 else None
    return _json(status, body, extra)


def unavailable() -> Response:
    return _json(503, UNAVAILABLE)


def redirect_origin_source(redirect_uri: str) -> str | None:
    """Exact origin of `redirect_uri` as a CSP source (`https://host[:port]`), otherwise None.

    CSP 3 applies `form-action` to the whole navigation born from the submission, redirects
    included: the barrier's 302 redirect to the client must be allowed by the page that carries
    the form (HULL-31). The URI is already validated (BR-061); a host that the CSP grammar cannot
    express unambiguously (IPv6, non-ASCII name) widens nothing.
    """
    try:
        parts = urlsplit(redirect_uri)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.scheme not in ("https", "http") or not _CSP_HOST_RE.match(host):
        return None
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def consent_page_headers(redirect_uri: str) -> dict[str, str]:
    """Headers of the barrier page: `form-action` also admits the client's origin."""
    source = redirect_origin_source(redirect_uri)
    if source is None:
        return dict(PAGE_HEADERS)
    return {**PAGE_HEADERS, "Content-Security-Policy": f"{PAGE_CSP}; form-action 'self' {source}"}


def _page(
    status: int, title: str, body: str, headers: Mapping[str, str] = PAGE_HEADERS
) -> Response:
    text = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px system-ui,sans-serif;max-width:32rem;margin:3rem auto;"
        "padding:0 1rem}dt{font-weight:600}dd{margin:0 0 .6rem}"
        "input{font-size:1.4rem;width:9ch;letter-spacing:.2em}"
        "button{font-size:1rem;margin:.8rem .6rem 0 0;padding:.4rem 1rem}.err{color:#a00}</style>"
        f"<h1>{html.escape(title)}</h1>{body}</html>"
    )
    return HTMLResponse(text, status_code=status, headers=dict(headers))


def error_page(status: int, message: str) -> Response:
    return _page(status, "Authorization impossible", f"<p>{html.escape(message)}</p>")


def _with_query(uri: str, params: Mapping[str, str | None]) -> str:
    parts = urlsplit(uri)
    extra = urlencode([(k, v) for k, v in params.items() if v is not None])
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _redirect(uri: str, params: Mapping[str, str | None]) -> Response:
    return RedirectResponse(_with_query(uri, params), status_code=302, headers=NO_STORE)


# ---------------------------------------------------------------------------------------------
# Admitted clients (BR-061, BR-062)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Client:
    client_id: str
    kind: str  # preregistered | dcr | cimd
    redirect_uris: tuple[str, ...]
    name: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


def stored_client(
    conn: sqlite3.Connection, tree: Tree, role: Node, client_id: str
) -> Client | None:
    """Client registered and admitted by the authorization server of `role`, or None.

    Preregistered: placed on the role or one of its ancestors (the closest wins). DCR: registered
    with this same authorization server. CIMD: without a node (S9), known after a first
    authorization; the binding to the access point goes through the lineage, placed on the role.
    """
    chain = [n.id for n in tree.lineage_of(role)]
    rows = conn.execute(
        "SELECT kind, node_id, metadata FROM client WHERE client_id = ? AND removed_at IS NULL",
        (client_id,),
    ).fetchall()
    admitted = [
        r
        for r in rows
        if r[0] == "cimd"
        or (r[0] == "dcr" and r[1] == role.id)
        or (r[0] == "preregistered" and r[1] in chain)
    ]
    if not admitted:
        return None
    admitted.sort(key=lambda r: chain.index(r[1]) if r[1] in chain else len(chain))
    kind, _node_id, metadata = admitted[0]
    try:
        meta = json.loads(metadata or "{}")
    except ValueError:
        meta = {}
    uris = tuple(u for u in meta.get("redirect_uris", []) if isinstance(u, str))
    return Client(client_id, kind, uris, str(meta.get("client_name") or client_id), meta)


def instance_flag(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM instance_flag WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def dcr_enabled(conn: sqlite3.Connection) -> bool:
    return instance_flag(conn, "dcr") == "on"


# ---------------------------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    """Result of an operation played under the database lock: response and audit entry."""

    response: Response
    audit: dict[str, Any] | None = None


@dataclass
class AuthorizationServers:
    """ASGI application of the OAuth routes of all access points (one instance per listener).

    Each request is served by a copy bound to the stored identifier read in step 1 (`org_id`),
    from which every URL it states is built (BR-041, BR-056 v2)."""

    vault: Vault
    public_url: str
    clock: Clock = time.time
    fetcher: Fetcher = field(default_factory=Fetcher)
    limits: Limits | None = None  # F-020: the listener's limits; None lifts them
    barrier_origin: AdminOrigin | None = None
    org_id: str = ""  # the instance's identifier, set per request after step 1

    def _key(self) -> str:
        return limits.limit_key(limits.CLIENT_ADDRESS.get() or "")

    def _limited(self, surface: str, delay: float, page: bool) -> Response:
        """429 with `Retry-After`, JSON or the generic page (F-020 §3.4); one coalesced entry per
        key and minute (BR-312)."""
        key = self._key()
        if self.limits is not None:
            suppressed = self.limits.coalescer.refused(surface, key)
            if suppressed is not None:
                entry = limits.refusal_entry(surface, key, suppressed, actor=audit.ANONYMOUS)
                self._audit(entry, required=False)
        seconds = str(limits.whole_seconds(delay))
        if page:
            response = error_page(429, LIMITED_PAGE)
        else:
            response = _json(429, {"error": limits.RATE_LIMITED})
        response.headers["Retry-After"] = seconds
        return response

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        route = routes.route_of(scope)
        # BR-306: before BR-161's 404 and before any database read, for a route of any
        # identifier and any unknown form alike (no oracle); `token` outside
        if self.limits is not None and (route is None or route.action != "token"):
            value, _ = limits.OAUTH_BUDGET
            delay = self.limits.oauth.admit(self._key(), value)
            if delay is not None:
                page = route is not None and route.action in PAGE_ROUTES
                limited = await to_thread.run_sync(self._limited, limits.OAUTH, delay, page)
                await limited(scope, receive, send)
                return
        if route is None or route.action == routes.MCP:
            await _json(404, {"error": "not_found"})(scope, receive, send)
            return
        path, action = route.path, route.action
        # step 1 (BR-055, BR-161): the instance's identifier and an existing role, read under the
        # store; an outage answers 503 before any 404 (BR-064); a foreign identifier answers as an
        # unknown role, byte for byte
        try:
            org_id = await to_thread.run_sync(
                self.vault.db.call, lambda c: _instance_role(c, route.org_id, path)
            )
        except StoreUnavailable:
            await unavailable()(scope, receive, send)
            return
        if org_id is None:
            await _json(404, {"error": "not_found"})(scope, receive, send)
            return
        server = replace(self, org_id=org_id)
        state = await to_thread.run_sync(self.vault.readiness)
        if not state.ok:
            await unavailable()(scope, receive, send)
            return
        request = Request(scope, receive)
        method = request.method
        allowed = {
            "resource-metadata": ("GET",),
            "server-metadata": ("GET",),
            "authorize": ("GET", "POST"),
            "consent": ("GET", "POST"),
            "token": ("POST",),
            "revoke": ("POST",),
            "register": ("POST",),
        }[action]
        if method not in allowed:
            await _json(405, {"error": "method_not_allowed"}, {"Allow": ", ".join(allowed)})(
                scope, receive, send
            )
            return
        body = b""
        if method == "POST":
            try:
                read = await request_body.read_body(scope, receive, BODY_MAX)
            except TimeoutError:
                response = await to_thread.run_sync(server._body_timeout, path, action)
                await response(scope, receive, send)
                return
            if read is None:  # The client disconnected; neither answer nor audit.
                return
            if isinstance(read, request_body.TooLarge):
                await oauth_error(413, "invalid_request", "body too large")(scope, receive, send)
                return
            body = read
        handler: Callable[[str, Request, bytes], Response] = {
            "resource-metadata": server.resource_metadata,
            "server-metadata": server.server_metadata,
            "authorize": server.authorize,
            "consent": server.consent,
            "token": server.token,
            "revoke": server.revoke,
            "register": server.register,
        }[action]
        try:
            response = await to_thread.run_sync(handler, path, request, body)
        except (StoreUnavailable, KeyUnavailable) as exc:
            log.error("%s %s: vault unavailable (%s)", action, path, type(exc).__name__)
            response = unavailable()
        await response(scope, receive, send)

    # -- helpers -----------------------------------------------------------------------------

    def _body_timeout(self, path: str, action: str) -> Response:
        """F-024 / BR-316: the body identifies no authenticated actor yet."""
        response = (
            error_page(408, "The request did not arrive in time. Please try again.")
            if action in PAGE_ROUTES
            else oauth_error(408, "invalid_request", "request_timeout")
        )
        response.headers["Connection"] = "close"
        return self._refused(
            response,
            f"oauth.{action}",
            path,
            "request_timeout",
            audit.ANONYMOUS,
            detail={"timeout_seconds": request_body.BODY_READ_TIMEOUT},
        )

    def _call[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return self.vault.db.call(fn)

    def _audit(self, entry: Mapping[str, Any] | None, *, required: bool) -> None:
        """Audit of a refusal: the refusal is served even if the entry fails (BR-066)."""
        if entry is None:
            return

        fields = dict(entry)
        if fields.get("result") == "refused":  # BR-313
            fields["detail"] = _refusal_detail(fields.get("detail"))

        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                audit.write(conn, now=self.clock(), **fields)

        try:
            self._call(write)
        except StoreUnavailable as exc:
            if required:
                raise
            log.error(
                "audit not written (%s, %s): %s", entry.get("action"), entry.get("reason"), exc
            )

    def _refused(
        self,
        response: Response,
        action: str,
        path: str,
        reason: str,
        actor: str,
        node_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> Response:
        self._audit(
            {
                "actor": actor,
                "action": action,
                "result": "refused",
                "node_id": node_id,
                "path": path,
                "reason": reason,
                "detail": dict(detail) if detail else None,
            },
            required=False,
        )
        return response

    def resource_url(self, path: str) -> str:
        return resource_for(self.public_url, self._org(), path)

    def issuer(self, path: str) -> str:
        return issuer_for(self.public_url, self._org(), path)

    def _org(self) -> str:
        if not self.org_id:  # only a copy bound in `__call__` states URLs
            raise RuntimeError("authorization server not bound to the instance's identifier")
        return self.org_id

    # -- metadata (BR-056) --------------------------------------------------------------------

    def resource_metadata(self, path: str, _request: Request, _body: bytes) -> Response:
        return _json(
            200,
            {
                "resource": self.resource_url(path),
                "authorization_servers": [self.issuer(path)],
                "scopes_supported": RS_SCOPES,
                "bearer_methods_supported": ["header"],
                "resource_name": f"hullgate {path}",
            },
            {"Cache-Control": "no-store"},
        )

    def server_metadata(self, path: str, _request: Request, _body: bytes) -> Response:
        issuer = self.issuer(path)
        body: dict[str, Any] = {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "revocation_endpoint": f"{issuer}/revoke",
            "scopes_supported": AS_SCOPES,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": AUTH_METHODS,
            "revocation_endpoint_auth_methods_supported": AUTH_METHODS,
            "code_challenge_methods_supported": ["S256"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        }
        if self._call(dcr_enabled):
            body["registration_endpoint"] = f"{issuer}/register"
        return _json(200, body)

    # -- authorization (BR-057, BR-058, BR-060, BR-061, BR-062) ----------------------------

    def authorize(self, path: str, request: Request, body: bytes) -> Response:
        if request.method == "POST":
            pairs = parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        else:
            pairs = parse_qsl(request.url.query, keep_blank_values=True)
        params: dict[str, list[str]] = {}
        for k, v in pairs:
            params.setdefault(k, []).append(v)

        def one(name: str) -> str | None:
            values = params.get(name, [])
            return values[0] if len(values) == 1 else None

        client_id = one("client_id")
        refuse = "oauth.authorize"
        if not client_id:
            return self._refused(
                error_page(400, "Parameter client_id missing or repeated."),
                refuse,
                path,
                "invalid_request",
                audit.ANONYMOUS,
            )
        tree_role = self._call(lambda c: _tree_and_role(c, path))
        if tree_role is None:
            return error_page(404, "Unknown access point.")
        tree, role = tree_role
        client: Client | None = None
        if is_cimd_client_id(client_id):
            # B1 (review 411): a CIMD client is admitted on each request by its document (fetcher
            # cache of 10 min at most); the `client` table only serves its removal.
            removed = self._call(
                lambda c: c.execute(
                    "SELECT 1 FROM client WHERE client_id = ? AND node_id IS NULL"
                    " AND removed_at IS NOT NULL",
                    (client_id,),
                ).fetchone()
            )
            if removed is not None:
                return self._refused(
                    error_page(400, "Client unknown to this access point."),
                    refuse,
                    path,
                    "unknown_client",
                    client_id,
                    role.id,
                )
            try:
                doc = self.fetcher.document(client_id, self._key())  # BR-309
            except CimdError as exc:
                return self._refused(
                    error_page(400, "Client metadata document unretrievable or invalid."),
                    refuse,
                    path,
                    exc.reason,
                    client_id,
                    role.id,
                )
            client = Client(
                client_id,
                "cimd",
                tuple(doc["redirect_uris"]),
                str(doc.get("client_name") or client_id),
                doc,
            )
        else:
            client = self._call(lambda c: stored_client(c, tree, role, client_id))
        if client is None:
            return self._refused(
                error_page(400, "Client unknown to this access point."),
                refuse,
                path,
                "unknown_client",
                client_id,
                role.id,
            )
        redirect_values = params.get("redirect_uri", [])
        if len(redirect_values) > 1:
            redirect_uri = None
        elif redirect_values:
            redirect_uri = redirect_values[0]
        else:
            redirect_uri = client.redirect_uris[0] if len(client.redirect_uris) == 1 else None
        if redirect_uri is None or redirect_uri not in client.redirect_uris:
            return self._refused(
                error_page(400, "Redirect URI not registered for this client."),
                refuse,
                path,
                "invalid_redirect_uri",
                client_id,
                role.id,
            )
        state = one("state")
        issuer = self.issuer(path)

        def back(error: str, reason: str, description: str) -> Response:
            response = _redirect(
                redirect_uri,
                {"error": error, "error_description": description, "state": state, "iss": issuer},
            )
            return self._refused(response, refuse, path, reason, client_id, role.id)

        if one("response_type") != "code":
            return back("unsupported_response_type", "unsupported_response_type", "code only")
        resources = params.get("resource", [])
        expected = self.resource_url(path)
        if len(resources) != 1:
            return back("invalid_target", "invalid_target", "exactly one resource parameter")
        if resources[0] != expected:
            if not self._call(lambda c: _known_resource(c, self.public_url, resources[0])):
                return self._refused(
                    error_page(400, "Unknown resource."),
                    refuse,
                    path,
                    "invalid_target",
                    client_id,
                    role.id,
                )
            return back("invalid_target", "invalid_target", "resource of another access point")
        challenge, method = one("code_challenge"), one("code_challenge_method")
        if not challenge or method != "S256" or not (43 <= len(challenge) <= 128):
            return back("invalid_request", "pkce_required", "PKCE S256 required")
        scope_text = one("scope") if "scope" in params else "mcp"
        if scope_text is None:
            return back("invalid_scope", "invalid_scope", "repeated scope")
        scopes = scope_text.split()
        if not scopes or any(s not in AS_SCOPES for s in scopes):
            return back("invalid_scope", "invalid_scope", "allowed scopes: mcp, offline_access")
        if (self.vault.totp is None or not self.vault.totp.configured()) and not self._call(
            lambda c: barrier.usable(c, self.barrier_origin)
        ):
            return back("access_denied", "totp_not_configured", "barrier closed")
        request_id = secrets.token_urlsafe(32)  # 256 bits (BR-060: 192 at least)
        now = int(self.clock())

        def store(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute("DELETE FROM consent_request WHERE expires_at <= ?", (now,))
                conn.execute(
                    "INSERT INTO consent_request (hash, role_id, client_id, client_kind,"
                    " client_metadata, redirect_uri, code_challenge, resource, scope, state,"
                    " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        digest(request_id),
                        role.id,
                        client_id,
                        client.kind,
                        json.dumps(dict(client.metadata), ensure_ascii=False, sort_keys=True),
                        redirect_uri,
                        challenge,
                        expected,
                        " ".join(scopes),
                        state,
                        now,
                        now + PENDING_TTL,
                    ),
                )

        self._call(store)
        # m1 (review 411): the request reference travels in a cookie limited to the barrier
        # page, never in the URL (which the access log writes).
        response = RedirectResponse(f"{issuer}/consent", status_code=302, headers=NO_STORE)
        response.set_cookie(
            CONSENT_COOKIE,
            request_id,
            max_age=PENDING_TTL,
            path=routes.consent_cookie_path(self._org(), path),
            secure=self.public_url.startswith("https://"),
            httponly=True,
            samesite="lax",
        )
        return response

    # -- barrier (BR-060, BR-073) ------------------------------------------------------------

    def consent(self, path: str, request: Request, body: bytes) -> Response:
        form = dict(parse_qsl(body.decode("utf-8", errors="replace"))) if body else {}
        cookie = request.cookies.get(CONSENT_COOKIE, "")
        request_id = cookie
        if request.method == "POST":
            # m5 (review 413): double submit — the hidden field `req` must be present and equal
            # to the cookie; the protection does not rest on `SameSite` alone.
            field_value = form.get("req", "")
            if (
                not field_value
                or not cookie
                or not hmac.compare_digest(field_value.encode(), cookie.encode())
            ):
                return error_page(400, EXPIRED_REQUEST)
        now = int(self.clock())
        pending = self._call(lambda c: _pending(c, path, request_id, now))
        if pending is None:
            return error_page(400, EXPIRED_REQUEST)
        tree, role, req = pending
        if request.method == "GET":
            return self._consent_page(path, tree, role, req, request_id, 200, "")
        decision = form.get("decision", "")
        issuer = self.issuer(path)
        client_id = str(req["client_id"])
        if decision == "deny":  # the operator's refusal consumes the request (operator decision)
            self._call(lambda c: _consume_pending(c, request_id))
            response = _redirect(
                str(req["redirect_uri"]),
                {"error": "access_denied", "state": req["state"], "iss": issuer},
            )
            return self._refused(
                response, "oauth.consent", path, "consent_denied", client_id, role.id
            )
        if decision in ("passkey-options", "passkey"):
            if (
                self.barrier_origin is None
                or request.headers.get("origin") != self.barrier_origin.origin
                or not self._call(lambda c: barrier.usable(c, self.barrier_origin))
            ):
                return oauth_error(403, "access_denied", "Passkey refused. Try again.")
            if decision == "passkey-options":
                return self._barrier_options(path, request_id, now)
        if decision not in ("allow", "passkey"):
            return self._consent_page(path, tree, role, req, request_id, 400, "Invalid choice.")
        secret = self.vault.totp.secret() if self.vault.totp is not None else None
        given = form.get("totp", "").strip()
        code = secrets.token_urlsafe(32)
        # BR-073: window checked, code evaluated and failure counted in one write transaction;
        # an approval from the host (BR-310) is checked before the window
        credential = None
        if decision == "passkey":
            try:
                credential = json.loads(form.get("credential", ""))
            except (ValueError, RecursionError):
                credential = {}
            if not isinstance(credential, dict):
                credential = {}
        outcome = self._call(
            lambda c: self._approve(
                c, path, request_id, secret, given, code, now, credential=credential
            )
        )
        if outcome == "passkey_limited":
            return self._consent_page(
                path, tree, role, req, request_id, 429, "Too many recent attempts. Try again later."
            )
        if outcome == LOCKED and self._call(lambda c: barrier.usable(c, self.barrier_origin)):
            response = self._consent_page(
                path,
                tree,
                role,
                req,
                request_id,
                429,
                "Too many recent TOTP attempts. Use a passkey or try again later.",
            )
            return self._refused(
                response, "oauth.consent", path, "consent_locked", client_id, role.id
            )
        if outcome == LOCKED:
            ref = html.escape(reference(digest(request_id)))
            response = _page(
                429,
                "Barrier closed",
                "<p>Too many recent attempts. Try again later.</p>"
                f"<p>Request reference: <code>{ref}</code></p>",
            )
            return self._refused(
                response, "oauth.consent", path, "consent_locked", client_id, role.id
            )
        if outcome == NO_TOTP:
            response = _redirect(
                str(req["redirect_uri"]),
                {"error": "access_denied", "state": req["state"], "iss": issuer},
            )
            return self._refused(
                response, "oauth.consent", path, "totp_not_configured", client_id, role.id
            )
        if outcome is None:  # failure: wrong or already used code (counted, audited in its tx)
            return self._consent_page(
                path,
                tree,
                role,
                req,
                request_id,
                200,
                "Passkey refused. Try again." if credential is not None else "Code refused.",
            )
        return _redirect(
            str(req["redirect_uri"]), {"code": code, "state": req["state"], "iss": issuer}
        )

    def _barrier_options(self, path: str, request_id: str, now: int) -> Response:
        address = limits.CLIENT_ADDRESS.get() or ""

        def create(conn: sqlite3.Connection) -> Response:
            with transaction(conn):
                accounts.purge(conn, now)
                if _pending(conn, path, request_id, now) is None:
                    return oauth_error(400, "invalid_request", EXPIRED_REQUEST)
                if accounts.limited(conn, self._key(), now):
                    barrier.failed(conn, address, now, "passkey_limited", limited=True)
                    return oauth_error(
                        429, "access_denied", "Too many recent attempts. Try again later."
                    )
                try:
                    value = auth.new_challenge(
                        conn, "barrier", self._key(), now, enrolment_digest=digest(request_id)
                    )
                except auth.TooManyChallenges:
                    barrier.failed(conn, address, now, "passkey_limited", limited=True)
                    return oauth_error(
                        429, "access_denied", "Too many recent attempts. Try again later."
                    )
                if self.barrier_origin is None:
                    return oauth_error(403, "access_denied")
                return _json(
                    200, {"publicKey": auth.authentication_options(self.barrier_origin, value)}
                )

        return self._call(create)

    def _consent_page(
        self,
        path: str,
        tree: Tree,
        role: Node,
        req: Mapping[str, Any],
        request_id: str,
        status: int,
        message: str,
    ) -> Response:
        name = html.escape(str(req["client_name"]))
        client_id = html.escape(str(req["client_id"]))
        sensitive = "yes" if tree.is_sensitive(role) else "no"
        err = f"<p class=err role=alert>{html.escape(message)}</p>" if message else ""
        action = html.escape(f"{self.issuer(path)}/consent")
        ref = html.escape(reference(digest(request_id)))
        body = (
            "<p>A client requests access to this MCP access point.</p>"
            f"<dl><dt>Client</dt><dd>{name}<br><small>{client_id}</small></dd>"
            f"<dt>Role</dt><dd>{html.escape(path)}</dd>"
            f"<dt>Sensitive environment</dt><dd>{sensitive}</dd>"
            f"<dt>Request reference</dt><dd><code>{ref}</code></dd></dl>{err}"
            f"<form method=post action='{action}'>"
            f"<input type=hidden name=req value='{html.escape(request_id)}'>"
            "<label>Code from the authenticator app<br>"
            "<input name=totp inputmode=numeric autocomplete=one-time-code maxlength=6 "
            "pattern='[0-9]{6}' autofocus></label><br>"
            "<button name=decision value=allow>Authorize</button>"
            "<button name=decision value=deny formnovalidate>Deny</button></form>"
        )
        headers = consent_page_headers(str(req["redirect_uri"]))
        if self._call(lambda c: barrier.usable(c, self.barrier_origin)):
            control = (
                "<input type=hidden name=credential>"
                "<button type=button id=passkey>Authorize with a passkey</button>"
                "<p id=passkey-error role=alert></p>"
            )
            body = (
                body.replace("</form>", control + "</form>")
                + "<script>"
                + barrier.SCRIPT
                + "</script>"
            )
            script_hash = base64.b64encode(
                hashlib.sha256(barrier.SCRIPT.encode()).digest()
            ).decode()
            headers["Content-Security-Policy"] += (
                f"; script-src 'sha256-{script_hash}'; connect-src 'self'"
            )
        return _page(status, "Authorize access", body, headers)

    def _approve(
        self,
        conn: sqlite3.Connection,
        path: str,
        request_id: str,
        secret: bytes | None,
        given: str,
        code: str,
        now: int,
        *,
        credential: dict[str, Any] | None = None,
    ) -> bool | str | None:
        """Success: request consumed, TOTP step marked, lineage and code issued, one transaction.

        BR-073: the window's check (purging rows past it), the code's evaluation and a failure's
        count run in this one write transaction, so parallel entries cannot pass the check
        together. BR-310: an approved request passes first, with no factor, closed or not.
        Returns `LOCKED`, `NO_TOTP`, None (a failure, counted and audited) or True."""
        with transaction(conn):
            tree = Tree.load(conn)
            row = conn.execute(
                "SELECT role_id, client_id, client_kind, client_metadata, redirect_uri,"
                " code_challenge, resource, scope, approved_at FROM consent_request"
                " WHERE hash = ? AND expires_at > ?",
                (digest(request_id), now),
            ).fetchone()
            role = tree.find(path)
            approved = row is not None and row[8] is not None
            counter: int | None = None
            used = False
            passkey_admin = None
            if credential is not None:
                address = limits.CLIENT_ADDRESS.get() or ""
                accounts.purge(conn, now)
                if accounts.limited(conn, self._key(), now):
                    barrier.failed(conn, address, now, "passkey_limited", limited=True)
                    return "passkey_limited"
                try:
                    if (
                        self.barrier_origin is None
                        or row is None
                        or role is None
                        or row[0] != role.id
                    ):
                        raise auth.CeremonyFailed("unknown_request")
                    passkey_admin = barrier.verify(
                        conn, credential, self.barrier_origin, digest(request_id), self._key(), now
                    )
                except auth.CeremonyFailed as exc:
                    barrier.failed(conn, address, now, exc.reason)
                    return None
            if not approved and passkey_admin is None:
                conn.execute("DELETE FROM consent_failure WHERE at <= ?", (now - LOCK_WINDOW,))
                if _recent_failures(conn, now) >= LOCK_THRESHOLD:
                    return LOCKED
                if secret is None:
                    return NO_TOTP
                counter = totp_match(secret, given, now)
                # S17: a code is refused if its step is lower than or equal to the last accepted.
                last = conn.execute("SELECT max(counter) FROM totp_used").fetchone()[0]
                used = counter is not None and last is not None and counter <= int(last)
            if (
                row is None
                or role is None
                or row[0] != role.id
                or (not approved and passkey_admin is None and (counter is None or used))
            ):
                conn.execute("INSERT INTO consent_failure (at) VALUES (?)", (now,))
                audit.write(
                    conn,
                    actor=row[1] if row else audit.ANONYMOUS,
                    action="oauth.consent",
                    result="refused",
                    node_id=role.id if role else None,
                    path=path,
                    reason="consent_failed",
                    detail=_refusal_detail(None),
                    now=now,
                )
                return None
            _, client_id, kind, metadata, redirect_uri, challenge, resource, scope, _ = row
            if counter is not None:
                conn.execute(
                    "INSERT INTO totp_used (counter, used_at) VALUES (?, ?)", (counter, now)
                )
                conn.execute("DELETE FROM totp_used WHERE counter < ?", (counter,))
            conn.execute("DELETE FROM consent_request WHERE hash = ?", (digest(request_id),))
            if kind == "cimd":
                conn.execute(
                    "INSERT INTO client (client_id, kind, node_id, metadata, created_at)"
                    " VALUES (?, 'cimd', NULL, ?, ?) ON CONFLICT(client_id) WHERE node_id IS NULL"
                    " DO UPDATE SET metadata = excluded.metadata",
                    (client_id, metadata, now),
                )
            seconds, _capped = tokens.ttl_for(tree, path, None)
            lineage_id = "lin_" + secrets.token_hex(8)
            conn.execute(
                "INSERT INTO lineage (id, role_id, client_id, resource, scope, created_at,"
                " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lineage_id, role.id, client_id, resource, scope, now, now + seconds),
            )
            conn.execute(
                "INSERT INTO auth_code (hash, lineage_id, expires_at, redirect_uri,"
                " code_challenge, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (digest(code), lineage_id, now + CODE_TTL, redirect_uri, challenge, now),
            )
            audit.write(
                conn,
                actor=client_id,
                action="oauth.code.issue",
                result="ok",
                node_id=role.id,
                path=path,
                after={
                    "lineage_id": lineage_id,
                    "scope": scope,
                    "expires_at": audit.utc(now + seconds),
                },
                detail={
                    "client_kind": kind,
                    "factor": "passkey"
                    if passkey_admin
                    else "host_approval"
                    if approved
                    else "totp",
                    **({"administrator": passkey_admin} if passkey_admin else {}),
                },
                now=now,
            )
            return True

    # -- tokens (BR-052, BR-054, BR-056, BR-058, BR-059, BR-065) -------------------------------

    def _client_auth(
        self,
        conn: sqlite3.Connection,
        tree: Tree,
        role: Node,
        request: Request,
        form: Mapping[str, list[str]],
    ) -> tuple[Client | None, str]:
        """(admitted client, announced identifier); client None: `invalid_client`.

        Public clients only (operator decision): the identifier comes from the form, or from the
        user of a `Basic` header; a presented `client_secret` is ignored.
        """
        client_id: str | None = None
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None, audit.ANONYMOUS
            from urllib.parse import unquote

            client_id = unquote(raw.partition(":")[0])
        else:
            ids = form.get("client_id", [])
            client_id = ids[0] if len(ids) == 1 else None
        if not client_id:
            return None, audit.ANONYMOUS
        client = stored_client(conn, tree, role, client_id)
        if client is None:
            return None, client_id
        return client, client_id

    def token(self, path: str, request: Request, body: bytes) -> Response:
        form = _form(body)
        now = int(self.clock())
        refuse = "oauth.token"
        result = self._call(lambda c: self._token_locked(c, path, request, form, now))
        if isinstance(result, _TokenRefusal):
            response = oauth_error(result.status, result.error, result.description)
            if result.audited:
                return response
            if result.unknown and not self._unknown_written(path, result):
                return response  # BR-308: same answer, no entry of its own
            return self._refused(
                response, refuse, path, result.reason, result.actor, result.node_id
            )
        return _json(200, result)

    def _unknown_written(self, path: str, refusal: _TokenRefusal) -> bool:
        """BR-308: counts an unknown client or grant against its key; False beyond 30 in 60 s,
        when only BR-312's coalesced entry may be written."""
        if self.limits is None:
            return True
        key = self._key()
        if self.limits.unknown_credential(key):
            return True
        suppressed = self.limits.coalescer.refused(limits.CREDENTIAL, key)
        if suppressed is not None:
            entry = limits.refusal_entry(
                limits.CREDENTIAL, key, suppressed, actor=refusal.actor,
                node_id=refusal.node_id, path=path,
            )  # fmt: skip
            self._audit(entry, required=False)
        return False

    def _token_locked(
        self,
        conn: sqlite3.Connection,
        path: str,
        request: Request,
        form: Mapping[str, list[str]],
        now: int,
    ) -> dict[str, Any] | _TokenRefusal:
        """m2 (review 411): reading of the code or the refresh token, decision and write under one
        write lock: two concurrent exchanges can no longer both see `used = 0`; the second is a
        reuse (`reuse_detected`), never a 503 outside an outage."""
        with transaction(conn):
            result = self._token(conn, path, request, form, now)
            if isinstance(result, _TokenRefusal) and result.unknown:
                grants = form.get("grant_type", [])
                if len(grants) == 1 and grants[0] in ("authorization_code", "refresh_token"):
                    # BR-308 v3: classify the presented values, independently of the answer.
                    # This read neither admits a malformed form nor consumes a stored grant.
                    result.unknown = _presented_grant_unknown(conn, grants[0], form)
            return result

    def _token(
        self,
        conn: sqlite3.Connection,
        path: str,
        request: Request,
        form: Mapping[str, list[str]],
        now: int,
    ) -> dict[str, Any] | _TokenRefusal:
        tree = Tree.load(conn)
        role = tree.find(path)
        if role is None or role.level != "role":
            return _TokenRefusal(400, "invalid_grant", "access point deleted", "node_deleted")
        client, announced = self._client_auth(conn, tree, role, request, form)
        if client is None:
            return _TokenRefusal(
                401,
                "invalid_client",
                "unknown or unauthenticated client",
                "invalid_client",
                announced,
                role.id,
                unknown=True,
            )
        grants = form.get("grant_type", [])
        grant = grants[0] if len(grants) == 1 else ""
        if grant not in ("authorization_code", "refresh_token"):
            return _TokenRefusal(
                400,
                "unsupported_grant_type",
                "",
                "unsupported_grant_type",
                client.client_id,
                role.id,
                unknown=True,
            )
        # BR-058: a single resource, that of this access point, checked before any write
        resources = form.get("resource", [])
        if len(resources) != 1 or resources[0] != self.resource_url(path):
            return _TokenRefusal(
                400,
                "invalid_target",
                "exactly one resource: this access point",
                "invalid_target",
                client.client_id,
                role.id,
                unknown=True,  # stored grants are classified by _token_locked
            )
        if grant == "authorization_code":
            return self._exchange_code(conn, tree, role, path, client, form, now)
        return self._refresh(conn, tree, role, path, client, form, now)

    def _exchange_code(
        self,
        conn: sqlite3.Connection,
        tree: Tree,
        role: Node,
        path: str,
        client: Client,
        form: Mapping[str, list[str]],
        now: int,
    ) -> dict[str, Any] | _TokenRefusal:
        codes = form.get("code", [])
        code = codes[0] if len(codes) == 1 else ""
        row = conn.execute(
            "SELECT a.lineage_id, a.expires_at, a.used, a.redirect_uri, a.code_challenge,"
            " l.role_id, l.client_id, l.resource, l.scope, l.revoked, l.expires_at"
            " FROM auth_code a JOIN lineage l ON l.id = a.lineage_id WHERE a.hash = ?",
            (digest(code),),
        ).fetchone()
        cid = client.client_id

        def refusal(description: str, reason: str) -> _TokenRefusal:
            return _TokenRefusal(400, "invalid_grant", description, reason, cid, role.id)

        if not code or row is None:
            unknown = refusal("unknown code", "invalid_grant")
            unknown.unknown = True
            return unknown
        (
            lineage_id,
            code_exp,
            used,
            redirect_uri,
            challenge,
            role_id,
            owner,
            resource,
            scope,
            revoked,
            lineage_exp,
        ) = row
        if role_id != role.id:  # BR-056: code of another authorization server
            return refusal("code of another access point", "foreign_code")
        if owner != cid:
            return refusal("code of another client", "client_mismatch")
        if revoked:
            return refusal("lineage revoked", "revoked")
        if resource != self.resource_url(path):
            return _TokenRefusal(
                400,
                "invalid_target",
                "different resource for the code",
                "invalid_target",
                cid,
                role.id,
            )
        if used:  # BR-059: reuse of a code, the whole lineage falls
            with _held(conn):
                _revoke_lineage(conn, tree, lineage_id, role.id, path, cid, "reuse_detected", now)
                audit.write(
                    conn,
                    actor=cid,
                    action=refuse_action("token"),
                    result="refused",
                    node_id=role.id,
                    path=path,
                    reason="reuse_detected",
                    detail=_refusal_detail(
                        {"lineage_id": lineage_id, "grant": "authorization_code"}
                    ),
                    now=now,
                )
            return _TokenRefusal(
                400,
                "invalid_grant",
                "code already used",
                "reuse_detected",
                cid,
                role.id,
                audited=True,
            )
        if code_exp <= now or revoked or lineage_exp <= now:
            return refusal("code expired or revoked", "invalid_grant")
        redirects = form.get("redirect_uri", [])
        if len(redirects) != 1 or redirects[0] != redirect_uri:
            return refusal("redirect_uri differs from the authorization", "redirect_mismatch")
        verifiers = form.get("code_verifier", [])
        verifier = verifiers[0] if len(verifiers) == 1 else ""
        if not (43 <= len(verifier) <= 128) or not hmac.compare_digest(
            s256(verifier), challenge or ""
        ):
            return refusal("wrong PKCE verifier", "pkce_mismatch")
        with _held(conn):
            cur = conn.execute(
                "UPDATE auth_code SET used = 1 WHERE hash = ? AND used = 0", (digest(code),)
            )
            if cur.rowcount != 1:  # impossible under the lock: defensive, never served
                raise StoreUnavailable("code consumed outside the lock")
            return self._issue(
                conn, role, path, cid, scope, lineage_id, lineage_exp, "authorization_code", now
            )

    def _refresh(
        self,
        conn: sqlite3.Connection,
        tree: Tree,
        role: Node,
        path: str,
        client: Client,
        form: Mapping[str, list[str]],
        now: int,
    ) -> dict[str, Any] | _TokenRefusal:
        values = form.get("refresh_token", [])
        value = values[0] if len(values) == 1 else ""
        row = conn.execute(
            "SELECT r.used, l.id, l.role_id, l.client_id, l.resource, l.scope, l.revoked,"
            " l.expires_at FROM refresh r JOIN lineage l ON l.id = r.lineage_id WHERE r.hash = ?",
            (digest(value),),
        ).fetchone()
        cid = client.client_id

        def refusal(description: str, reason: str) -> _TokenRefusal:
            return _TokenRefusal(400, "invalid_grant", description, reason, cid, role.id)

        if not value or row is None:
            unknown = refusal("unknown refresh token", "invalid_grant")
            unknown.unknown = True
            return unknown
        used, lineage_id, role_id, owner, resource, scope, revoked, lineage_exp = row
        if role_id != role.id:
            return refusal("refresh token of another access point", "foreign_refresh")
        if owner != cid:
            return refusal("refresh token of another client", "client_mismatch")
        if revoked:  # lineage revoked (revocation, deletion, path change, reuse)
            return refusal("lineage revoked", "revoked")
        if resource != self.resource_url(path):
            return _TokenRefusal(
                400,
                "invalid_target",
                "different resource for the lineage",
                "invalid_target",
                cid,
                role.id,
            )
        if used:  # BR-059: reuse, the whole lineage falls
            with _held(conn):
                _revoke_lineage(conn, tree, lineage_id, role.id, path, cid, "reuse_detected", now)
                audit.write(
                    conn,
                    actor=cid,
                    action=refuse_action("token"),
                    result="refused",
                    node_id=role.id,
                    path=path,
                    reason="reuse_detected",
                    detail=_refusal_detail({"lineage_id": lineage_id, "grant": "refresh_token"}),
                    now=now,
                )
            return _TokenRefusal(
                400,
                "invalid_grant",
                "refresh token already used",
                "reuse_detected",
                cid,
                role.id,
                audited=True,
            )
        if lineage_exp <= now:  # BR-052: rotation does not extend the lineage
            return refusal("lineage expired", "expired")
        scopes = form.get("scope", [])
        granted = scope
        if scopes:
            asked = scopes[0].split() if len(scopes) == 1 else []
            if not asked or any(s not in scope.split() for s in asked):
                return _TokenRefusal(
                    400, "invalid_scope", "scope widened", "invalid_scope", cid, role.id
                )
            granted = " ".join(asked)
        with _held(conn):
            cur = conn.execute(
                "UPDATE refresh SET used = 1 WHERE hash = ? AND used = 0", (digest(value),)
            )
            if cur.rowcount != 1:  # impossible under the lock: defensive, never served
                raise StoreUnavailable("refresh token consumed outside the lock")
            # rotation: the previous access tokens of the lineage fall (BR-059)
            conn.execute(
                "UPDATE access_jti SET revoked = 1 WHERE lineage_id = ? AND revoked = 0",
                (lineage_id,),
            )
            return self._issue(
                conn, role, path, cid, granted, lineage_id, lineage_exp, "refresh_token", now
            )

    def _issue(
        self,
        conn: sqlite3.Connection,
        role: Node,
        path: str,
        client_id: str,
        scope: str,
        lineage_id: str,
        lineage_exp: int,
        grant: str,
        now: int,
    ) -> dict[str, Any]:
        """Access token and refresh token, recorded with their audit (transaction in progress)."""
        access, jti = access_tokens.issue(
            conn,
            self.vault.key or _missing_key(),
            issuer=self.issuer(path),
            audience=self.resource_url(path),
            role_id=role.id,
            client_id=client_id,
            scope=scope,
            lineage_id=lineage_id,
            now=now,
        )
        refresh = secrets.token_urlsafe(48)
        conn.execute(
            "INSERT INTO refresh (hash, lineage_id, created_at) VALUES (?, ?, ?)",
            (digest(refresh), lineage_id, now),
        )
        audit.write(
            conn,
            actor=client_id,
            action="oauth.token.issue",
            result="ok",
            node_id=role.id,
            path=path,
            after={
                "lineage_id": lineage_id,
                "jti": jti,
                "expires_at": audit.utc(now + access_tokens.ACCESS_TTL),
            },
            detail={"grant": grant, "scope": scope},
            now=now,
        )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": access_tokens.ACCESS_TTL,
            "scope": scope,
            "refresh_token": refresh,
            "refresh_token_expires_in": max(lineage_exp - now, 0),
        }

    # -- revocation (BR-063) -----------------------------------------------------------------

    def revoke(self, path: str, request: Request, body: bytes) -> Response:
        form = _form(body)
        now = int(self.clock())
        values = form.get("token", [])
        if len(values) != 1 or not values[0]:  # N13
            return self._refused(
                oauth_error(400, "invalid_request", "token parameter required"),
                "oauth.revoke",
                path,
                "invalid_request",
                audit.ANONYMOUS,
            )
        token = values[0]
        result = self._call(lambda c: self._revoke(c, path, request, form, token, now))
        if isinstance(result, _TokenRefusal):
            return self._refused(
                oauth_error(result.status, result.error, result.description),
                "oauth.revoke",
                path,
                result.reason,
                result.actor,
                result.node_id,
            )
        return Response(status_code=200, headers=NO_STORE)

    def _revoke(
        self,
        conn: sqlite3.Connection,
        path: str,
        request: Request,
        form: Mapping[str, list[str]],
        token: str,
        now: int,
    ) -> _TokenRefusal | None:
        tree = Tree.load(conn)
        role = tree.find(path)
        if role is None:
            return _TokenRefusal(401, "invalid_client", "", "node_deleted")
        client, announced = self._client_auth(conn, tree, role, request, form)
        if client is None:
            return _TokenRefusal(
                401,
                "invalid_client",
                "unknown or unauthenticated client",
                "invalid_client",
                announced,
                role.id,
            )
        cid = client.client_id
        row = conn.execute(
            "SELECT l.id, l.role_id, l.client_id, l.revoked FROM refresh r"
            " JOIN lineage l ON l.id = r.lineage_id WHERE r.hash = ?",
            (digest(token),),
        ).fetchone()
        if row is not None:
            lineage_id, role_id, owner, revoked = row
            if role_id == role.id and owner == cid and not revoked:
                with transaction(conn):
                    _revoke_lineage(
                        conn, tree, lineage_id, role.id, path, cid, "revoked_by_client", now
                    )
            return None
        if self.vault.key is None:
            return None
        claims = access_tokens.verify(self.vault.key, token, self.public_url, now)
        if claims is None:
            return None  # unknown or foreign token: 200 without effect (RFC 7009 §2.2)
        row = conn.execute(
            "SELECT a.revoked, l.role_id, l.client_id FROM access_jti a"
            " JOIN lineage l ON l.id = a.lineage_id WHERE a.jti = ?",
            (claims["jti"],),
        ).fetchone()
        if row is None:
            return None
        jti_revoked, role_id, owner = row
        if role_id == role.id and owner == cid and not jti_revoked:
            with transaction(conn):
                conn.execute("UPDATE access_jti SET revoked = 1 WHERE jti = ?", (claims["jti"],))
                audit.write(
                    conn,
                    actor=cid,
                    action="oauth.access.revoke",
                    result="ok",
                    node_id=role.id,
                    path=path,
                    reason="revoked_by_client",
                    after={"jti": claims["jti"]},
                    now=now,
                )
        return None

    # -- dynamic registration (BR-061, RFC 7591) ----------------------------------------------

    def register(self, path: str, _request: Request, body: bytes) -> Response:
        now = int(self.clock())
        if not self._call(dcr_enabled):
            return _json(404, {"error": "not_found"})
        if self.limits is not None:  # BR-307: on top of BR-306, before any client is written
            value, _ = limits.REGISTER_BUDGET
            delay = self.limits.register.admit(self._key(), value)
            if delay is not None:
                return self._limited(limits.REGISTER, delay, page=False)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return self._refused(
                oauth_error(400, "invalid_client_metadata", "JSON object expected"),
                "oauth.register",
                path,
                "invalid_client_metadata",
                audit.ANONYMOUS,
            )
        uris = data.get("redirect_uris")
        if (
            not isinstance(uris, list)
            or not uris
            or not all(isinstance(u, str) and valid_redirect_uri(u) for u in uris)
        ):
            return self._refused(
                oauth_error(400, "invalid_redirect_uri", "https, or http to localhost"),
                "oauth.register",
                path,
                "invalid_redirect_uri",
                audit.ANONYMOUS,
            )
        # Public clients only: no secret is ever issued, the requested method is brought back
        # to `none` (operator decision).
        method = "none"
        grants = data.get("grant_types", ["authorization_code", "refresh_token"])
        scope = data.get("scope", "mcp")
        name = data.get("client_name")
        if (
            not isinstance(grants, list)
            or any(g not in ("authorization_code", "refresh_token") for g in grants)
            or not isinstance(scope, str)
            or any(s not in AS_SCOPES for s in scope.split())
            or (name is not None and (not isinstance(name, str) or len(name) > 200))
        ):
            return self._refused(
                oauth_error(400, "invalid_client_metadata", "metadata refused"),
                "oauth.register",
                path,
                "invalid_client_metadata",
                audit.ANONYMOUS,
            )
        client_id = "dcr_" + secrets.token_hex(12)
        meta: dict[str, Any] = {
            "redirect_uris": uris,
            "token_endpoint_auth_method": method,
            "grant_types": grants,
            "response_types": ["code"],
            "scope": scope,
        }
        if name:
            meta["client_name"] = name

        def store(conn: sqlite3.Connection) -> Node:
            with transaction(conn):
                role = Tree.load(conn).find(path)
                if role is None:
                    raise StoreUnavailable("access point gone")
                conn.execute(
                    "INSERT INTO client (client_id, kind, node_id, metadata, created_at)"
                    " VALUES (?, 'dcr', ?, ?, ?)",
                    (client_id, role.id, json.dumps(meta, ensure_ascii=False, sort_keys=True), now),
                )
                audit.write(
                    conn,
                    actor=audit.ANONYMOUS,  # the registrant is not authenticated
                    action="oauth.client.register",
                    result="ok",
                    node_id=role.id,
                    path=path,
                    after={"client_id": client_id, "token_endpoint_auth_method": method},
                    now=now,
                )
                return role

        self._call(store)
        out: dict[str, Any] = {**meta, "client_id": client_id, "client_id_issued_at": now}
        return _json(201, out)


@dataclass
class _TokenRefusal:
    status: int
    error: str
    description: str
    reason: str
    actor: str = audit.ANONYMOUS
    node_id: str | None = None
    audited: bool = False
    unknown: bool = False  # BR-308's closed list: unknown client, or no stored code or refresh


def _presented_grant_unknown(
    conn: sqlite3.Connection, grant: str, form: Mapping[str, list[str]]
) -> bool:
    """BR-308 v3: True unless a presented grant matches a stored row.

    Even a malformed multi-value form can present an authentic credential; its refusal must
    stay visible after the unknown-credential budget is spent.
    """
    field, table = (
        ("code", "auth_code") if grant == "authorization_code" else ("refresh_token", "refresh")
    )
    hashes = [digest(value) for value in form.get(field, []) if value]
    # The 64 KiB form bounds the input. One bound JSON array avoids both a query per value
    # and SQLite's variable limit; the IN lookup uses the stored hash index.
    query = f"SELECT 1 FROM {table} WHERE hash IN (SELECT value FROM json_each(?)) LIMIT 1"  # noqa: S608 — table from a closed pair
    return conn.execute(query, (json.dumps(hashes),)).fetchone() is None


def refuse_action(kind: str) -> str:
    return f"oauth.{kind}"


def _missing_key() -> Any:
    raise KeyUnavailable("signing key not configured")


def _form(body: bytes) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True):
        out.setdefault(k, []).append(v)
    return out


def _find_role(conn: sqlite3.Connection, path: str) -> Node | None:
    node = Tree.load(conn).find(path)
    return node if node is not None and node.level == "role" else None


def _tree_and_role(conn: sqlite3.Connection, path: str) -> tuple[Tree, Node] | None:
    tree = Tree.load(conn)
    node = tree.find(path)
    return (tree, node) if node is not None and node.level == "role" else None


def _instance_role(conn: sqlite3.Connection, org_id: str, path: str) -> str | None:
    """Step 1 (BR-055, BR-161): the stored identifier when `org_id` is it and `path` names a
    live role; None otherwise."""
    stored = tree_mod.organisation_id(conn)
    if not stored or org_id != stored or _find_role(conn, path) is None:
        return None
    return stored


def _known_resource(conn: sqlite3.Connection, public_url: str, resource: str) -> bool:
    point = routes.parse_resource(public_url, resource)
    if point is None or point[0] != tree_mod.organisation_id(conn):
        return False
    return _find_role(conn, point[1]) is not None


def _pending(
    conn: sqlite3.Connection, path: str, request_id: str, now: int
) -> tuple[Tree, Node, dict[str, Any]] | None:
    if not request_id or len(request_id) > 200:
        return None
    tree = Tree.load(conn)
    role = tree.find(path)
    row = conn.execute(
        "SELECT role_id, client_id, client_metadata, redirect_uri, state FROM consent_request"
        " WHERE hash = ? AND expires_at > ?",
        (digest(request_id), now),
    ).fetchone()
    if row is None or role is None or row[0] != role.id:
        return None
    try:
        meta = json.loads(row[2] or "{}")
    except ValueError:
        meta = {}
    name = meta.get("client_name") if isinstance(meta.get("client_name"), str) else None
    stored = conn.execute(
        "SELECT metadata FROM client WHERE client_id = ? LIMIT 1", (row[1],)
    ).fetchone()
    if name is None and stored is not None:
        try:
            name = json.loads(stored[0] or "{}").get("client_name")
        except ValueError:
            name = None
    return (
        tree,
        role,
        {
            "client_id": row[1],
            "client_name": name or row[1],
            "redirect_uri": row[3],
            "state": row[4],
        },
    )


@contextlib.contextmanager
def _held(_conn: sqlite3.Connection) -> Iterator[None]:
    """The write transaction is already opened by `_token_locked` (m2)."""
    yield


def _consume_pending(conn: sqlite3.Connection, request_id: str) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM consent_request WHERE hash = ?", (digest(request_id),))


def _recent_failures(conn: sqlite3.Connection, now: int) -> int:
    row = conn.execute(
        "SELECT count(*) FROM consent_failure WHERE at > ?", (now - LOCK_WINDOW,)
    ).fetchone()
    return int(row[0])


@dataclass(frozen=True, slots=True)
class Pending:
    """A pending authorisation request, as `hullgate consent list` shows it (BR-310)."""

    reference: str
    role: str
    client_id: str
    redirect_host: str
    expires_at: int
    approved: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "role": self.role,
            "client_id": self.client_id,
            "redirect_host": self.redirect_host,
            "expires_at": audit.utc(self.expires_at),
            "approved": self.approved,
        }


def _pending_rows(conn: sqlite3.Connection, now: int) -> list[tuple[Pending, str]]:
    tree = Tree.load(conn)
    rows = conn.execute(
        "SELECT hash, role_id, client_id, redirect_uri, expires_at, approved_at"
        " FROM consent_request WHERE expires_at > ? ORDER BY created_at",
        (now,),
    ).fetchall()
    out: list[tuple[Pending, str]] = []
    for stored, role_id, client_id, redirect_uri, expires_at, approved_at in rows:
        node = tree.nodes.get(role_id)
        try:
            host = urlsplit(str(redirect_uri)).hostname or ""
        except ValueError:
            host = ""
        pending = Pending(
            reference(str(stored)),
            tree.path(node) if node is not None else "?",
            str(client_id),
            host,
            int(expires_at),
            approved_at is not None,
        )
        out.append((pending, str(stored)))
    return out


def pending_requests(conn: sqlite3.Connection, now: int) -> list[Pending]:
    """`hullgate consent list`: the live pending requests, oldest first (BR-310)."""
    return [p for p, _ in _pending_rows(conn, now)]


def approve_request(conn: sqlite3.Connection, ref: str, now: int) -> Pending:
    """`hullgate consent approve <reference>`: marks exactly one live pending request approved,
    audited `consent.approved` (BR-310). Its browser then passes the barrier with no factor, closed
    or not, within the request's 10 minutes; the code still goes only to the cookie's holder."""
    wanted = ref.strip().lower()
    with transaction(conn):
        found = [(p, h) for p, h in _pending_rows(conn, now) if p.reference == wanted]
        if not found:
            raise Refused(f"no pending request with reference {wanted!r} (expired?)")
        if len(found) > 1:
            raise Refused(f"reference {wanted!r} is ambiguous: deny and start again")
        pending, stored = found[0]
        if pending.approved:
            raise Refused(f"request {wanted} is already approved")
        conn.execute("UPDATE consent_request SET approved_at = ? WHERE hash = ?", (now, stored))
        role_id = conn.execute(
            "SELECT role_id FROM consent_request WHERE hash = ?", (stored,)
        ).fetchone()[0]
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_APPROVED,
            result="ok",
            node_id=role_id,
            path=pending.role,
            detail={"role": pending.role, "client_id": pending.client_id, "reference": wanted},
            now=now,
        )
    return pending


def _revoke_lineage(
    conn: sqlite3.Connection,
    tree: Tree,
    lineage_id: str,
    role_id: str,
    path: str,
    actor: str,
    reason: str,
    now: int,
) -> None:
    """Lineage and its access tokens revoked, with an audit entry (transaction in progress)."""
    conn.execute(
        "UPDATE access_jti SET revoked = 1 WHERE lineage_id = ? AND revoked = 0", (lineage_id,)
    )
    conn.execute(
        "UPDATE lineage SET revoked = 1, revoked_at = ? WHERE id = ? AND revoked = 0",
        (now, lineage_id),
    )
    audit.write(
        conn,
        actor=actor,
        action="lineage.revoke",
        result="ok",
        node_id=role_id,
        path=path,
        reason=reason,
        after={"token_id": lineage_id},
        now=now,
    )
