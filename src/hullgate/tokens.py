"""Operator tokens (BR-050 to BR-053, BR-072) and the list shared with OAuth lineages.

An operator token is an opaque value `hg_` + 256 bits (base64url), placed on a node; the
database keeps only its SHA-256 digest. The value is returned only once, by `issue`.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass

from hullgate import audit
from hullgate.authorization import Credential
from hullgate.db import transaction
from hullgate.tree import DAY, Clock, Refused, Tree, parse_duration, validate_name

PREFIX = "hg_"
SENSITIVE_CAP = 7 * DAY
ROOT_CONFIRM = "*"


def new_value() -> str:
    raw = secrets.token_bytes(32)
    return PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_id() -> str:
    return "tok_" + secrets.token_hex(6)


@dataclass(frozen=True, slots=True)
class Issued:
    id: str
    value: str
    path: str
    label: str
    issued_at: int
    expires_at: int
    capped: bool  # duration brought down to the sensitive cap (BR-052)
    uncovered_sensitive: tuple[str, ...]  # sensitive environments the token does not cover


def ttl_for(tree: Tree, path: str, duration: str | None) -> tuple[int, bool]:
    """Duration of an operator token on `path`: `--duration` or `token_duration`, capped
    (BR-052)."""
    node = tree.resolve(path)
    if duration is not None:
        seconds = parse_duration(duration)
    else:
        seconds = parse_duration(tree.effective(node, "token_duration")[0])
    if tree.is_sensitive(node) and seconds > SENSITIVE_CAP:
        return SENSITIVE_CAP, True
    return seconds, False


def issue(
    conn: sqlite3.Connection,
    path: str,
    label: str,
    duration: str | None = None,
    confirm: str | None = None,
    *,
    clock: Clock = time.time,
) -> Issued:
    """Issues an operator token; the issuance and its audit form one transaction (BR-065).

    The lock does not apply to issuance (H11).
    """
    validate_name(label)
    if duration is not None:
        parse_duration(duration)
    value = new_value()
    token_id = _new_id()
    with transaction(conn):
        now = int(clock())
        tree = Tree.load(conn)
        node = tree.resolve(path)
        node_path = tree.path(node)
        root = node.level == "org"
        if root and confirm != ROOT_CONFIRM:
            raise Refused(
                "root token: --confirm '*' required (every client outside sensitive "
                "environments; reserved for administration, not advised for an agent)"
            )
        if node.level == "env" and node.sensitive:
            raise Refused(
                f"a token placed on a sensitive environment is accepted on none of its "
                f"roles: {node_path} (place the token on a role)"
            )
        seconds, capped = ttl_for(tree, node_path, duration)
        expires_at = now + seconds
        conn.execute(
            "INSERT INTO operator_token (id, node_id, hash, label, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (token_id, node.id, digest(value), label, now, expires_at),
        )
        uncovered = (
            tuple(tree.path(e) for e in tree.sensitive_envs_below(node))
            if node.level in ("org", "client", "project")
            else ()
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action="token.issue",
            result="ok",
            node_id=node.id,
            path=node_path,
            after={"token_id": token_id, "label": label, "expires_at": audit.utc(expires_at)},
            detail={"root_token": True} if root else ({"capped": True} if capped else None),
            now=now,
        )
    return Issued(token_id, value, node_path, label, now, expires_at, capped, uncovered)


# ---------------------------------------------------------------------------------------------
# Listing and revocation (operator tokens and OAuth lineages)
# ---------------------------------------------------------------------------------------------

DELETED = "(deleted)"


@dataclass(frozen=True, slots=True)
class Listed:
    id: str
    kind: str  # "operator" or "oauth"
    holder: str  # label of the operator token or OAuth client_id
    path: str
    issued_at: int
    expires_at: int
    state: str  # "active", "revoked", "expired"

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.kind,
            "holder": self.holder,
            "path": self.path,
            "issued_at": audit.utc(self.issued_at),
            "expires_at": audit.utc(self.expires_at),
            "state": self.state,
        }


def _state(revoked: int, expires_at: int, now: float) -> str:
    if revoked:
        return "revoked"
    if expires_at <= now:
        return "expired"
    return "active"


def list_tokens(
    conn: sqlite3.Connection,
    path: str | None = None,
    client_segment: str | None = None,
    *,
    clock: Clock = time.time,
) -> list[Listed]:
    """Operator tokens and OAuth lineages; never a value or a digest (BR-053)."""
    now = clock()
    tree = Tree.load(conn)
    scope: set[str] | None = None
    if path is not None:
        scope = {n.id for n in tree.subtree(tree.resolve(path))}
    if client_segment is not None:
        client = next((k for k in tree.kids(tree.org) if k.slug == client_segment), None)
        if client is None:
            raise Refused(f"unknown client: {client_segment}")
        ids = {n.id for n in tree.subtree(client)}
        scope = ids if scope is None else scope & ids
    out: list[Listed] = []
    rows = conn.execute(
        "SELECT id, node_id, label, issued_at, expires_at, revoked FROM operator_token"
    ).fetchall()
    for tid, node_id, label, issued, expires, revoked in rows:
        if scope is not None and node_id not in scope:
            continue
        where = tree.path(tree.nodes[node_id]) if node_id in tree.nodes else DELETED
        out.append(
            Listed(tid, "operator", label, where, issued, expires, _state(revoked, expires, now))
        )
    rows = conn.execute(
        "SELECT id, role_id, client_id, created_at, expires_at, revoked FROM lineage"
    ).fetchall()
    for lid, role_id, client_id, created, expires, revoked in rows:
        if scope is not None and role_id not in scope:
            continue
        where = tree.path(tree.nodes[role_id]) if role_id in tree.nodes else DELETED
        out.append(
            Listed(lid, "oauth", client_id, where, created, expires, _state(revoked, expires, now))
        )
    out.sort(key=lambda t: (t.issued_at, t.id))
    return out


@dataclass(frozen=True, slots=True)
class Revoked:
    id: str
    kind: str
    already: bool


def revoke(conn: sqlite3.Connection, token_id: str, *, clock: Clock = time.time) -> Revoked:
    """Revokes an operator token or a whole OAuth lineage; outside the lock (H11)."""
    with transaction(conn):
        now = int(clock())
        tree = Tree.load(conn)
        row = conn.execute(
            "SELECT node_id, revoked FROM operator_token WHERE id = ?", (token_id,)
        ).fetchone()
        kind = "operator"
        if row is None:
            row = conn.execute(
                "SELECT role_id, revoked FROM lineage WHERE id = ?", (token_id,)
            ).fetchone()
            kind = "oauth"
        if row is None:
            raise Refused(f"unknown token: {token_id}")
        node_id, revoked = row
        if revoked:
            return Revoked(token_id, kind, True)
        if kind == "operator":
            conn.execute(
                "UPDATE operator_token SET revoked = 1, revoked_at = ? WHERE id = ?",
                (now, token_id),
            )
        else:
            conn.execute("UPDATE access_jti SET revoked = 1 WHERE lineage_id = ?", (token_id,))
            conn.execute(
                "UPDATE lineage SET revoked = 1, revoked_at = ? WHERE id = ?", (now, token_id)
            )
        audit.write(
            conn,
            actor=audit.actor(),
            action="token.revoke" if kind == "operator" else "lineage.revoke",
            result="ok",
            node_id=node_id,
            path=tree.path(tree.nodes[node_id]) if node_id in tree.nodes else DELETED,
            after={"token_id": token_id},
            now=now,
        )
        return Revoked(token_id, kind, False)


# ---------------------------------------------------------------------------------------------
# Resolution of a token presented at the access point (step 4 of BR-055)
# ---------------------------------------------------------------------------------------------


def resolve_operator(conn: sqlite3.Connection, bearer: str) -> Credential | None:
    """Operator token known by its digest, or None (token of another family, or unknown)."""
    if not bearer.startswith(PREFIX):
        return None
    row = conn.execute(
        "SELECT t.id, t.node_id, t.expires_at, t.revoked, n.deleted"
        " FROM operator_token t JOIN node n ON n.id = t.node_id WHERE t.hash = ?",
        (digest(bearer),),
    ).fetchone()
    if row is None:
        return None
    token_id, node_id, expires_at, revoked, deleted = row
    return Credential(
        kind="operator",
        id=token_id,
        actor=token_id,
        node_id=node_id,
        expires_at=float(expires_at),
        revoked=bool(revoked),
        node_deleted=bool(deleted),
        scopes=frozenset({"mcp"}),  # BR-055: operator tokens implicitly carry `mcp`
        audience=None,
        token_key=f"operator:{token_id}",
    )
