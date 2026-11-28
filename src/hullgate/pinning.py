"""Operator side of definition pinning (F-015 lot B, BR-182): `tool diff` and `tool validate`.

Both take any node and act on the placements of its subtree. Each lists the upstream at reading
time, through `get_access`, with no tool call; an upstream that does not answer refuses the
command (exit 2) with nothing written. `tool diff` writes nothing and prints upstream text with
control characters and terminal escape sequences escaped: the text comes from the party pinning
distrusts. `tool validate` alone records the current definition as baseline and lifts a
suspension, audited `tool.validate` with both hashes; it is refused under a lock (BR-049). No
access point, relay path or client action reaches this module.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hullgate import audit, lifecycle, policy, relay
from hullgate.cipher import ConnectionsKey
from hullgate.connections import AccessError, _find, _placements, display_name
from hullgate.db import transaction
from hullgate.outbound import Outbound
from hullgate.tree import Clock, Node, Refused, Tree
from hullgate.upstream_mcp import UNAVAILABLE, RpcError, UpstreamClient, UpstreamError

STATES = ("suspended", "changed", "new", "removed")


def escaped(text: str, *, backslash: bool = True) -> str:
    """Upstream text made inert for a terminal: every non-printable character (C0, DEL, C1, format
    and separator controls, so ESC, BEL and CSI among them) becomes a `\\x..` or `\\u....`
    escape; a backslash is doubled so an escape cannot be forged (`backslash=False` for JSON text,
    where the encoder has already escaped every backslash of the value)."""
    out: list[str] = []
    for ch in text:
        if ch == "\\" and backslash:
            out.append("\\\\")
        elif ch.isprintable():
            out.append(ch)
        elif ord(ch) < 0x100:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(f"\\u{ord(ch):04x}")
    return "".join(out)


def shown(value: Any) -> str:
    """A field value as escaped one-line JSON; `-` when the field is absent."""
    if value is None:
        return "-"
    return escaped(json.dumps(value, sort_keys=True, ensure_ascii=False), backslash=False)


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    old: Any  # None: absent
    new: Any


@dataclass(frozen=True, slots=True)
class Change:
    connection: str
    role: str
    tool: str  # upstream name as listed, raw: escape before display
    state: str  # STATES
    baseline: str | None
    current: str | None
    fields: tuple[Field, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "role": self.role,
            "tool": self.tool,
            "state": self.state,
            "baseline_sha256": self.baseline,
            "current_sha256": self.current,
            "fields": [{"field": f.name, "old": f.old, "new": f.new} for f in self.fields],
        }


@dataclass(frozen=True, slots=True)
class Validated:
    connection: str
    role: str
    tool: str
    before: str | None
    after: str | None  # None: the tool is gone upstream, its baseline dropped


@dataclass(frozen=True, slots=True)
class _Row:
    tool_pin: bool
    sha256: str | None
    definition: str | None
    drift_sha256: str | None
    drift_definition: str | None
    suspended: bool


@dataclass(frozen=True, slots=True)
class _Placement:
    carried: relay.Carried
    role: Node
    role_path: str


def _rows(conn: sqlite3.Connection, connection_id: str, role_id: str) -> dict[str, _Row]:
    return {
        r[0]: _Row(bool(r[1]), r[2], r[3], r[4], r[5], r[6] is not None)
        for r in conn.execute(
            "SELECT upstream_name, tool_pin, sha256, definition, drift_sha256, drift_definition,"
            " suspended_at FROM pinned_definition WHERE connection_id = ? AND role_id = ?",
            (connection_id, role_id),
        )
    }


def _catalogue(conn: sqlite3.Connection, service_id: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT upstream_name FROM tool_catalog WHERE service_id = ?", (service_id,)
        )
    }


def _relevant(p: policy.PlacementState) -> bool:
    return bool(p.whole or p.tool_pins or p.rules or p.baselined or p.suspended)


def _serving(c: relay.Carried) -> None:
    if c.state != "active":
        raise Refused(f"{c.name} is not active (state {c.state}): nothing written")
    if c.url is None:
        raise Refused(f"{c.name}: upstream_undeclared: nothing written")


def _listing(
    run: lifecycle.Runner,
    key: ConnectionsKey,
    out: Outbound,
    client: UpstreamClient,
    target: _Placement,
    clock: Clock,
) -> dict[str, dict[str, Any]]:
    """A fresh listing of the upstream for one placement, no tool call; any failure refuses."""
    c = target.carried
    session_key = (target.role.id, c.id)
    try:
        access = lifecycle.access(run, key, out, c.id, target.role.id, clock=clock)
        try:
            try:
                listing = client.list_tools(session_key, c.target, access.authorization)
            except RpcError as rpc:
                raise UpstreamError(UNAVAILABLE, f"JSON-RPC error {rpc.code}") from None
        finally:
            client.close(session_key, wait=True)  # the CLI holds no session (BR-119)
    except UpstreamError as exc:
        raise Refused(f"{c.name}: upstream unavailable ({exc.code}): nothing written") from None
    except (lifecycle.Unavailable, AccessError):
        raise Refused(f"{c.name}: connection unavailable: nothing written") from None
    found: dict[str, dict[str, Any]] = {}
    for t in listing.tools:
        if isinstance(t.get("name"), str):
            found.setdefault(t["name"], t)
    return found


def _loads(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        raise policy.StateError("stored definition unreadable") from None
    return value if isinstance(value, dict) else None


def _state(conn: sqlite3.Connection, tree: Tree, t: _Placement) -> policy.PlacementState | None:
    """The placement's policy state; one that cannot be loaded names the connection (BR-185)."""
    try:
        return policy.ENGINE.load(conn, t.role.id, tree).placements.get(t.carried.id)
    except policy.StateError as exc:
        raise policy.StateError(str(exc), t.carried.name) from None


def _fields(old: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> tuple[Field, ...]:
    out = []
    for name in policy.PIN_FIELDS:
        o = old.get(name) if old is not None else None
        n = new.get(name) if new is not None else None
        if o != n:
            out.append(Field(name, o, n))
    return tuple(out)


# ---------------------------------------------------------------------------------------------
# tool diff
# ---------------------------------------------------------------------------------------------


def diff(
    run: lifecycle.Runner,
    key: ConnectionsKey,
    out: Outbound,
    client: UpstreamClient,
    node_path: str,
    *,
    clock: Clock = time.time,
) -> tuple[str, list[Change]]:
    """`tool diff <node-path>`: each suspended, changed, new or removed pinned tool of the
    subtree's placements, the fields that differ, old (baseline) and new (current upstream, or
    the definition that suspended it when the tool is gone). Writes nothing."""

    def plan(conn: sqlite3.Connection) -> tuple[str, list[_Placement]]:
        tree = Tree.load(conn)
        node = tree.resolve(node_path)
        targets: list[_Placement] = []
        for role in tree.roles_below(node):
            for c in relay.carried(conn, tree, role.id):
                p = _state(conn, tree, _Placement(c, role, tree.path(role)))
                if p is not None and _relevant(p):
                    targets.append(_Placement(c, role, tree.path(role)))
        return tree.path(node), targets

    path, targets = run(plan)
    for t in targets:
        _serving(t.carried)
    listings = [(t, _listing(run, key, out, client, t, clock)) for t in targets]

    def read(conn: sqlite3.Connection) -> list[Change]:
        tree = Tree.load(conn)
        changes: list[Change] = []
        for t, current in listings:
            p = _state(conn, tree, t)
            if p is None:
                continue
            rows = _rows(conn, t.carried.id, t.role.id)
            known = _catalogue(conn, t.carried.service_id)
            names = sorted(set(rows) | (set(current) & known))
            for name in names:
                try:
                    change = _change(t, p, name, rows.get(name), current.get(name))
                except policy.StateError as exc:
                    raise policy.StateError(str(exc), t.carried.name) from None
                if change is not None:
                    changes.append(change)
        return changes

    return path, run(read)


def _change(t: _Placement, p: policy.PlacementState, name: str, row: _Row | None,
            cur: Mapping[str, Any] | None) -> Change | None:  # fmt: skip
    digest = policy.definition_hash(cur, lenient=True)[0] if cur is not None else None
    new = policy.canonical(cur) if cur is not None else None
    if row is not None and row.suspended:
        old = _loads(row.definition) if row.sha256 is not None else None
        if new is None:
            new = _loads(row.drift_definition)
        state = "suspended"
    elif row is not None and row.sha256 is not None:
        if cur is not None and digest == row.sha256:
            return None
        old = _loads(row.definition)
        state = "removed" if cur is None else "changed"
    elif cur is not None and p.pinned(name) and p.whole and p.baselined:
        old, state = None, "new"
    else:
        return None
    return Change(t.carried.name, t.role_path, name, state, row.sha256 if row else None, digest,
                  _fields(old, new))  # fmt: skip


# ---------------------------------------------------------------------------------------------
# tool validate
# ---------------------------------------------------------------------------------------------


def _locked(tree: Tree, roles: list[Node]) -> None:
    for role in roles:
        locker = tree.locked_by(role)
        if locker is not None:
            raise Refused(f"locked by {tree.path(locker)}: tool validate on {tree.path(role)}")


def validate(
    run: lifecycle.Runner,
    key: ConnectionsKey,
    out: Outbound,
    client: UpstreamClient,
    node_path: str,
    connection: str,
    tool: str | None = None,
    *,
    clock: Clock = time.time,
) -> list[Validated]:
    """`tool validate <node-path> <connection> [--tool <tool>]`: re-reads the upstream and
    records what is there now as the baseline of each pinned tool of the connection's placements
    in the subtree (or of `--tool` only), lifting its suspension; a tool gone upstream loses its
    baseline. Every refusal (lock, placement, unknown or unpinned tool, unavailable upstream)
    comes before any write."""

    def plan(conn: sqlite3.Connection) -> tuple[str, list[_Placement]]:
        tree = Tree.load(conn)
        node = tree.resolve(node_path)
        row = _find(conn, tree, connection)
        name = display_name(tree.nodes, row)
        placed = set(_placements(conn, row.id))
        roles = [r for r in tree.roles_below(node) if r.id in placed]
        if not roles:
            raise Refused(f"not placed under {tree.path(node)}: {name}")
        _locked(tree, roles)  # before any exchange (BR-049)
        if tool is not None:
            policy._catalogued(conn, row.service_id, tool)
        targets = []
        for role in roles:
            c = next(x for x in relay.carried(conn, tree, role.id) if x.id == row.id)
            targets.append(_Placement(c, role, tree.path(role)))
        return name, targets

    name, targets = run(plan)
    _serving(targets[0].carried)
    current = _listing(run, key, out, client, targets[0], clock)  # one upstream per connection

    def write(conn: sqlite3.Connection) -> list[Validated]:
        with transaction(conn):
            now = clock()
            tree = Tree.load(conn)
            _locked(tree, [t.role for t in targets])
            done: list[Validated] = []
            candidates_seen = False
            for t in targets:
                p = _state(conn, tree, t)
                if p is None:
                    continue
                rows = _rows(conn, t.carried.id, t.role.id)
                known = _catalogue(conn, t.carried.service_id)
                names = {n for n in set(current) & known if p.pinned(n)}
                names |= {n for n, r in rows.items() if r.sha256 is not None or r.suspended}
                if tool is not None:
                    names &= {tool}
                candidates_seen = candidates_seen or bool(names)
                for n in sorted(names):
                    v = _validate_one(conn, t, n, rows.get(n), current.get(n), now)
                    if v is not None:
                        done.append(v)
            if tool is not None and not candidates_seen:
                raise Refused(f"not pinned under {node_path.strip('/') or '/'}: "
                              f"{relay.safe_label(tool, 256)} on {name}")  # fmt: skip
            return done

    return run(write)


def _validate_one(conn: sqlite3.Connection, t: _Placement, name: str, row: _Row | None,
                  cur: Mapping[str, Any] | None, now: float) -> Validated | None:  # fmt: skip
    cid, rid = t.carried.id, t.role.id
    before = row.sha256 if row is not None else None
    suspended = row is not None and row.suspended
    if cur is not None:
        try:
            digest, text = policy.definition_hash(cur)
        except UnicodeError:  # a lone surrogate: no baseline can hold it, the relay serves none
            raise Refused(
                f"{t.carried.name}: the upstream definition of {escaped(name)} cannot be"
                " encoded: nothing written (validate the other tools with --tool)"
            ) from None
        if row is not None and row.sha256 == digest and not suspended and not row.drift_sha256:
            return None  # already the baseline
        conn.execute(
            "INSERT INTO pinned_definition (connection_id, role_id, upstream_name, sha256,"
            " definition) VALUES (?, ?, ?, ?, ?) ON CONFLICT (connection_id, role_id,"
            " upstream_name) DO UPDATE SET sha256 = excluded.sha256,"
            " definition = excluded.definition, drift_sha256 = NULL, drift_definition = NULL,"
            " suspended_at = NULL",
            (cid, rid, name, digest, text),
        )
        after: str | None = digest
    else:  # gone upstream: validating drops its baseline (BR-181)
        if row is None:
            return None
        if row.tool_pin:
            conn.execute(
                "UPDATE pinned_definition SET sha256 = NULL, definition = NULL,"
                " drift_sha256 = NULL, drift_definition = NULL, suspended_at = NULL"
                " WHERE connection_id = ? AND role_id = ? AND upstream_name = ?",
                (cid, rid, name),
            )
        else:
            conn.execute(
                "DELETE FROM pinned_definition WHERE connection_id = ? AND role_id = ?"
                " AND upstream_name = ?",
                (cid, rid, name),
            )
        after = None
    audit.write(
        conn,
        actor=audit.actor(),
        action=policy.A_VALIDATE,
        result="ok",
        node_id=rid,
        path=t.role_path,
        before={"sha256": before, "suspended": suspended},
        after={"sha256": after, "suspended": False},
        detail={"connection": t.carried.name, "tool": relay.safe_label(name, 256)},
        now=now,
    )
    return Validated(t.carried.name, t.role_path, name, before, after)
