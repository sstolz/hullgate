"""Organisation-scoped routes of the public listener: one URL source and one grammar (F-014).

Every organisation-scoped route is the route in force before F-014 with `/o/<id>` in front, the
metadata keeping their `/.well-known/<suffix>` insertion (BR-044 v2, BR-056 v2):

- access point of a role: `/o/<id>/p/<path>/mcp`;
- its authorization server: `/o/<id>/oauth/p/<path>`, actions `/o/<id>/oauth/p/<path>/<action>`;
- RFC 9728 metadata: `/.well-known/oauth-protected-resource/o/<id>/p/<path>/mcp`;
- RFC 8414 metadata: `/.well-known/oauth-authorization-server/o/<id>/oauth/p/<path>`.

`<path>` is `<client>/<project>/<env>/<role>`; `<id>` is the organisation's public identifier
(BR-160). URLs are built from `PUBLIC_URL` and the stored identifier only, never from a request
(BR-041). The grammar reads the request's raw path as sent (BR-161): `scope["path"]` is
percent-decoded by the server, so a decoded check would serve `%2F` as a separator. A scope with
no `raw_path`, or with bytes that are not ASCII, matches no route; any `%`, empty segment, `..`,
trailing slash or case change fails the segment grammar. Whether the identifier is the
instance's own is not decided here: it is read in step 1 of the authorization order (BR-055,
BR-161), where an unreadable store answers 503 (BR-064).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from starlette.types import Scope

from hullgate.tree import SEGMENT_RE

ORG_ID_RE = re.compile(r"^[a-z2-7]{12}$")  # BR-160: 12 characters of lowercase RFC 4648 base32
RS_METADATA = "/.well-known/oauth-protected-resource"
AS_METADATA = "/.well-known/oauth-authorization-server"
ACTIONS = ("authorize", "consent", "token", "revoke", "register")
MCP = "mcp"  # the access point itself
RESOURCE_METADATA = "resource-metadata"
SERVER_METADATA = "server-metadata"
ROLE_DEPTH = 4  # client, project, environment, role


@dataclass(frozen=True, slots=True)
class Route:
    """An organisation-scoped route of well-formed shape: identifier, role path, action."""

    org_id: str
    path: str
    action: str


def _base(public_url: str) -> str:
    return public_url.rstrip("/")


def org_root(public_url: str, org_id: str) -> str:
    return f"{_base(public_url)}/o/{org_id}"


def resource_path(org_id: str, path: str) -> str:
    """Request path of a role's access point on the public listener."""
    return f"/o/{org_id}/p/{path}/mcp"


def resource_url(public_url: str, org_id: str, path: str) -> str:
    """URL of a role's access point, its RFC 8707 audience and `resource` (BR-044 v2)."""
    return f"{_base(public_url)}{resource_path(org_id, path)}"


def issuer_url(public_url: str, org_id: str, path: str) -> str:
    """Issuer of a role's authorization server (BR-056 v2), the JWT `iss` (BR-054)."""
    return f"{org_root(public_url, org_id)}/oauth/p/{path}"


def resource_metadata_url(public_url: str, org_id: str, path: str) -> str:
    """RFC 9728 metadata location of a role's access point."""
    return f"{_base(public_url)}{RS_METADATA}/o/{org_id}/p/{path}/mcp"


def consent_cookie_path(org_id: str, path: str) -> str:
    """`Path` of the barrier's cookie (F-014 §3.1)."""
    return f"/o/{org_id}/oauth/p/{path}/consent"


def _role_path(segments: list[str]) -> str | None:
    if len(segments) != ROLE_DEPTH or not all(SEGMENT_RE.match(s) for s in segments):
        return None  # segment grammar (BR-044): `dev%20`, `Dev`, `dev-`, `..`, `` → no route
    return "/".join(segments)


def parse(path: str) -> Route | None:
    """The organisation-scoped route a raw request path names, or None (no route, 404)."""
    if path.startswith(RS_METADATA + "/"):
        family, rest = RESOURCE_METADATA, path[len(RS_METADATA) :]
    elif path.startswith(AS_METADATA + "/"):
        family, rest = SERVER_METADATA, path[len(AS_METADATA) :]
    else:
        family, rest = "", path
    parts = rest.split("/")
    # ['', 'o', <id>, ...]: the prefix is exactly `/o/<id>/`
    if len(parts) < 4 or parts[0] != "" or parts[1] != "o" or not ORG_ID_RE.match(parts[2]):
        return None
    org_id, tail = parts[2], parts[3:]
    if family == RESOURCE_METADATA or family == "":
        # `p/<4 segments>/mcp`, or `oauth/p/<4 segments>/<action>` outside the metadata
        if len(tail) == ROLE_DEPTH + 2 and tail[0] == "p" and tail[-1] == MCP:
            role = _role_path(tail[1:-1])
            return Route(org_id, role, family or MCP) if role else None
        action = tail[-1]
        if family == "" and len(tail) == ROLE_DEPTH + 3 and tail[:2] == ["oauth", "p"]:
            role = _role_path(tail[2:-1]) if action in ACTIONS else None
            return Route(org_id, role, action) if role else None
        return None
    # server metadata: `oauth/p/<4 segments>`
    if len(tail) == ROLE_DEPTH + 2 and tail[:2] == ["oauth", "p"]:
        role = _role_path(tail[2:])
        return Route(org_id, role, SERVER_METADATA) if role else None
    return None


def raw_request_path(scope: Scope) -> str | None:
    """The request's path as sent (ASGI `raw_path`); None when absent or not ASCII (BR-161)."""
    raw = scope.get("raw_path")
    if not isinstance(raw, bytes | bytearray):
        return None
    try:
        return bytes(raw).decode("ascii")
    except UnicodeDecodeError:
        return None


def route_of(scope: Scope) -> Route | None:
    """The organisation-scoped route of an HTTP request, read on its raw path only."""
    if scope.get("type") != "http":
        return None
    raw = raw_request_path(scope)
    return parse(raw) if raw is not None else None


def parse_resource(public_url: str, url: str) -> tuple[str, str] | None:
    """`(identifier, role path)` of a string of an access point's URL form, else None."""
    base = _base(public_url)
    if not url.startswith(base + "/"):
        return None
    route = parse(url[len(base) :])
    return (route.org_id, route.path) if route is not None and route.action == MCP else None
