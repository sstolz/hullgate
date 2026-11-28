"""Outbound OAuth client (F-006 lot B): discovery, dynamic registration, code exchange,
renewal, account reading, revocation — on BR-084's bounded client.

Written by hand (no SDK object, ADR-004 proposed). These functions do not touch the database:
they return values or raise `ProviderError`, whose `kind` follows BR-092's closed table
(`permanent`, `transient`, `uncertain`, `anomaly`) and whose `reason` carries only the `error`
code of the closed list (truncated to 64) or a short cause: never the body, the
`error_description`, a callback URL, a code or a token.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from hullgate.outbound import ANOMALY, TRANSIENT, Failure, InternalHost, Outbound

DEFINITIVE = "permanent"
DEFINITIVE_ERRORS = frozenset(
    {"invalid_grant", "invalid_scope", "consent_required", "interaction_required", "login_required"}
)
APPLICATION_ERRORS = frozenset({"invalid_client", "unauthorized_client"})
SHORT_EXPIRY = "expiry_too_short"
EXPIRY_MARGIN = 60  # BR-091
DEFAULT_EXPIRY = 3600  # §3.1, both models
ERROR_MAX = 64
ENDPOINT_NAMES = (
    "authorization_endpoint",
    "token_endpoint",
    "userinfo_endpoint",
    "revocation_endpoint",
    "registration_endpoint",
)
_ERROR_RE = re.compile(r"^[\x20-\x21\x23-\x5b\x5d-\x7e]+$")  # RFC 6749 §5.2, `error`


class ProviderError(Exception):
    """Response or failure classified by BR-092's table."""

    def __init__(self, kind: str, reason: str, retry_after: float | None = None) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason
        self.retry_after = retry_after

    @property
    def application(self) -> bool:
        return self.reason in APPLICATION_ERRORS


class DiscoveryError(Exception):
    """Discovery failed or document refused (BR-097); `reason` is a short cause."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def error_code(value: object) -> str:
    """`error` code of a response, reduced to its RFC 6749 form and truncated to 64 characters."""
    if not isinstance(value, str) or not value or not _ERROR_RE.match(value):
        return "unknown"
    return value[:ERROR_MAX]


def _failure(exc: Failure) -> ProviderError:
    return ProviderError(exc.kind, exc.reason)


def parse_retry_after(value: str | None, now: float) -> float | None:
    """`Retry-After` in seconds or as an HTTP date; invalid or past: None (BR-094)."""
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        seconds = float(int(text))
        return seconds if seconds > 0 else None
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None or when.tzinfo is None:
        return None
    delta = when.timestamp() - now
    return delta if delta > 0 else None


def classify(
    status: int, doc: Mapping[str, Any] | None, retry_after: float | None
) -> ProviderError:
    """Unsuccessful response of a token endpoint, classified by BR-092's table."""
    if status == 429 or status >= 500:
        return ProviderError(TRANSIENT, f"HTTP {status}", retry_after)
    if 300 <= status < 400:
        return ProviderError(ANOMALY, "redirection")
    if doc is not None:
        code = error_code(doc.get("error"))
        if code in DEFINITIVE_ERRORS or code in APPLICATION_ERRORS:
            return ProviderError(DEFINITIVE, code)
    return ProviderError(ANOMALY, f"HTTP {status}")


# ---------------------------------------------------------------------------------------------
# Discovery (BR-097, §3.1)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Discovered:
    endpoints: dict[str, str]
    resource: str | None


def _well_known(base: str, suffix: str) -> str:
    """Well-known URL inserted between the host and the path (RFC 8414 §3.1, RFC 9728 §3.1)."""
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, f"/.well-known/{suffix}{path}", "", ""))


def _get_json(
    out: Outbound, url: str, internal: InternalHost | None
) -> tuple[int, dict[str, Any] | None]:
    try:
        response = out.request("GET", url, internal=internal)
    except Failure as exc:
        raise DiscoveryError(exc.reason) from None
    if response.status != 200:
        return response.status, None
    try:
        return 200, response.json()
    except Failure as exc:
        raise DiscoveryError(exc.reason) from None


def _endpoints(doc: Mapping[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in ENDPOINT_NAMES:
        value = doc.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise DiscoveryError(f"{name} invalid")
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.fragment:
            raise DiscoveryError(f"{name} not https")
        found[name] = value
    for required in ("authorization_endpoint", "token_endpoint"):
        if required not in found:
            raise DiscoveryError(f"{required} absent")
    return found


def discover(out: Outbound, model: str, base_url: str, internal: InternalHost | None) -> Discovered:
    """Endpoints according to the model (§3.1); blocking, bounded by BR-084."""
    if model == "gitea":
        status, doc = _get_json(
            out, f"{base_url.rstrip('/')}/.well-known/openid-configuration", internal
        )
        if doc is None:
            raise DiscoveryError(f"openid-configuration: HTTP {status}")
        return Discovered(_endpoints(doc), None)
    resource: str | None = None
    issuer = base_url
    status, prm = _get_json(out, _well_known(base_url, "oauth-protected-resource"), internal)
    if prm is not None:
        value = prm.get("resource")
        if isinstance(value, str) and urlsplit(value).scheme == "https":
            resource = value
        servers = prm.get("authorization_servers")
        if isinstance(servers, list) and servers and isinstance(servers[0], str):
            if urlsplit(servers[0]).scheme != "https":
                raise DiscoveryError("authorization_servers not https")
            issuer = servers[0]
    status, doc = _get_json(out, _well_known(issuer, "oauth-authorization-server"), internal)
    if doc is None:
        status, doc = _get_json(
            out, f"{issuer.rstrip('/')}/.well-known/openid-configuration", internal
        )
    if doc is None:
        raise DiscoveryError(f"authorization server metadata: HTTP {status}")
    return Discovered(_endpoints(doc), resource)


# ---------------------------------------------------------------------------------------------
# Client and authorization (BR-082)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class Client:
    """Application as seen by the token endpoint; never displayed (the plaintext secret lives
    in it)."""

    client_id: str
    secret: str | None = None
    method: str = "none"  # none | client_secret_basic | client_secret_post

    def __repr__(self) -> str:
        return f"Client({self.client_id!r}, <redacted>)"

    def apply(self, form: dict[str, str], headers: dict[str, str]) -> None:
        if self.secret is not None and self.method == "client_secret_basic":
            pair = f"{quote(self.client_id, safe='')}:{quote(self.secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
            return
        form["client_id"] = self.client_id
        if self.secret is not None and self.method == "client_secret_post":
            form["client_secret"] = self.secret


@dataclass(frozen=True, slots=True, repr=False)
class Pkce:
    verifier: str
    challenge: str

    @classmethod
    def new(cls) -> Pkce:
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return cls(verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"))


def authorization_url(
    endpoint: str,
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str,
    challenge: str,
    resource: str | None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = scopes
    if resource is not None:
        params["resource"] = resource
    sep = "&" if urlsplit(endpoint).query else "?"
    return f"{endpoint}{sep}{urlencode(params)}"


# ---------------------------------------------------------------------------------------------
# Token endpoint (code exchange, renewal; BR-091, BR-092)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str | None
    refresh_token_expires_in: int | None = None

    def __repr__(self) -> str:
        return "TokenSet(<redacted>)"


def _token_request(
    out: Outbound,
    endpoint: str,
    form: dict[str, str],
    client: Client,
    internal: InternalHost | None,
    now: float,
) -> TokenSet:
    headers: dict[str, str] = {}
    client.apply(form, headers)
    try:
        response = out.request("POST", endpoint, headers=headers, form=form, internal=internal)
    except Failure as exc:
        raise _failure(exc) from None
    retry = parse_retry_after(response.header("Retry-After"), now)
    if response.status != 200:
        try:
            doc: dict[str, Any] | None = response.json()
        except Failure:
            doc = None
        raise classify(response.status, doc, retry)
    try:
        doc = response.json()
    except Failure as exc:
        raise _failure(exc) from None
    access = doc.get("access_token")
    kind = doc.get("token_type")
    if not isinstance(access, str) or not access:
        raise ProviderError(ANOMALY, "access_token absent")
    if not isinstance(kind, str) or kind.lower() != "bearer":
        raise ProviderError(ANOMALY, "token_type other than Bearer")
    refresh = doc.get("refresh_token")
    if refresh is not None and (not isinstance(refresh, str) or not refresh):
        raise ProviderError(ANOMALY, "invalid refresh_token")
    expires = doc.get("expires_in")
    expires_in: int | None = None
    if expires is not None:
        if isinstance(expires, bool) or not isinstance(expires, (int, float, str)):
            raise ProviderError(ANOMALY, "invalid expires_in")
        try:
            expires_in = int(float(expires))
        except ValueError:
            raise ProviderError(ANOMALY, "invalid expires_in") from None
        if expires_in <= EXPIRY_MARGIN:
            raise ProviderError(DEFINITIVE, SHORT_EXPIRY)
    scope = doc.get("scope")
    refresh_expiry = doc.get("refresh_token_expires_in")
    if refresh_expiry is not None and (
        isinstance(refresh_expiry, bool)
        or not isinstance(refresh_expiry, int)
        or refresh_expiry < 0
    ):
        raise ProviderError(ANOMALY, "invalid refresh_token_expires_in")
    if refresh_expiry is not None:
        try:
            datetime.fromtimestamp(int(now) + refresh_expiry, UTC)
        except (OverflowError, OSError, ValueError):
            raise ProviderError(ANOMALY, "invalid refresh_token_expires_in") from None
    return TokenSet(
        access, refresh, expires_in, scope if isinstance(scope, str) else None, refresh_expiry
    )


def exchange_code(
    out: Outbound,
    endpoint: str,
    client: Client,
    code: str,
    verifier: str,
    redirect_uri: str,
    resource: str | None,
    internal: InternalHost | None,
    now: float,
) -> TokenSet:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if resource is not None:
        form["resource"] = resource
    return _token_request(out, endpoint, form, client, internal, now)


def refresh(
    out: Outbound,
    endpoint: str,
    client: Client,
    refresh_token: str,
    resource: str | None,
    internal: InternalHost | None,
    now: float,
) -> TokenSet:
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if resource is not None:
        form["resource"] = resource
    return _token_request(out, endpoint, form, client, internal, now)


# ---------------------------------------------------------------------------------------------
# Account (BR-080, BR-096), revocation (BR-075, BR-095), dynamic registration (BR-083)
# ---------------------------------------------------------------------------------------------


def account(
    out: Outbound,
    model: str,
    endpoint: str | None,
    access_token: str,
    internal: InternalHost | None,
) -> str | None:
    """Account by the model's path (§3.1); None = "unknown". Network failure: `ProviderError`."""
    if endpoint is None:
        return None
    try:
        response = out.request(
            "GET", endpoint, headers={"Authorization": f"Bearer {access_token}"}, internal=internal
        )
    except Failure as exc:
        raise _failure(exc) from None
    if response.status == 429 or response.status >= 500:
        raise ProviderError(TRANSIENT, f"userinfo HTTP {response.status}")
    if response.status != 200:
        return None
    try:
        doc = response.json()
    except Failure:
        return None
    keys = ("preferred_username",) if model == "gitea" else ("preferred_username", "sub")
    for key in keys:
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return None


def revoke(
    out: Outbound,
    endpoint: str,
    client: Client,
    token: str,
    hint: str,
    internal: InternalHost | None,
) -> None:
    """RFC 7009 revocation; any status other than 200 is a failure (audited by the caller)."""
    form = {"token": token, "token_type_hint": hint}
    headers: dict[str, str] = {}
    client.apply(form, headers)
    try:
        response = out.request("POST", endpoint, headers=headers, form=form, internal=internal)
    except Failure as exc:
        raise _failure(exc) from None
    if response.status != 200:
        raise ProviderError(
            TRANSIENT if response.status >= 500 else ANOMALY, f"HTTP {response.status}"
        )


@dataclass(frozen=True, slots=True, repr=False)
class Registration:
    client_id: str
    secret: str | None
    secret_expires_at: int | None
    method: str

    def __repr__(self) -> str:
        return f"Registration({self.client_id!r}, <redacted>)"


def register(
    out: Outbound,
    endpoint: str,
    redirect_uri: str,
    scopes: str,
    internal: InternalHost | None,
    web_redirect: str | None = None,
) -> Registration:
    """RFC 7591 dynamic registration of a public PKCE client (decision B2); with the console's
    return URI as a second one when it is known (BR-216)."""
    body: dict[str, Any] = {
        "client_name": "hullgate",
        "redirect_uris": [redirect_uri] if web_redirect is None else [redirect_uri, web_redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scopes:
        body["scope"] = scopes
    try:
        response = out.request("POST", endpoint, json_body=body, internal=internal)
    except Failure as exc:
        raise _failure(exc) from None
    if response.status not in (200, 201):
        try:
            doc: dict[str, Any] | None = response.json()
        except Failure:
            doc = None
        raise classify(response.status, doc, None)
    try:
        doc = response.json()
    except Failure as exc:
        raise _failure(exc) from None
    client_id = doc.get("client_id")
    if not isinstance(client_id, str) or not client_id or len(client_id) > 255:
        raise ProviderError(ANOMALY, "client_id absent")
    secret = doc.get("client_secret")
    if secret is not None and (not isinstance(secret, str) or not secret):
        raise ProviderError(ANOMALY, "invalid client_secret")
    expires = doc.get("client_secret_expires_at")
    expires_at = int(expires) if isinstance(expires, int) and expires > 0 else None
    method = doc.get("token_endpoint_auth_method")
    if secret is None:
        chosen = "none"
    elif method in ("client_secret_basic", "client_secret_post"):
        chosen = str(method)
    else:
        chosen = "client_secret_basic"
    return Registration(client_id, secret, expires_at, chosen)


@dataclass(frozen=True, slots=True)
class Callback:
    """Authorization callback URL read on standard input; never displayed."""

    state: str | None
    code: str | None
    error: str | None = None

    def __repr__(self) -> str:
        return "Callback(<redacted>)"


def parse_callback(text: str) -> Callback:
    try:
        parts = urlsplit(text.strip())
    except ValueError:  # unreadable URL: no state, refused as such by `authorize`
        return Callback(None, None, None)
    query = parse_qs(parts.query, keep_blank_values=True)

    def one(name: str) -> str | None:
        values = query.get(name)
        return values[0] if values and len(values) == 1 and values[0] else None

    error = one("error")
    return Callback(one("state"), one("code"), None if error is None else error_code(error))
