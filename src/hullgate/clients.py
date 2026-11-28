"""OAuth clients managed by the CLI: preregistration, list, removal, DCR (BR-061, SCR-02).

A preregistered client is bound to a node and admitted by the authorization server of each role
of its subtree. Removing it revokes its active lineages. Outside the lock (H11): authorizing or
removing a client is not a change to the tree's configuration.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from hullgate import audit
from hullgate.cimd import is_cimd_client_id, valid_redirect_uri
from hullgate.db import transaction
from hullgate.tokens import DELETED as DELETED_PATH
from hullgate.tree import Clock, Refused, Tree, validate_name

CLIENT_ID_MAX = 200


@dataclass(frozen=True, slots=True)
class Listed:
    client_id: str
    kind: str
    path: str
    redirect_uris: tuple[str, ...]
    name: str | None
    state: str  # "active" or "removed"

    def as_dict(self) -> dict[str, object]:
        return {
            "client_id": self.client_id,
            "kind": self.kind,
            "path": self.path,
            "redirect_uris": list(self.redirect_uris),
            "name": self.name,
            "state": self.state,
        }


def _validate_client_id(client_id: str) -> str:
    if not client_id or len(client_id) > CLIENT_ID_MAX or not client_id.isprintable():
        raise Refused(f"invalid client_id (1 to {CLIENT_ID_MAX} printable characters)")
    if any(c.isspace() for c in client_id):
        raise Refused("invalid client_id: space forbidden")
    try:
        urlsplit(client_id)
    except ValueError:  # "https://[": neither a document's address nor a plain identifier
        raise Refused("invalid client_id: unreadable URL form") from None
    if is_cimd_client_id(client_id):
        raise Refused(
            "client_id as an https URL: reserved for metadata documents (CIMD), "
            "admitted without preregistration"
        )
    if client_id.startswith("dcr_"):
        raise Refused("prefix dcr_ reserved for dynamic registration")
    return client_id


def add(
    conn: sqlite3.Connection,
    client_id: str,
    path: str,
    redirect_uris: Sequence[str],
    name: str | None = None,
    *,
    clock: Clock = time.time,
) -> Listed:
    _validate_client_id(client_id)
    if not redirect_uris:
        raise Refused("at least one --redirect-uri is required")
    for uri in redirect_uris:
        if not valid_redirect_uri(uri):
            raise Refused(f"redirect_uri refused (https, or http to localhost): {uri}")
    if name is not None:
        validate_name(name)
    with transaction(conn):
        now = int(clock())
        tree = Tree.load(conn)
        node = tree.resolve(path)
        node_path = tree.path(node)
        if conn.execute(
            "SELECT 1 FROM client WHERE client_id = ? AND node_id = ? AND removed_at IS NULL",
            (client_id, node.id),
        ).fetchone():
            raise Refused(f"client already registered on {node_path}: {client_id}")
        uris = list(dict.fromkeys(redirect_uris))
        meta: dict[str, object] = {"redirect_uris": uris}
        if name:
            meta["client_name"] = name
        conn.execute(
            "INSERT INTO client (client_id, kind, node_id, metadata, created_at)"
            " VALUES (?, 'preregistered', ?, ?, ?)",
            (client_id, node.id, json.dumps(meta, ensure_ascii=False, sort_keys=True), now),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action="client.add",
            result="ok",
            node_id=node.id,
            path=node_path,
            after={"client_id": client_id, **meta},
            now=now,
        )
    return Listed(client_id, "preregistered", node_path, tuple(uris), name, "active")


def list_clients(conn: sqlite3.Connection) -> list[Listed]:
    tree = Tree.load(conn)
    out: list[Listed] = []
    rows = conn.execute(
        "SELECT client_id, kind, node_id, metadata, removed_at FROM client ORDER BY client_id"
    ).fetchall()
    for client_id, kind, node_id, metadata, removed_at in rows:
        try:
            meta = json.loads(metadata or "{}")
        except ValueError:
            meta = {}
        uris = tuple(u for u in meta.get("redirect_uris", []) if isinstance(u, str))
        name = meta.get("client_name") if isinstance(meta.get("client_name"), str) else None
        if kind == "cimd":
            where = "(metadata document)"
        else:
            where = tree.path(tree.nodes[node_id]) if node_id in tree.nodes else DELETED_PATH
        out.append(Listed(client_id, kind, where, uris, name,
                          "removed" if removed_at is not None else "active"))  # fmt: skip
    return out


@dataclass(frozen=True, slots=True)
class Removed:
    client_id: str
    lineages_revoked: int


def remove(conn: sqlite3.Connection, client_id: str, *, clock: Clock = time.time) -> Removed:
    """The client is no longer admitted; its active lineages are revoked, one entry each."""
    with transaction(conn):
        now = int(clock())
        tree = Tree.load(conn)
        rows = conn.execute(
            "SELECT node_id, removed_at FROM client WHERE client_id = ? ORDER BY node_id",
            (client_id,),
        ).fetchall()
        if not rows:
            raise Refused(f"unknown client: {client_id}")
        live = [r for r in rows if r[1] is None]
        if not live:
            return Removed(client_id, 0)
        node_id = live[0][0]
        conn.execute(
            "UPDATE client SET removed_at = ? WHERE client_id = ? AND removed_at IS NULL",
            (now, client_id),
        )
        lineages = conn.execute(
            "SELECT id, role_id FROM lineage WHERE client_id = ? AND revoked = 0 ORDER BY id",
            (client_id,),
        ).fetchall()
        for lineage_id, role_id in lineages:
            conn.execute(
                "UPDATE access_jti SET revoked = 1 WHERE lineage_id = ? AND revoked = 0",
                (lineage_id,),
            )
            conn.execute(
                "UPDATE lineage SET revoked = 1, revoked_at = ? WHERE id = ?", (now, lineage_id)
            )
            node = tree.nodes.get(role_id)
            audit.write(
                conn,
                actor=audit.actor(),
                action="lineage.revoke",
                result="ok",
                node_id=role_id,
                path=tree.path(node) if node is not None else None,
                reason="client_removed",
                after={"token_id": lineage_id},
                now=now,
            )
        node = tree.nodes.get(node_id)
        audit.write(
            conn,
            actor=audit.actor(),
            action="client.remove",
            result="ok",
            node_id=node_id,
            path=tree.path(node) if node is not None else None,
            before={"client_id": client_id},
            detail={"lineages_revoked": len(lineages)},
            now=now,
        )
        return Removed(client_id, len(lineages))


def set_dcr(conn: sqlite3.Connection, on: bool, *, clock: Clock = time.time) -> bool:
    """Turns dynamic registration on or off for the whole instance; returns the previous state."""
    value = "on" if on else "off"
    with transaction(conn):
        now = int(clock())
        row = conn.execute("SELECT value FROM instance_flag WHERE key = 'dcr'").fetchone()
        before = row[0] if row else "off"
        if before == value:
            return before == "on"
        conn.execute(
            "INSERT INTO instance_flag (key, value) VALUES ('dcr', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (value,),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action="instance.dcr",
            result="ok",
            before={"dcr": before},
            after={"dcr": value},
            now=now,
        )
        return before == "on"
