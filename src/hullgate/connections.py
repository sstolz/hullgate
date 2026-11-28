"""Connection library (F-006): services, applications, connections, placements.

A connection belongs to the organisation, is named `<client>/<name>` and carries its origin client
(BR-074); its token set is encrypted by the connections key (BR-087, `hullgate.cipher`) and is
unique whatever its number of placements. A placement puts a connection on a role (BR-077); a
sharing outside the origin client or between a sensitive and a non-sensitive role requires a
traced confirmation (BR-078), and a tree change that creates one is signalled (BR-079).

The functions that write open their own `BEGIN IMMEDIATE` transaction and write their audit entry
in it (BR-065, BR-066 v2); a refusal raises `Refused` (code 2) before any visible write, an
unavailable key raises `KeyUnavailable` (code 1). No token or secret value enters an output, an
audit entry or a message.

Lot B: the endpoint discovery (BR-097) happens at `service declare` through the bounded client of
BR-084 (`hullgate.outbound`, `hullgate.upstream`); `connection add --application` and `reauthorize`
open the outbound authorization (PKCE S256, single-use `state`, BR-082). What calls the provider
after the opening — `authorize`, renewal under lease, `revoke`, `remove`, `signal_refusal`, dynamic
registration — lives in `hullgate.lifecycle`.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from hullgate import audit, upstream
from hullgate.cipher import ConnectionsKey, Sealer
from hullgate.db import Database, transaction
from hullgate.outbound import Failure, InternalHost, Outbound
from hullgate.tree import Clock, Node, Refused, Tree, validate_segment

MODELS = ("gitea", "generic")
DEFAULT_MODEL = "generic"
REDIRECT_URI = "http://127.0.0.1/oauth/callback"  # BR-082 (§3.1): loopback without a listener
PREFIX_RE = re.compile(r"^[a-z0-9]{2,8}$")  # BR-076
ACCOUNT_MAX = 80
SCOPE_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")  # RFC 6749 §3.3, scope-token
EXPIRY_MARGIN = 60  # BR-091
PENDING_TTL_S = 600  # BR-082: single-use `state`, 10 minutes

STATE_LABELS = {
    "pending": "pending",
    "active": "active",
    "reauth_required": "reauthorization required",
}
KIND_LABELS = {"oauth": "oauth", "provided": "provided token"}
UPSTREAM_TYPES = ("mcp-public", "mcp-internal")  # F-008, BR-106: extensible enumeration (Q2)
UPSTREAM_UNDECLARED = "no MCP upstream declared"
MCP_REFUSAL_REASON = "mcp_refusal_401"  # `last_reason` set by BR-127

# Audit keys (BR-066 v2)
A_SERVICE_DECLARE = "service.declare"
A_SERVICE_REMOVE = "service.remove"
A_SERVICE_REDISCOVER = "service.rediscover"
A_SERVICE_UPSTREAM = "service.upstream"
A_SERVICE_UPSTREAM_REMOVE = "service.upstream_remove"
A_APP_DECLARE = "application.declare"
A_APP_REMOVE = "application.remove"
A_APP_WEB_RETURN = "application.web_return"
A_CONN_ADD = "connection.add"
A_CONN_REPLACE = "connection.replace_value"
A_CONN_PREFIX = "connection.prefix"
A_CONN_REMOVE = "connection.remove"
A_CONN_REVOKE_UPSTREAM = "connection.upstream_revoke"
A_PLACE = "connection.place"
A_UNPLACE = "connection.unplace"
A_SHARE_CONFIRMED = "connection.share_confirmed"
A_SHARE_SIGNAL = "connection.share_signal"
A_AUTHORIZE_OPEN = "connection.authorize_open"
A_KEY_UNAVAILABLE = "connection.key_unavailable"
A_INTEGRITY = "connection.integrity_error"

SHARE_CLIENT = "other_client"
SHARE_SENSITIVITY = "sensitivity"


def _new_id() -> str:
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------


def parse_name(text: str) -> tuple[str, str]:
    """`<client>/<name>` → (client segment, name); refusal if the form is other (BR-074)."""
    parts = text.strip().strip("/").split("/")
    if len(parts) != 2:
        raise Refused(f"invalid connection name: {text!r} (expected <client>/<name>)")
    try:
        validate_segment(parts[1])
    except Refused as exc:
        raise Refused(f"invalid connection name: {text!r} ({exc})") from exc
    return parts[0], parts[1]


def validate_prefix(prefix: str) -> str:
    if not PREFIX_RE.match(prefix):
        raise Refused(f"invalid prefix: {prefix!r} (2 to 8 characters [a-z0-9])")
    return prefix


def validate_id(kind: str, value: str) -> str:
    try:
        return validate_segment(value)
    except Refused as exc:
        raise Refused(f"invalid {kind} identifier: {exc}") from exc


def validate_account(label: str) -> str:
    if not 1 <= len(label) <= ACCOUNT_MAX or any(unicodedata.category(ch) == "Cc" for ch in label):
        raise Refused(f"invalid account: 1 to {ACCOUNT_MAX} characters, no control character")
    return label


SECRET_MAX = 64 * 1024


def secret_value(data: str) -> str:
    """A provided token or an application secret as read (BR-086): one final newline removed,
    refused when longer than `SECRET_MAX`, blank, or on several lines. The CLI's file reader and
    the admin API apply the same checks (BR-211). No refusal repeats the value."""
    if len(data) > SECRET_MAX:
        raise Refused(f"value too long (more than {SECRET_MAX} bytes)")
    if data.endswith("\r\n"):
        data = data[:-2]
    elif data.endswith("\n"):
        data = data[:-1]
    if not data.strip():
        raise Refused("empty value")
    if any(c in data for c in "\r\n"):
        raise Refused("value on several lines")
    return data


def validate_base_url(url: str) -> str:
    """https base URL of a service. A refusal never repeats the URL: a `user:secret@` typed by
    mistake is copied nowhere (review 417 m1, spirit of BR-086)."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise Refused("invalid service URL (URL not repeated): unreadable form") from None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise Refused("invalid service URL (URL not repeated): credentials in the URL refused")
    if parts.scheme != "https" or not parts.hostname:
        raise Refused("invalid service URL (URL not repeated): https:// and a host expected")
    if parts.query or parts.fragment or port == 0:
        raise Refused("invalid service URL (URL not repeated): neither query nor fragment")
    return url.strip().rstrip("/")


def parse_scopes(text: str | None) -> str:
    if not text:
        return ""
    scopes = [s for s in (p.strip() for p in text.split(",")) if s]
    for scope in scopes:
        if not SCOPE_RE.match(scope):
            raise Refused(f"invalid scope: {scope!r}")
    return " ".join(dict.fromkeys(scopes))


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Service:
    id: str
    model: str
    base_url: str
    scopes: str
    internal_host: bool
    endpoints: dict[str, str]
    resource: str | None
    upstream_type: str | None = None  # F-008, BR-106: NULL = no MCP upstream declared
    mcp_url: str | None = None
    mcp_internal: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "url": self.base_url,
            "scopes": self.scopes.split() if self.scopes else [],
            "internal_host": self.internal_host,
            "endpoints": dict(sorted(self.endpoints.items())),
            "resource": self.resource,
            "upstream": (
                None
                if self.upstream_type is None
                else {
                    "type": self.upstream_type,
                    "mcp_url": self.mcp_url,
                    "mcp_internal_host": self.mcp_internal,
                }
            ),
        }

    def upstream_label(self) -> str:
        """ "<type> <host>" or "no MCP upstream declared" (`connection status`, F-008 §3.2)."""
        if self.upstream_type is None or self.mcp_url is None:
            return UPSTREAM_UNDECLARED
        return f"{self.upstream_type} {urlsplit(self.mcp_url).hostname}"


@dataclass(frozen=True, slots=True)
class Application:
    id: str
    service_id: str
    mode: str
    client_id: str
    redirect_uri: str
    has_secret: bool
    secret_expires_at: int | None
    auth_method: str = "none"
    web_return: bool = False  # BR-216: the console's return URI is registered at the provider

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "service": self.service_id,
            "mode": self.mode,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "secret": "encrypted" if self.has_secret else None,
            "secret_expires_at": (
                audit.utc(self.secret_expires_at) if self.secret_expires_at else None
            ),
            "method": self.auth_method,
        }


@dataclass(frozen=True, slots=True)
class Row:
    """Live row of `connection` (never the encrypted value)."""

    id: str
    client_node_id: str
    name: str
    prefix: str
    service_id: str
    application_id: str | None
    kind: str
    account: str | None
    state: str
    granted_scopes: str
    expires_at: int | None
    expiry_estimated: bool
    not_before: int | None
    generation: int
    last_outcome: str | None
    has_tokens: bool
    last_reason: str | None = None
    refresh_expires_at: int | None = None


_ROW_COLUMNS = (
    "id, client_node_id, name, prefix, service_id, application_id, kind, account, state,"
    " granted_scopes, expires_at, expiry_estimated, not_before, generation, last_outcome,"
    " tokens_enc IS NOT NULL, last_reason, refresh_expires_at"
)


def _row(r: tuple[Any, ...]) -> Row:
    return Row(
        r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], bool(r[11]), r[12],
        int(r[13]), r[14], bool(r[15]), r[16], r[17],
    )  # fmt: skip


def _services(conn: sqlite3.Connection) -> list[Service]:
    rows = conn.execute(
        "SELECT id, model, base_url, scopes, internal_host, endpoints, resource, upstream_type,"
        " mcp_url, mcp_internal FROM service ORDER BY id"
    ).fetchall()
    return [
        Service(r[0], r[1], r[2], r[3], bool(r[4]), json.loads(r[5]), r[6], r[7], r[8], bool(r[9]))
        for r in rows
    ]


def _service(conn: sqlite3.Connection, service_id: str) -> Service:
    found = next((s for s in _services(conn) if s.id == service_id), None)
    if found is None:
        raise Refused(f"unknown service: {service_id}")
    return found


def _applications(conn: sqlite3.Connection) -> list[Application]:
    rows = conn.execute(
        "SELECT id, service_id, mode, client_id, redirect_uri, secret_enc IS NOT NULL,"
        " secret_expires_at, auth_method, web_return FROM application ORDER BY id"
    ).fetchall()
    return [
        Application(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6], r[7], bool(r[8])) for r in rows
    ]


def _live_rows(conn: sqlite3.Connection) -> list[Row]:
    rows = conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM connection WHERE removed = 0"  # noqa: S608
    ).fetchall()
    return [_row(r) for r in rows]


def display_name(nodes: Mapping[str, Node], row: Row) -> str:
    client = nodes.get(row.client_node_id)
    return f"{client.slug if client is not None else '?'}/{row.name}"


def _find(conn: sqlite3.Connection, tree: Tree, text: str) -> Row:
    client_slug, name = parse_name(text)
    client = next((k for k in tree.kids(tree.org) if k.slug == client_slug), None)
    if client is None:
        raise Refused(f"unknown client: {client_slug}")
    row = conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM connection"  # noqa: S608
        " WHERE removed = 0 AND client_node_id = ? AND name = ?",
        (client.id, name),
    ).fetchone()
    if row is None:
        raise Refused(f"unknown connection: {client_slug}/{name}")
    return _row(row)


def erase_policy(conn: sqlite3.Connection, connection_id: str, role_id: str | None = None) -> None:
    """A placement's policy goes with it, in the caller's transaction (F-015 BR-176): deleted
    before the placement row, which the policy rows reference."""
    for table in ("placement_tool", "argument_rule", "pinned_definition"):
        if role_id is None:
            conn.execute(f"DELETE FROM {table} WHERE connection_id = ?", (connection_id,))  # noqa: S608
        else:
            conn.execute(
                f"DELETE FROM {table} WHERE connection_id = ? AND role_id = ?",  # noqa: S608
                (connection_id, role_id),
            )


def _placements(conn: sqlite3.Connection, connection_id: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT role_id FROM placement WHERE connection_id = ?", (connection_id,)
        ).fetchall()
    ]


# ---------------------------------------------------------------------------------------------
# Sharing (BR-078, BR-079): computed on a dictionary of nodes, real or simulated
# ---------------------------------------------------------------------------------------------


def client_of(nodes: Mapping[str, Node], node: Node) -> Node | None:
    current: Node | None = node
    while current is not None and current.level != "client":
        current = nodes.get(current.parent_id) if current.parent_id is not None else None
    return current


def role_sensitive(nodes: Mapping[str, Node], role: Node) -> bool:
    """Effective sensitivity of a role (BR-047): that of its environment."""
    parent = nodes.get(role.parent_id) if role.parent_id is not None else None
    return parent is not None and parent.sensitive


def node_path(nodes: Mapping[str, Node], node: Node) -> str:
    segments: list[str] = []
    current: Node | None = node
    while current is not None and current.level != "org":
        segments.append(current.slug)
        current = nodes.get(current.parent_id) if current.parent_id is not None else None
    return "/".join(reversed(segments)) or "/"


def share_reasons(nodes: Mapping[str, Node], origin_id: str, roles: Iterable[Node]) -> set[str]:
    """Reasons why a connection placed on `roles` is shared in the sense of BR-078."""
    reasons: set[str] = set()
    sensitivities: set[bool] = set()
    for role in roles:
        client = client_of(nodes, role)
        if client is None or client.id != origin_id:
            reasons.add(SHARE_CLIENT)
        sensitivities.add(role_sensitive(nodes, role))
    if len(sensitivities) > 1:
        reasons.add(SHARE_SENSITIVITY)
    return reasons


@dataclass(frozen=True, slots=True)
class ShareSignal:
    """A connection that a tree change makes shared (BR-079)."""

    connection: str
    reasons: tuple[str, ...]
    roles: tuple[tuple[str, bool], ...]  # (role path, effective sensitivity) after the change

    def text(self) -> str:
        why = {
            SHARE_CLIENT: "outside its origin client",
            SHARE_SENSITIVITY: "between sensitive and non-sensitive roles",
        }
        roles = ", ".join(f"{p} ({'sensitive' if s else 'non-sensitive'})" for p, s in self.roles)
        return (
            f"warning: connection {self.connection} becomes shared "
            f"({' and '.join(why[r] for r in self.reasons)}): {roles}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "reasons": list(self.reasons),
            "roles": [{"role": p, "sensitive": s} for p, s in self.roles],
        }


def shares_created(
    conn: sqlite3.Connection, before: Mapping[str, Node], after: Mapping[str, Node]
) -> list[ShareSignal]:
    """Placed connections whose sharing gains a reason between two states of the tree."""
    out: list[ShareSignal] = []
    for row in sorted(_live_rows(conn), key=lambda r: display_name(before, r)):
        role_ids = _placements(conn, row.id)
        if not role_ids:
            continue
        old = share_reasons(before, row.client_node_id, (before[i] for i in role_ids))
        roles_after = [after[i] for i in role_ids if i in after]
        new = share_reasons(after, row.client_node_id, roles_after)
        if new - old:
            out.append(
                ShareSignal(
                    display_name(before, row),
                    tuple(sorted(new - old)),
                    tuple(
                        sorted((node_path(after, r), role_sensitive(after, r)) for r in roles_after)
                    ),
                )
            )
    return out


def audit_share_signals(
    conn: sqlite3.Connection, signals: Iterable[ShareSignal], trigger: str, path: str, now: float
) -> None:
    for signal in signals:
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_SHARE_SIGNAL,
            result="warning",
            path=signal.connection,
            reason=trigger,
            detail={"tree_path": path, **signal.as_dict()},
            now=now,
        )


# ---------------------------------------------------------------------------------------------
# Tree hooks (BR-045 clarified)
# ---------------------------------------------------------------------------------------------


def refuse_if_clients_carry_connections(
    conn: sqlite3.Connection, tree: Tree, nodes: Iterable[Node]
) -> None:
    """Deleting a client that carries connections is refused; the refusal names them."""
    clients = {n.id for n in nodes if n.level == "client"}
    carried = sorted(
        display_name(tree.nodes, r) for r in _live_rows(conn) if r.client_node_id in clients
    )
    if carried:
        raise Refused(f"client carrying connections: {', '.join(carried)} (remove them first)")


def remove_placements_of_roles(
    conn: sqlite3.Connection, tree: Tree, role_ids: Iterable[str], reason: str, now: float
) -> int:
    """Removes the placements of the given roles, in the caller's transaction; audited."""
    removed = 0
    rows = {r.id: r for r in _live_rows(conn)}
    for role_id in role_ids:
        for (connection_id,) in conn.execute(
            "SELECT connection_id FROM placement WHERE role_id = ?", (role_id,)
        ).fetchall():
            erase_policy(conn, connection_id, role_id)
            conn.execute(
                "DELETE FROM placement WHERE connection_id = ? AND role_id = ?",
                (connection_id, role_id),
            )
            row = rows.get(connection_id)
            role = tree.nodes.get(role_id)
            audit.write(
                conn,
                actor=audit.actor(),
                action=A_UNPLACE,
                result="ok",
                node_id=role_id,
                path=tree.path(role) if role is not None else None,
                reason=reason,
                detail={"connection": display_name(tree.nodes, row) if row else connection_id},
                now=now,
            )
            removed += 1
    return removed


# ---------------------------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Declared:
    service: Service
    addresses: tuple[str, ...]  # admitted addresses of an internal host, at declaration (BR-084)


def internal_of(service: Service) -> InternalHost | None:
    """Internal-host exception of the record (BR-084), or None."""
    return InternalHost.of(service.base_url) if service.internal_host else None


def _discover(
    out: Outbound, model: str, url: str, internal: InternalHost | None
) -> upstream.Discovered:
    try:
        return upstream.discover(out, model, url, internal)
    except upstream.DiscoveryError as exc:
        raise Refused(f"discovery impossible: {exc.reason} (nothing was written)") from None


def service_declare(
    conn: sqlite3.Connection,
    out: Outbound,
    service_id: str,
    base_url: str,
    model: str | None,
    scopes: str | None,
    internal_host: bool,
    *,
    clock: Clock = time.time,
) -> Declared:
    """Record of a service (BR-074) and bounded discovery of its endpoints (BR-097, BR-084),
    kept in the record. For an internal host, the resolved addresses admitted at that moment are
    displayed and audited; they are checked again on every call."""
    if conn.execute("SELECT 1 FROM service WHERE id = ?", (service_id,)).fetchone():
        raise Refused(f"service already declared: {service_id}")
    prepared = prepare_service(out, service_id, base_url, model, scopes, internal_host)
    return write_service(conn, prepared, clock=clock)


@dataclass(frozen=True)
class PreparedService:
    id: str
    model: str
    url: str
    scopes: str
    internal_host: bool
    addresses: list[str]
    found: upstream.Discovered


def prepare_service(
    out: Outbound,
    service_id: str,
    base_url: str,
    model: str | None,
    scopes: str | None,
    internal_host: bool,
) -> PreparedService:
    """Validate and discover without holding a database transaction or process lock."""
    validate_id("service", service_id)
    url = validate_base_url(base_url)
    chosen = model or DEFAULT_MODEL
    if chosen not in MODELS:
        raise Refused(f"unknown model: {chosen} (gitea or generic)")
    scope_text = parse_scopes(scopes)
    if chosen == "gitea" and "openid" not in scope_text.split():
        scope_text = " ".join([*scope_text.split(), "openid"])  # §3.1: account through userinfo
    internal = InternalHost.of(url) if internal_host else None
    addresses: list[str] = []
    if internal is not None:
        try:
            addresses = out.addresses(url, internal)
        except Failure as exc:
            raise Refused(f"internal host refused: {exc.reason} (nothing was written)") from None
    found = _discover(out, chosen, url, internal)
    return PreparedService(service_id, chosen, url, scope_text, internal_host, addresses, found)


def write_service(
    conn: sqlite3.Connection,
    prepared: PreparedService,
    *,
    update: bool = False,
    clock: Clock = time.time,
) -> Declared:
    """Store a prepared declaration; an import update preserves catalogue and MCP metadata."""
    service_id, chosen, url = prepared.id, prepared.model, prepared.url
    scope_text, internal_host = prepared.scopes, prepared.internal_host
    addresses, found = prepared.addresses, prepared.found
    with transaction(conn):
        now = clock()
        before = None
        if update:
            service = _service(conn, service_id)
            apps = [a.id for a in _applications(conn) if a.service_id == service_id]
            users = [r for r in _live_rows(conn) if r.service_id == service_id]
            if apps or users:
                raise Refused("service referenced by applications or connections")
            before = {
                "model": service.model,
                "base_url": service.base_url,
                "scopes": service.scopes,
                "internal_host": service.internal_host,
            }
            conn.execute(
                "UPDATE service SET model = ?, base_url = ?, scopes = ?, internal_host = ?,"
                " endpoints = ?, resource = ? WHERE id = ?",
                (
                    chosen,
                    url,
                    scope_text,
                    int(internal_host),
                    json.dumps(found.endpoints, sort_keys=True),
                    found.resource,
                    service_id,
                ),
            )
        else:
            if conn.execute("SELECT 1 FROM service WHERE id = ?", (service_id,)).fetchone():
                raise Refused(f"service already declared: {service_id}")
            conn.execute(
                "INSERT INTO service (id, model, base_url, scopes, internal_host, endpoints,"
                " resource, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    service_id,
                    chosen,
                    url,
                    scope_text,
                    int(internal_host),
                    json.dumps(found.endpoints, sort_keys=True),
                    found.resource,
                    int(now),
                ),
            )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_SERVICE_DECLARE,
            result="ok",
            path=service_id,
            before=before,
            after={
                "model": chosen,
                "base_url": url,
                "scopes": scope_text,
                "internal_host": internal_host,
                "admitted_addresses": addresses,
                "endpoints": found.endpoints,
                "resource": found.resource,
            },
            now=now,
        )
        return Declared(_service(conn, service_id), tuple(addresses))


def service_rediscover(
    conn: sqlite3.Connection,
    out: Outbound,
    service_id: str,
    confirm: str | None,
    *,
    clock: Clock = time.time,
) -> tuple[Service, Service]:
    """`service rediscover <id> --confirm <id>` (BR-097): new values, audit before and after;
    the connections are not touched."""
    before = _service(conn, service_id)
    if confirm is None or confirm != service_id:
        raise Refused(f"confirmation required: --confirm {service_id}")
    found = _discover(out, before.model, before.base_url, internal_of(before))
    with transaction(conn):
        now = clock()
        current = _service(conn, service_id)
        conn.execute(
            "UPDATE service SET endpoints = ?, resource = ? WHERE id = ?",
            (json.dumps(found.endpoints, sort_keys=True), found.resource, service_id),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_SERVICE_REDISCOVER,
            result="ok",
            path=service_id,
            before={"endpoints": current.endpoints, "resource": current.resource},
            after={"endpoints": found.endpoints, "resource": found.resource},
            now=now,
        )
        return current, _service(conn, service_id)


def validate_mcp_url(url: str, internal: bool) -> str:
    """MCP URL of a service (BR-106): https (or http with the internal-host exception), without
    credentials, query or fragment. A refusal never repeats the URL (spirit of BR-086)."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise Refused("invalid MCP URL (URL not repeated): unreadable form") from None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise Refused("invalid MCP URL (URL not repeated): credentials in the URL refused")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise Refused("invalid MCP URL (URL not repeated): https:// and a host expected")
    if parts.scheme == "http" and not internal:
        raise Refused("invalid MCP URL (URL not repeated): http:// only with --mcp-internal-host")
    if parts.query or parts.fragment or "?" in url or "#" in url or port == 0:
        raise Refused("invalid MCP URL (URL not repeated): neither query nor fragment")
    return url.strip()


@dataclass(frozen=True, slots=True)
class UpstreamDeclared:
    service: Service
    before: Service
    addresses: tuple[str, ...]  # addresses admitted at declaration (BR-106)
    changed: bool


def service_upstream(
    conn: sqlite3.Connection,
    transport: Any,
    service_id: str,
    kind: str,
    url: str,
    internal: bool,
    confirm: str | None,
    *,
    clock: Clock = time.time,
) -> UpstreamDeclared:
    """`service upstream <id> --type … --mcp-url … [--mcp-internal-host]` (BR-106): the MCP URL is
    checked (form, then resolved addresses under the relay's policy, `transport.addresses`) and
    fixed; changing an already declared URL requires `--confirm <id>`. Audited before and after,
    with the admitted addresses."""
    from hullgate.upstream_mcp import Target, UpstreamError

    if kind not in UPSTREAM_TYPES:
        raise Refused(f"unknown upstream type: {kind} ({' or '.join(UPSTREAM_TYPES)})")
    if kind == "mcp-internal" and not internal:
        raise Refused(
            "mcp-internal: --mcp-internal-host required (a stdio bridge is an internal host)"
        )
    mcp_url = validate_mcp_url(url, internal)
    before = _service(conn, service_id)
    if kind == "mcp-internal":
        oauth = sorted(
            display_name(Tree.load(conn).nodes, r)
            for r in _live_rows(conn)
            if r.service_id == service_id and r.kind == "oauth"
        )
        if oauth:
            raise Refused(
                f"mcp-internal: provided-token connections only ({', '.join(oauth)} is OAuth)"
            )
    same = (before.upstream_type, before.mcp_url, before.mcp_internal) == (kind, mcp_url, internal)
    if before.upstream_type is not None and not same and confirm != service_id:
        raise Refused(f"upstream already declared: change with --confirm {service_id}")
    try:
        addresses = transport.addresses(Target(mcp_url, internal))
    except UpstreamError as exc:
        raise Refused(f"MCP URL refused: {exc.reason} (nothing was written)") from None
    with transaction(conn):
        now = clock()
        current = _service(conn, service_id)
        if current != before:
            raise Refused(f"service {service_id} modified in the meantime (replay)")
        if not same:
            conn.execute(
                "UPDATE service SET upstream_type = ?, mcp_url = ?, mcp_internal = ? WHERE id = ?",
                (kind, mcp_url, int(internal), service_id),
            )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_SERVICE_UPSTREAM,
            result="ok" if not same else "unchanged",
            path=service_id,
            before=(
                None
                if before.upstream_type is None
                else {
                    "type": before.upstream_type,
                    "mcp_url": before.mcp_url,
                    "internal": before.mcp_internal,
                }
            ),
            after={
                "type": kind,
                "mcp_url": mcp_url,
                "internal": internal,
                "admitted_addresses": addresses,
            },
            now=now,
        )
        return UpstreamDeclared(_service(conn, service_id), before, tuple(addresses), not same)


def service_upstream_remove(
    conn: sqlite3.Connection, service_id: str, confirm: str | None, *, clock: Clock = time.time
) -> Service:
    """`service upstream <id> --remove --confirm <id>` (BR-106): the upstream becomes NULL again;
    the service's connections bring no tool any more, and the server closes their sessions at the
    next request (checked against the database, BR-119)."""
    with transaction(conn):
        now = clock()
        before = _service(conn, service_id)
        if confirm != service_id:
            raise Refused(f"confirmation required: --confirm {service_id}")
        if before.upstream_type is None:
            raise Refused(f"service {service_id}: {UPSTREAM_UNDECLARED}")
        conn.execute(
            "UPDATE service SET upstream_type = NULL, mcp_url = NULL, mcp_internal = 0"
            " WHERE id = ?",
            (service_id,),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_SERVICE_UPSTREAM_REMOVE,
            result="ok",
            path=service_id,
            before={
                "type": before.upstream_type,
                "mcp_url": before.mcp_url,
                "internal": before.mcp_internal,
            },
            now=now,
        )
        return before


def service_list(conn: sqlite3.Connection) -> list[Service]:
    return _services(conn)


def service_remove(conn: sqlite3.Connection, service_id: str, *, clock: Clock = time.time) -> None:
    with transaction(conn):
        now = clock()
        service = _service(conn, service_id)
        apps = [a.id for a in _applications(conn) if a.service_id == service_id]
        if apps:
            raise Refused(f"service referenced by applications: {', '.join(apps)}")
        tree = Tree.load(conn)
        users = sorted(
            display_name(tree.nodes, r) for r in _live_rows(conn) if r.service_id == service_id
        )
        if users:
            raise Refused(f"service referenced by connections: {', '.join(users)}")
        conn.execute("DELETE FROM tool_catalog WHERE service_id = ?", (service_id,))  # BR-173
        conn.execute("DELETE FROM service WHERE id = ?", (service_id,))
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_SERVICE_REMOVE,
            result="ok",
            path=service_id,
            before={"model": service.model, "base_url": service.base_url},
            now=now,
        )


# ---------------------------------------------------------------------------------------------
# Applications (BR-081)
# ---------------------------------------------------------------------------------------------


def application_declare(
    conn: sqlite3.Connection,
    key: ConnectionsKey,
    app_id: str,
    service_id: str,
    client_id: str,
    secret: str | None,
    *,
    clock: Clock = time.time,
    mode: str = "declared",
) -> Application:
    """Application declared by hand (mode `declared`); its secret, if any, encrypted (BR-087)."""
    if mode not in ("declared", "dynamic"):
        raise Refused("invalid application mode")
    validate_id("application", app_id)
    if not client_id or len(client_id) > 255 or any(c.isspace() for c in client_id):
        raise Refused("invalid client_id: 1 to 255 characters, no space")
    with transaction(conn):
        now = clock()
        sealer = key.sealer(conn) if secret is not None else None
        _service(conn, service_id)
        if conn.execute("SELECT 1 FROM application WHERE id = ?", (app_id,)).fetchone():
            raise Refused(f"application already declared: {app_id}")
        sealed = (
            sealer.seal(conn, "application", app_id, "secret", secret)
            if sealer is not None and secret is not None
            else None
        )
        conn.execute(
            "INSERT INTO application (id, service_id, mode, client_id, redirect_uri, secret_enc,"
            " auth_method, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                app_id,
                service_id,
                mode,
                client_id,
                REDIRECT_URI,
                sealed,
                "client_secret_basic" if sealed is not None else "none",
                int(now),
            ),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_APP_DECLARE,
            result="ok",
            path=app_id,
            after={
                "service": service_id,
                "mode": mode,
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "secret": sealed is not None,
            },
            now=now,
        )
    return next(a for a in _applications(conn) if a.id == app_id)


def application_list(conn: sqlite3.Connection) -> list[Application]:
    return _applications(conn)


def application_web_return(
    conn: sqlite3.Connection, app_id: str, registered: bool, *, clock: Clock = time.time
) -> Application:
    """BR-216: the operator says the console's return URI is registered at the provider (or no
    longer is); authorisations opened afterwards use it. Audited with before and after."""
    with transaction(conn):
        now = clock()
        app = next((a for a in _applications(conn) if a.id == app_id), None)
        if app is None:
            raise Refused(f"unknown application: {app_id}")
        if app.web_return != registered:
            conn.execute(
                "UPDATE application SET web_return = ? WHERE id = ?", (int(registered), app_id)
            )
            audit.write(
                conn,
                actor=audit.actor(),
                action=A_APP_WEB_RETURN,
                result="ok",
                path=app_id,
                before={"web_return": app.web_return},
                after={"web_return": registered},
                now=now,
            )
    return next(a for a in _applications(conn) if a.id == app_id)


def application_remove(conn: sqlite3.Connection, app_id: str, *, clock: Clock = time.time) -> None:
    """Refused while a live connection references it (BR-081); its secret goes with it."""
    with transaction(conn):
        now = clock()
        app = next((a for a in _applications(conn) if a.id == app_id), None)
        if app is None:
            raise Refused(f"unknown application: {app_id}")
        tree = Tree.load(conn)
        users = sorted(
            display_name(tree.nodes, r) for r in _live_rows(conn) if r.application_id == app_id
        )
        if users:
            raise Refused(f"application referenced by connections: {', '.join(users)}")
        conn.execute("DELETE FROM application WHERE id = ?", (app_id,))
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_APP_REMOVE,
            result="ok",
            path=app_id,
            before={"service": app.service_id, "client_id": app.client_id},
            now=now,
        )


# ---------------------------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------------------------


def _tokens_json(value: str) -> str:
    return json.dumps({"access_token": value, "token_type": "Bearer"})


def _check_new(
    conn: sqlite3.Connection, tree: Tree, text: str, service_id: str, prefix: str
) -> tuple[Node, str]:
    client_slug, name = parse_name(text)
    validate_prefix(prefix)
    client = next((k for k in tree.kids(tree.org) if k.slug == client_slug), None)
    if client is None:
        raise Refused(f"unknown client: {client_slug}")
    _service(conn, service_id)
    if conn.execute(
        "SELECT 1 FROM connection WHERE removed = 0 AND client_node_id = ? AND name = ?",
        (client.id, name),
    ).fetchone():
        raise Refused(f"connection already exists: {client_slug}/{name}")
    if conn.execute(
        "SELECT 1 FROM connection WHERE removed = 0 AND prefix = ?", (prefix,)
    ).fetchone():
        raise Refused(f"prefix already taken: {prefix}")
    return client, name


@dataclass(frozen=True, slots=True)
class Added:
    name: str
    kind: str
    state: str
    url: str | None = None  # authorization URL (OAuth), to open in the browser
    redirect: str | None = None  # BR-216: "web" (console return) or "paste" (F-006), OAuth only


WEB_CALLBACK_PATH = "/admin/oauth/callback"  # BR-216, served by the console


def web_redirect_uri(admin_origin: str) -> str:
    """`${ADMIN_URL}/admin/oauth/callback` (BR-216)."""
    return f"{admin_origin.rstrip('/')}{WEB_CALLBACK_PATH}"


def redirect_for(app: Application, web_redirect: str | None) -> tuple[str, str]:
    """The `redirect_uri` an authorisation opens with (BR-082 v3) and its kind: the console's
    return URI when the caller offers one and the application has it registered (BR-216), the
    application's own URI otherwise."""
    if web_redirect is not None and app.web_return:
        return web_redirect, "web"
    return app.redirect_uri, "paste"


def connection_for_state(conn: sqlite3.Connection, state: str) -> str | None:
    """The connection whose open authorisation carries this `state` (BR-216's callback), by its
    digest; None when no pending authorisation matches."""
    row = conn.execute(
        "SELECT c.id FROM pending_auth p JOIN connection c ON c.id = p.connection_id"
        " WHERE c.removed = 0 AND p.state_hash = ?",
        (state_hash(state),),
    ).fetchone()
    if row is None:
        return None
    tree = Tree.load(conn)
    found = next((r for r in _live_rows(conn) if r.id == row[0]), None)
    return None if found is None else display_name(tree.nodes, found)


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _open_authorization(
    conn: sqlite3.Connection,
    sealer: Sealer,
    row_id: str,
    service: Service,
    app: Application,
    now: float,
    redirect_uri: str | None = None,
) -> str:
    """Opens the outbound authorization of a connection (BR-082) in the caller's transaction:
    random single-use `state` (only its digest is kept), encrypted PKCE verifier, 10 minutes;
    replaces the open authorization if there is one. Returns the URL to print. The
    `redirect_uri` chosen here is kept with it and sent again at the exchange (BR-082 v3)."""
    chosen = app.redirect_uri if redirect_uri is None else redirect_uri
    endpoint = service.endpoints.get("authorization_endpoint")
    if endpoint is None or "token_endpoint" not in service.endpoints:
        raise Refused(
            f"service {service.id} without discovered endpoints: "
            f"service rediscover {service.id} --confirm {service.id}"
        )
    state = secrets.token_urlsafe(32)
    pkce = upstream.Pkce.new()
    sealed = sealer.seal(conn, "pending_auth", row_id, "verifier", pkce.verifier)
    conn.execute("DELETE FROM pending_auth WHERE connection_id = ?", (row_id,))
    conn.execute(
        "INSERT INTO pending_auth (connection_id, state_hash, verifier_enc, expires_at, used,"
        " redirect_uri) VALUES (?, ?, ?, ?, 0, ?)",
        (row_id, state_hash(state), sealed, int(now) + PENDING_TTL_S, chosen),
    )
    resource = service.resource if service.model == "generic" else None
    return upstream.authorization_url(
        endpoint, app.client_id, chosen, service.scopes, state, pkce.challenge, resource
    )


def add_oauth(
    conn: sqlite3.Connection,
    key: ConnectionsKey,
    text: str,
    service_id: str,
    application_id: str,
    prefix: str,
    *,
    clock: Clock = time.time,
    web_redirect: str | None = None,
) -> Added:
    """OAuth connection `pending` (BR-074, BR-081) and opening of its outbound authorization
    (BR-082) in the same transaction; returns the authorization URL. `web_redirect`: the
    console's return URI, used when the application has it registered (BR-216)."""
    with transaction(conn):
        now = clock()
        sealer = key.sealer(conn)  # list "refused" without a key (§3.2, BR-088)
        tree = Tree.load(conn)
        client, name = _check_new(conn, tree, text, service_id, prefix)
        app = next((a for a in _applications(conn) if a.id == application_id), None)
        if app is None:
            raise Refused(f"unknown application: {application_id}")
        if app.service_id != service_id:
            raise Refused(
                f"application {application_id} declared for the service {app.service_id}, "
                f"not {service_id}"
            )
        if _service(conn, service_id).upstream_type == "mcp-internal":
            raise Refused(f"service {service_id} mcp-internal: provided-token connection only")
        cid = _new_id()
        conn.execute(
            "INSERT INTO connection (id, client_node_id, name, prefix, service_id,"
            " application_id, kind, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'oauth', 'pending', ?)",
            (cid, client.id, name, prefix, service_id, application_id, int(now)),
        )
        chosen, redirect = redirect_for(app, web_redirect)
        url = _open_authorization(conn, sealer, cid, _service(conn, service_id), app, now, chosen)
        display = f"{client.slug}/{name}"
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_CONN_ADD,
            result="ok",
            path=display,
            after={
                "kind": "oauth",
                "service": service_id,
                "application": application_id,
                "prefix": prefix,
                "state": "pending",
                "authorization_opened": True,
            },
            now=now,
        )
        return Added(display, "oauth", "pending", url, redirect)


@dataclass(frozen=True, slots=True)
class Reopened:
    name: str
    state: str
    url: str
    redirect: str = "paste"


def reopen(
    conn: sqlite3.Connection,
    key: ConnectionsKey,
    text: str,
    *,
    clock: Clock = time.time,
    web_redirect: str | None = None,
) -> Reopened:
    """`connection reauthorize <c>` (OAuth): new authorization opened, the old `state`
    invalid; an `active` connection stays served until the exchange (BR-082), a connection
    `reauthorization required` goes back to `pending` (§6.4)."""
    with transaction(conn):
        now = clock()
        sealer = key.sealer(conn)
        tree = Tree.load(conn)
        row = _find(conn, tree, text)
        name = display_name(tree.nodes, row)
        if row.kind != "oauth" or row.application_id is None:
            raise Refused(f"reauthorize: {name} is a provided-token connection (add --file <f|->)")
        app = next(a for a in _applications(conn) if a.id == row.application_id)
        chosen, redirect = redirect_for(app, web_redirect)
        url = _open_authorization(
            conn, sealer, row.id, _service(conn, row.service_id), app, now, chosen
        )
        state = "pending" if row.state == "reauth_required" else row.state
        conn.execute("UPDATE connection SET state = ? WHERE id = ?", (state, row.id))
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_AUTHORIZE_OPEN,
            result="ok",
            path=name,
            before={"state": row.state},
            after={"state": state},
            now=now,
        )
        return Reopened(name, state, url, redirect)


def provided_expiry(value: str | None) -> int | None:
    """An operator-known expiry, never inferred from an opaque token (BR-238)."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        end = int(parsed.timestamp())
        audit.utc(end)  # The offset must not move the expiry outside the UTC calendar.
        return end
    except (ValueError, OverflowError):
        raise Refused("expires_at: ISO 8601 timestamp with timezone expected") from None


def add_static(
    conn: sqlite3.Connection,
    key: ConnectionsKey,
    text: str,
    service_id: str,
    value: str,
    prefix: str,
    account: str | None,
    *,
    clock: Clock = time.time,
    expires_at: str | None = None,
) -> Added:
    """Provided-token connection (BR-085): `active` at once, value encrypted (BR-087)."""
    end = provided_expiry(expires_at)
    if account is not None:
        validate_account(account)
    with transaction(conn):
        now = clock()
        sealer = key.sealer(conn)
        tree = Tree.load(conn)
        client, name = _check_new(conn, tree, text, service_id, prefix)
        cid = _new_id()
        sealed = sealer.seal(conn, "connection", cid, "tokens", _tokens_json(value))
        conn.execute(
            "INSERT INTO connection (id, client_node_id, name, prefix, service_id, kind,"
            " account, state, tokens_enc, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, 'provided', ?, 'active', ?, ?, ?)",
            (cid, client.id, name, prefix, service_id, account, sealed, int(now), end),
        )
        display = f"{client.slug}/{name}"
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_CONN_ADD,
            result="ok",
            path=display,
            after={
                "kind": "provided",
                "service": service_id,
                "prefix": prefix,
                "account": account,
                "state": "active",
                "expires_at": audit.utc(end) if end is not None else None,
            },
            now=now,
        )
        return Added(display, "provided", "active")


def replace_static(
    conn: sqlite3.Connection,
    key: ConnectionsKey,
    text: str,
    value: str,
    account: str | None,
    *,
    clock: Clock = time.time,
    expires_at: str | None = None,
) -> Row:
    """`reauthorize --file` (BR-085, Q1 a): new value, `generation` + 1, `active`; name,
    prefix and placements kept; the old value is erased in the transaction."""
    end = provided_expiry(expires_at)
    if account is not None:
        validate_account(account)
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        row = _find(conn, tree, text)
        if row.kind != "provided":
            raise Refused(
                "--file: reserved for a provided-token connection "
                f"({display_name(tree.nodes, row)}"
                " is OAuth)"
            )
        sealer = key.sealer(conn)
        sealed = sealer.seal(conn, "connection", row.id, "tokens", _tokens_json(value))
        new_account = row.account if account is None else account
        conn.execute(
            "UPDATE connection SET tokens_enc = ?, state = 'active', account = ?,"
            " generation = generation + 1, expires_at = ?, expiry_estimated = 0 WHERE id = ?",
            (sealed, new_account, end, row.id),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_CONN_REPLACE,
            result="ok",
            path=display_name(tree.nodes, row),
            before={"state": row.state, "account": row.account, "generation": row.generation},
            after={
                "state": "active",
                "account": new_account,
                "generation": row.generation + 1,
                "expires_at": audit.utc(end) if end is not None else None,
            },
            now=now,
        )
        found = _find(conn, tree, text)
    return found


def set_prefix(
    conn: sqlite3.Connection, text: str, prefix: str, *, clock: Clock = time.time
) -> str:
    """Changes the prefix as long as no placement exists (BR-076)."""
    validate_prefix(prefix)
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        row = _find(conn, tree, text)
        if _placements(conn, row.id):
            raise Refused(
                f"prefix fixed: {display_name(tree.nodes, row)} is placed (remove its placements)"
            )
        if row.prefix == prefix:
            return prefix
        if conn.execute(
            "SELECT 1 FROM connection WHERE removed = 0 AND prefix = ? AND id != ?",
            (prefix, row.id),
        ).fetchone():
            raise Refused(f"prefix already taken: {prefix}")
        conn.execute("UPDATE connection SET prefix = ? WHERE id = ?", (prefix, row.id))
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_CONN_PREFIX,
            result="ok",
            path=display_name(tree.nodes, row),
            before={"prefix": row.prefix},
            after={"prefix": prefix},
            detail={"connection_id": row.id},
            now=now,
        )
        return prefix


# ---------------------------------------------------------------------------------------------
# Placements (BR-077, BR-078, BR-049 clarified)
# ---------------------------------------------------------------------------------------------


class ShareRefused(Refused):
    """BR-078 v3: a share placed without its confirmation (CLI code 2, API 409
    `share_confirmation_required`); `reasons` are `other_client` and `sensitivity`. The text
    names no CLI option: the CLI appends its own (`: add --confirm-share`)."""

    def __init__(self, message: str, reasons: Sequence[str]) -> None:
        super().__init__(message)
        self.reasons = tuple(reasons)


@dataclass(frozen=True, slots=True)
class Placed:
    connection: str
    role: str
    sensitive: bool
    confirmed: tuple[str, ...]  # sharing reasons confirmed by --confirm-share


def _role(tree: Tree, path: str) -> Node:
    node = tree.resolve(path)
    if node.level != "role":
        raise Refused(
            f"placement: a connection is placed on a role ({tree.path(node)} is of level "
            f"{'organisation' if node.level == 'org' else node.level})"
        )
    return node


def _refuse_if_role_locked(tree: Tree, role: Node, what: str) -> None:
    locker = tree.locked_by(role)
    if locker is not None:
        raise Refused(f"locked by {tree.path(locker)}: {what} on {tree.path(role)}")


def place(
    conn: sqlite3.Connection,
    text: str,
    role_path: str,
    confirm_share: bool,
    *,
    clock: Clock = time.time,
) -> Placed:
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        row = _find(conn, tree, text)
        role = _role(tree, role_path)
        name = display_name(tree.nodes, row)
        _refuse_if_role_locked(tree, role, "placement")
        existing = [tree.nodes[i] for i in _placements(conn, row.id) if i in tree.nodes]
        if any(r.id == role.id for r in existing):
            raise Refused(f"already placed: {name} on {tree.path(role)}")
        sensitive = tree.is_sensitive(role)
        reasons: list[str] = []
        client = client_of(tree.nodes, role)
        if client is None or client.id != row.client_node_id:
            reasons.append(SHARE_CLIENT)
        others = [r for r in existing if tree.is_sensitive(r) != sensitive]
        if others:
            reasons.append(SHARE_SENSITIVITY)
        if reasons and not confirm_share:
            texts = {
                SHARE_CLIENT: (
                    f"sharing outside the origin client ({tree.nodes[row.client_node_id].slug})"
                ),
                SHARE_SENSITIVITY: "sharing between a sensitive and a non-sensitive role",
            }
            raise ShareRefused("; ".join(texts[r] for r in reasons), reasons)
        confirmed_by = audit.actor() if reasons else None
        conn.execute(
            "INSERT INTO placement (connection_id, role_id, created_at, share_confirmed_by,"
            " share_confirmed_at) VALUES (?, ?, ?, ?, ?)",
            (row.id, role.id, int(now), confirmed_by, int(now) if reasons else None),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_PLACE,
            result="ok",
            node_id=role.id,
            path=tree.path(role),
            before=None,
            after=placement_document(conn, row.id, role.id),
            detail={
                "connection": name,
                "connection_id": row.id,
                "prefix": row.prefix,
                "sensitive": sensitive,
            },
            now=now,
        )
        if reasons:
            audit.write(
                conn,
                actor=audit.actor(),
                action=A_SHARE_CONFIRMED,
                result="ok",
                node_id=role.id,
                path=tree.path(role),
                reason=",".join(reasons),
                detail={
                    "connection": name,
                    "origin_client": tree.nodes[row.client_node_id].slug,
                    "role": tree.path(role),
                    "role_sensitive": sensitive,
                    "other_roles": [
                        {"role": tree.path(r), "sensitive": tree.is_sensitive(r)}
                        for r in sorted(existing, key=tree.path)
                    ],
                },
                now=now,
            )
        return Placed(name, tree.path(role), sensitive, tuple(reasons))


def unplace(
    conn: sqlite3.Connection, text: str, role_path: str, *, clock: Clock = time.time
) -> str:
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        row = _find(conn, tree, text)
        role = _role(tree, role_path)
        name = display_name(tree.nodes, row)
        if role.id not in _placements(conn, row.id):
            raise Refused(f"not placed: {name} on {tree.path(role)}")
        _refuse_if_role_locked(tree, role, "placement removal")
        before = placement_document(conn, row.id, role.id)
        erase_policy(conn, row.id, role.id)
        conn.execute(
            "DELETE FROM placement WHERE connection_id = ? AND role_id = ?", (row.id, role.id)
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_UNPLACE,
            result="ok",
            node_id=role.id,
            path=tree.path(role),
            before=before,
            after=None,
            detail={"connection": name, "connection_id": row.id},
            now=now,
        )
        return tree.path(role)


# ---------------------------------------------------------------------------------------------
# Views (BR-080)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class View:
    name: str
    service: str
    kind: str
    application: str | None
    prefix: str
    account: str
    scopes: tuple[str, ...]
    state: str  # label; `key …` when the key is unavailable (overlay, BR-088)
    stored_state: str
    key: str | None
    expires_at: int | None
    expiry_estimated: bool
    not_before: int | None
    last_outcome: str | None
    placements: tuple[tuple[str, bool], ...] = field(default=())
    upstream: str = UPSTREAM_UNDECLARED  # F-008 §3.2: "<type> <host>" or not declared
    refusal: str | None = None  # BR-127: `mcp_refusal_401`
    # BR-092: `invalid_client` or `unauthorized_client` — the application to declare again or
    # register again (reauthorizing alone is not enough).
    redeclare: str | None = None
    expiring: bool = False
    access_ends_at: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "service": self.service,
            "type": self.kind,
            "application": self.application,
            "prefix": self.prefix,
            "account": self.account,
            "scopes": list(self.scopes),
            "state": self.state,
            "stored_state": self.stored_state,
            "key": self.key,
            "expires_at": audit.utc(self.expires_at) if self.expires_at is not None else None,
            "expiry_estimated": self.expiry_estimated,
            "not_before": audit.utc(self.not_before) if self.not_before is not None else None,
            "last_outcome": self.last_outcome,
            "placements": [{"role": p, "sensitive": s} for p, s in self.placements],
            "application_to_redeclare": self.redeclare,
            "upstream": self.upstream,
            "refusal": self.refusal,
            "expiring": self.expiring,
            "access_ends_at": audit.utc(self.access_ends_at)
            if self.access_ends_at is not None
            else None,
        }


def views(
    conn: sqlite3.Connection,
    key: ConnectionsKey,
    text: str | None = None,
    *,
    clock: Clock = time.time,
) -> list[View]:
    tree = Tree.load(conn)
    rows = [_find(conn, tree, text)] if text is not None else _live_rows(conn)
    key_reason = key.status(conn)
    services = {s.id: s for s in _services(conn)}
    threshold = int(tree.effective(tree.org, "connection_expiry_days")[0]) * 86400
    now = clock()
    out: list[View] = []
    for row in rows:
        roles = [tree.nodes[i] for i in _placements(conn, row.id) if i in tree.nodes]
        last = row.last_outcome if row.last_outcome in ("uncertain", "anomaly") else None
        end = row.expires_at if row.kind == "provided" else row.refresh_expires_at
        out.append(
            View(
                expiring=row.state == "active" and end is not None and end <= now + threshold,
                access_ends_at=end,
                name=display_name(tree.nodes, row),
                service=row.service_id,
                kind=row.kind,
                application=row.application_id,
                prefix=row.prefix,
                account=row.account or "unknown",
                scopes=tuple(row.granted_scopes.split()),
                state=key_reason if key_reason is not None else row.state,
                stored_state=row.state,
                key=key_reason,
                expires_at=row.expires_at,
                expiry_estimated=row.expiry_estimated,
                not_before=row.not_before,
                last_outcome=last,
                redeclare=(
                    row.application_id
                    if row.state == "reauth_required"
                    and row.last_reason in upstream.APPLICATION_ERRORS
                    else None
                ),
                placements=tuple(sorted((tree.path(r), tree.is_sensitive(r)) for r in roles)),
                upstream=(
                    services[row.service_id].upstream_label()
                    if row.service_id in services
                    else UPSTREAM_UNDECLARED
                ),
                refusal=(
                    row.last_reason
                    if row.state == "reauth_required" and row.last_reason == MCP_REFUSAL_REASON
                    else None
                ),
            )
        )
    return sorted(out, key=lambda v: v.name)


@dataclass(frozen=True, slots=True)
class ExportedConnection:
    name: str
    service: str
    prefix: str
    application: str | None
    state: str
    placements: tuple[str, ...]
    id: str = ""  # internal identifier, never written: the export reads each placement's policy


def exported(
    conn: sqlite3.Connection,
) -> tuple[list[Service], list[Application], list[ExportedConnection]]:
    """Services, applications and connections for the export (BR-070 v2), without secrets."""
    tree = Tree.load(conn)
    conns = [
        ExportedConnection(
            display_name(tree.nodes, r),
            r.service_id,
            r.prefix,
            r.application_id,
            r.state,
            tuple(
                sorted(tree.path(tree.nodes[i]) for i in _placements(conn, r.id) if i in tree.nodes)
            ),
            r.id,
        )
        for r in _live_rows(conn)
    ]
    return _services(conn), _applications(conn), sorted(conns, key=lambda c: c.name)


# ---------------------------------------------------------------------------------------------
# Internal contract toward the relay (§3.3, SCR-03)
# ---------------------------------------------------------------------------------------------


class AccessError(Exception):
    """Typed error of `get_access`; `code` is the contract value (§3.3)."""

    code = "access_error"


class NotPlaced(AccessError):
    code = "access_not_placed"


class NotAuthorized(AccessError):
    code = "access_not_authorized"


class ReauthRequired(AccessError):
    code = "access_reauth_required"


class TemporarilyUnavailable(AccessError):
    code = "access_temporarily_unavailable"

    def __init__(self, retry_after: float | None) -> None:
        super().__init__(self.code)
        self.retry_after = retry_after


class KeyUnavailableError(AccessError):
    code = "access_key_unavailable"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class IntegrityErrorAccess(AccessError):
    code = "access_integrity_error"


@dataclass(frozen=True, slots=True, repr=False)
class Access:
    """Ready-to-use `Authorization` header and generation of the token set; never displayed."""

    authorization: str
    generation: int
    expires_at: int | None

    def __repr__(self) -> str:
        return f"Access(<redacted>, generation={self.generation})"

    __str__ = __repr__


def get_access_sync(
    conn: sqlite3.Connection,
    key: ConnectionsKey,
    connection_id: str,
    role_id: str,
    *,
    out: Outbound,
    clock: Clock = time.time,
) -> Access:
    """Valid token of a connection placed on a role, or a typed error (§3.3). Blocking call, on
    a SQLite connection of the caller's own; renews under lease if needed (BR-089 to BR-091).
    `out`: the process's outbound client, never built at call time (BR-125).
    Implementation: `hullgate.lifecycle`."""
    from hullgate import lifecycle

    return lifecycle.access(lifecycle.Direct(conn), key, out, connection_id, role_id, clock=clock)


async def get_access(
    db: Database,
    key: ConnectionsKey,
    connection_id: str,
    role_id: str,
    *,
    out: Outbound,
    clock: Clock = time.time,
) -> Access:
    """Asynchronous form given to the relay (HULL-9): `get_access(connection_id, role_id)`. The
    process's database lock is held only during the short transactions, never during the call to
    the provider (BR-089)."""
    from hullgate import lifecycle

    return await lifecycle.get_access(db, key, connection_id, role_id, clock=clock, out=out)


async def signal_refusal(
    db: Database,
    key: ConnectionsKey,
    connection_id: str,
    generation: int,
    cause: str,
    *,
    clock: Clock = time.time,
) -> None:
    """`signal_refusal(connection_id, generation, cause)` (§3.3), called by HULL-9 on upstream
    401."""
    from hullgate import lifecycle

    await lifecycle.signal_refusal(db, key, connection_id, generation, cause, clock=clock)


def placement_document(conn: sqlite3.Connection, cid: str, rid: str) -> dict[str, Any] | None:
    """Declarative placement state, with raw tool names; no secrets or pinning baseline."""
    row = conn.execute(
        "SELECT mode, pin FROM placement WHERE connection_id = ? AND role_id = ?", (cid, rid)
    ).fetchone()
    if row is None:
        return None
    doc: dict[str, Any] = {"mode": row[0], "pin": row[1]}
    for which in ("open", "deny", "allowlist"):
        doc[which] = [
            r[0]
            for r in conn.execute(
                "SELECT upstream_name FROM placement_tool WHERE connection_id = ? AND role_id = ?"
                " AND list = ? ORDER BY upstream_name",
                (cid, rid, which),
            )
        ]
    doc["pinned_tools"] = [
        r[0]
        for r in conn.execute(
            "SELECT upstream_name FROM pinned_definition WHERE connection_id = ? AND role_id = ?"
            " AND tool_pin = 1 ORDER BY upstream_name",
            (cid, rid),
        )
    ]
    doc["arguments"] = [
        {"tool": r[0], "argument": r[1], "values": json.loads(r[2])}
        for r in conn.execute(
            "SELECT upstream_name, argument, allowed_values FROM argument_rule"
            " WHERE connection_id = ? AND role_id = ? ORDER BY upstream_name",
            (cid, rid),
        )
    ]
    return doc


def import_connection(
    conn: sqlite3.Connection,
    text: str,
    service: str,
    prefix: str,
    application: str | None,
    *,
    clock: Clock = time.time,
) -> Added:
    """BR-236: restore a library declaration, without credentials or an OAuth exchange."""
    with transaction(conn):
        t = Tree.load(conn)
        client, name = _check_new(conn, t, text, service, prefix)
        if application is not None:
            app = next((a for a in _applications(conn) if a.id == application), None)
            if app is None or app.service_id != service:
                raise Refused("application does not belong to the connection's service")
        kind = "oauth" if application is not None else "provided"
        conn.execute(
            "INSERT INTO connection (id, client_node_id, name, prefix, service_id,"
            " application_id, kind, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?,"
            " 'reauth_required', ?)",
            (_new_id(), client.id, name, prefix, service, application, kind, int(clock())),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_CONN_ADD,
            result="ok",
            path=text,
            after={"kind": kind, "service": service, "prefix": prefix, "state": "reauth_required"},
            now=clock(),
        )
        return Added(text, kind, "reauth_required")
