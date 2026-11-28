"""Tree organisation › client › project › environment › role (BR-044 to BR-049).

The functions that write open their own `BEGIN IMMEDIATE` transaction and write their audit entry
in it (BR-065, BR-066); a violated constraint raises `Refused` before any visible write (the
transaction is rolled back).
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hullgate import audit
from hullgate.db import transaction
from hullgate.migrations import ORG_ID

if TYPE_CHECKING:  # circular import: `connections` relies on the tree
    from hullgate.connections import ShareSignal

LEVELS = ("org", "client", "project", "env", "role")
LEVEL_LABELS = {
    "org": "organisation",
    "client": "client",
    "project": "project",
    "env": "environment",
    "role": "role",
}
LEVEL_ALIASES = {
    "client": "client",
    "project": "project",
    "environment": "env",
    "env": "env",
    "role": "role",
}
SEGMENT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEGMENT_MAX = 40
NAME_MAX = 80
DEFAULT_ROLE = "principal"

HOUR = 3600
DAY = 24 * HOUR
DURATION_RE = re.compile(r"^([1-9][0-9]{0,5})([hd])$")
DURATION_MIN = HOUR
DURATION_MAX = 365 * DAY

Clock = Callable[[], float]


class Refused(Exception):
    """Violated constraint (exit code 2); the message names the constraint."""


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------


def parse_level(text: str) -> str:
    level = LEVEL_ALIASES.get(text.strip().lower())
    if level is None:
        raise Refused(
            f"unknown level: {text} (client, project, environment or role; "
            "the organisation is unique and already exists)"
        )
    return level


def validate_name(name: str) -> str:
    if not 1 <= len(name) <= NAME_MAX:
        raise Refused(f"invalid display name: 1 to {NAME_MAX} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise Refused("invalid display name: control character")
    return name


def validate_segment(segment: str) -> str:
    if not 1 <= len(segment) <= SEGMENT_MAX or not SEGMENT_RE.match(segment):
        raise Refused(
            f"invalid segment: {segment!r} (lowercase letters, digits and single hyphens, "
            f"1 to {SEGMENT_MAX} characters)"
        )
    return segment


def derive_segment(name: str) -> str:
    """The prototype's rule: lowercase, accents removed, any other run becomes `-`."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")


def segment_for(name: str, slug: str | None) -> str:
    if slug is not None:
        return validate_segment(slug)
    derived = derive_segment(name)
    if not derived:
        raise Refused(f"empty derived segment for the name {name!r}: give --slug")
    return validate_segment(derived)


def parse_duration(text: str) -> int:
    """`<n>h` or `<n>d`, from 1 h to 365 d; returns seconds."""
    match = DURATION_RE.match(text.strip())
    if match is None:
        raise Refused(f"invalid duration: {text!r} (expected <n>h or <n>d)")
    seconds = int(match.group(1)) * (HOUR if match.group(2) == "h" else DAY)
    if not DURATION_MIN <= seconds <= DURATION_MAX:
        raise Refused(f"duration out of bounds: {text} (from 1h to 365d)")
    return seconds


def _validate_bool(text: str) -> str:
    value = text.strip().lower()
    if value not in ("true", "false"):
        raise Refused(f"invalid value for lock: {text!r} (true or false)")
    return value


def _validate_duration(text: str) -> str:
    parse_duration(text)
    return text.strip()


MODES = ("read", "write", "destructive")  # BR-170, in order
PINNING = ("auto", "on", "off")  # BR-180
CAP_RE = re.compile(r"^([1-9][0-9]*)(KiB|MiB)$")
CAP_MIN = 1024
CAP_MAX = 1024 * 1024  # BR-184: at most the 1 MiB of BR-107


def _validate_choice(key: str, choices: tuple[str, ...]) -> Callable[[str], str]:
    def validate(text: str) -> str:
        value = text.strip()
        if value not in choices:
            raise Refused(f"invalid value for {key}: {text!r} ({', '.join(choices)})")
        return value

    return validate


def parse_cap(text: str) -> int:
    """`<n>KiB` or `<n>MiB`, from 1 KiB to 1 MiB; returns bytes (BR-184)."""
    match = CAP_RE.match(text.strip())
    if match is None:
        raise Refused(f"invalid value for response_cap: {text!r} (expected <n>KiB or <n>MiB)")
    size = int(match.group(1)) * (1024 if match.group(2) == "KiB" else 1024 * 1024)
    if not CAP_MIN <= size <= CAP_MAX:
        raise Refused(f"response_cap out of bounds: {text.strip()} (from 1KiB to 1MiB)")
    return size


def _validate_cap(text: str) -> str:
    parse_cap(text)
    return text.strip()


RATE_RE = re.compile(r"^([1-9][0-9]{0,5})/min$")  # digits bounded: int() never sees 4 301
RATE_MAX = 100_000  # F-020 §3.9


def parse_rate(text: str) -> int:
    """`<n>/min`, from 1 to 100000; returns the calls per minute (BR-303, BR-304)."""
    match = RATE_RE.match(text.strip())
    if match is None or int(match.group(1)) > RATE_MAX:
        raise Refused(f"invalid rate: {text!r} (expected <n>/min, from 1 to {RATE_MAX})")
    return int(match.group(1))


def _validate_rate(text: str) -> str:
    parse_rate(text)
    return text.strip()


def _validate_expiry_days(text: str) -> str:
    if not re.fullmatch(r"[0-9]{1,2}", text) or not 1 <= int(text) <= 90:
        raise Refused("connection_expiry_days: whole days from 1 to 90 expected")
    return str(int(text))


# Inherited settings (BR-048): the foundation's, the action policy's (F-015), then the tool-call
# limits (F-020: no "off" value, a high one instead).
SETTINGS: dict[str, Callable[[str], str]] = {
    "connection_expiry_days": _validate_expiry_days,
    "token_duration": _validate_duration,
    "lock": _validate_bool,
    "mode": _validate_choice("mode", MODES),
    "pinning": _validate_choice("pinning", PINNING),
    "response_cap": _validate_cap,
    "token_rate_limit": _validate_rate,
    "connection_rate_limit": _validate_rate,
}


# ---------------------------------------------------------------------------------------------
# Snapshot of the live tree
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    parent_id: str | None
    level: str
    slug: str
    name: str
    sensitive: bool


@dataclass(slots=True)
class Tree:
    """Live (not deleted) nodes and setting overrides, read in one go."""

    nodes: dict[str, Node]
    settings: dict[tuple[str, str], str]
    children: dict[str, list[Node]] = field(default_factory=dict)
    public_id: str = ""  # the organisation's public identifier (BR-160, F-014)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Tree:
        rows = conn.execute(
            "SELECT id, parent_id, level, slug, name, sensitive FROM node WHERE deleted = 0"
        ).fetchall()
        nodes = {r[0]: Node(r[0], r[1], r[2], r[3], r[4], bool(r[5])) for r in rows}
        settings = {
            (r[0], r[1]): r[2]
            for r in conn.execute("SELECT node_id, key, value FROM setting").fetchall()
            if r[0] in nodes
        }
        tree = cls(nodes, settings, public_id=organisation_id(conn))
        for node in nodes.values():
            if node.parent_id is not None:
                tree.children.setdefault(node.parent_id, []).append(node)
        for kids in tree.children.values():
            kids.sort(key=lambda n: n.slug)
        return tree

    @property
    def org(self) -> Node:
        return self.nodes[ORG_ID]

    def kids(self, node: Node) -> list[Node]:
        return self.children.get(node.id, [])

    def ancestors(self, node: Node) -> list[Node]:
        """From the parent up to the organisation."""
        out: list[Node] = []
        current = node
        while current.parent_id is not None:
            current = self.nodes[current.parent_id]
            out.append(current)
        return out

    def lineage_of(self, node: Node) -> list[Node]:
        """The node, then its ancestors up to the organisation."""
        return [node, *self.ancestors(node)]

    def path(self, node: Node) -> str:
        segments = [n.slug for n in reversed(self.lineage_of(node)) if n.level != "org"]
        return "/".join(segments) if segments else "/"

    def subtree(self, node: Node) -> list[Node]:
        out = [node]
        for kid in self.kids(node):
            out.extend(self.subtree(kid))
        return out

    def find(self, path: str) -> Node | None:
        """Live node at the given path; segments are compared as they are (BR-044)."""
        text = path
        if text in ("", "/", "*"):
            return self.org
        current = self.org
        for segment in text.strip("/").split("/"):
            current_next = next((k for k in self.kids(current) if k.slug == segment), None)
            if current_next is None:
                return None
            current = current_next
        return current

    def resolve(self, path: str) -> Node:
        node = self.find(path)
        if node is None:
            raise Refused(f"unknown path: {path}")
        return node

    def is_sensitive(self, node: Node) -> bool:
        """Effective sensitivity (BR-047): the marked environment and its roles, nothing else."""
        if node.level == "env":
            return node.sensitive
        if node.level == "role" and node.parent_id is not None:
            return self.nodes[node.parent_id].sensitive
        return False

    def environment_of(self, node: Node) -> Node | None:
        return next((n for n in self.lineage_of(node) if n.level == "env"), None)

    def effective(self, node: Node, key: str) -> tuple[str, Node]:
        """Effective value of a setting and its origin node: the nearest override (BR-048)."""
        for candidate in self.lineage_of(node):
            value = self.settings.get((candidate.id, key))
            if value is not None:
                return value, candidate
        raise KeyError(key)  # the organisation always carries a value (migration 1)

    def locked_by(self, node: Node) -> Node | None:
        """Nearest locked node among the node and its ancestors (BR-049), or None."""
        for candidate in self.lineage_of(node):
            if self.settings.get((candidate.id, "lock")) == "true":
                return candidate
        return None

    def locked_in_subtree(self, node: Node) -> Node | None:
        for candidate in self.subtree(node):
            if self.settings.get((candidate.id, "lock")) == "true":
                return candidate
        return None

    def roles_below(self, node: Node) -> list[Node]:
        return [n for n in self.subtree(node) if n.level == "role"]

    def sensitive_envs_below(self, node: Node) -> list[Node]:
        return [n for n in self.subtree(node) if n.level == "env" and n.sensitive]


def organisation_id(conn: sqlite3.Connection) -> str:
    """The organisation's public identifier (BR-160): drawn once by migration 10, never changed."""
    row = conn.execute("SELECT public_id FROM node WHERE id = ?", (ORG_ID,)).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else ""


def _refuse_if_locked(tree: Tree, node: Node) -> None:
    locker = tree.locked_by(node)
    if locker is not None:
        raise Refused(f"locked by {tree.path(locker)}: {tree.path(node)}")


def _refuse_if_locked_below(tree: Tree, node: Node) -> None:
    """BR-046 v1 amended (2026-10-28): a locked descendant blocks rename and move."""
    locked = tree.locked_in_subtree(node)
    if locked is not None and locked.id != node.id:
        raise Refused(f"locked by {tree.path(locked)}: subtree of {tree.path(node)}")


def _new_id() -> str:
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------------------------
# Counts of active tokens and lineage revocations
# ---------------------------------------------------------------------------------------------


def _placeholders(ids: Iterable[str]) -> tuple[str, list[str]]:
    values = list(ids)
    return ",".join("?" * len(values)), values


def active_operator_tokens(conn: sqlite3.Connection, node_ids: Iterable[str], now: float) -> int:
    marks, values = _placeholders(node_ids)
    if not values:
        return 0
    row = conn.execute(
        f"SELECT count(*) FROM operator_token WHERE revoked = 0 AND expires_at > ? "  # noqa: S608
        f"AND node_id IN ({marks})",
        [int(now), *values],
    ).fetchone()
    return int(row[0])


def active_lineages(conn: sqlite3.Connection, role_ids: Iterable[str], now: float) -> int:
    marks, values = _placeholders(role_ids)
    if not values:
        return 0
    row = conn.execute(
        f"SELECT count(*) FROM lineage WHERE revoked = 0 AND expires_at > ? "  # noqa: S608
        f"AND role_id IN ({marks})",
        [int(now), *values],
    ).fetchone()
    return int(row[0])


def revoke_lineages(
    conn: sqlite3.Connection,
    tree: Tree,
    role_ids: Iterable[str],
    now: float,
    reason: str,
) -> int:
    """Revokes the unrevoked OAuth lineages of these roles and their access tokens (BR-045/046).

    One `lineage.revoke` audit entry per lineage, with its identifier and the reason (BR-066).
    """
    marks, values = _placeholders(role_ids)
    if not values:
        return 0
    rows = conn.execute(
        f"SELECT id, role_id FROM lineage WHERE revoked = 0 AND role_id IN ({marks})"  # noqa: S608
        " ORDER BY id",
        values,
    ).fetchall()
    for lineage_id, role_id in rows:
        conn.execute(
            "UPDATE access_jti SET revoked = 1 WHERE revoked = 0 AND lineage_id = ?", (lineage_id,)
        )
        conn.execute(
            "UPDATE lineage SET revoked = 1, revoked_at = ? WHERE id = ?", (int(now), lineage_id)
        )
        _audit_cascade(conn, tree, "lineage.revoke", lineage_id, role_id, reason, now)
    return len(rows)


def revoke_operator_tokens(
    conn: sqlite3.Connection,
    tree: Tree,
    node_ids: Iterable[str],
    now: float,
    reason: str,
) -> int:
    """Revokes the unrevoked operator tokens placed on these nodes; one entry per token."""
    marks, values = _placeholders(node_ids)
    if not values:
        return 0
    rows = conn.execute(
        f"SELECT id, node_id FROM operator_token WHERE revoked = 0 AND node_id IN ({marks})"  # noqa: S608
        " ORDER BY id",
        values,
    ).fetchall()
    for token_id, node_id in rows:
        conn.execute(
            "UPDATE operator_token SET revoked = 1, revoked_at = ? WHERE id = ?",
            (int(now), token_id),
        )
        _audit_cascade(conn, tree, "token.revoke", token_id, node_id, reason, now)
    return len(rows)


def _audit_cascade(
    conn: sqlite3.Connection,
    tree: Tree,
    action: str,
    token_id: str,
    node_id: str,
    reason: str,
    now: float,
) -> None:
    node = tree.nodes.get(node_id)
    audit.write(
        conn,
        actor=audit.actor(),
        action=action,
        result="ok",
        node_id=node_id,
        path=tree.path(node) if node is not None else None,
        reason=reason,
        after={"token_id": token_id},
        now=now,
    )


# ---------------------------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Added:
    path: str
    level: str
    created: tuple[str, ...]  # paths created (an environment also creates its `principal` role)


def add(
    conn: sqlite3.Connection,
    parent_path: str,
    level_text: str,
    name: str,
    slug: str | None = None,
    *,
    clock: Clock = time.time,
) -> Added:
    level = parse_level(level_text)
    validate_name(name)
    segment = segment_for(name, slug)
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        parent = tree.resolve(parent_path)
        expected_parent = LEVELS[LEVELS.index(level) - 1]
        if parent.level != expected_parent:
            raise Refused(
                f"invalid level: a {LEVEL_LABELS[level]} is created under a "
                f"{LEVEL_LABELS[expected_parent]}, not under a {LEVEL_LABELS[parent.level]} "
                f"({tree.path(parent)})"
            )
        _refuse_if_locked(tree, parent)
        if any(k.slug == segment for k in tree.kids(parent)):
            raise Refused(f"segment already used under {tree.path(parent)}: {segment}")
        created = [_insert(conn, tree, parent, level, segment, name, now)]
        if level == "env":
            created.append(_insert(conn, tree, created[0], "role", DEFAULT_ROLE, DEFAULT_ROLE, now))
        for node in created:
            audit.write(
                conn,
                actor=audit.actor(),
                action="tree.add",
                result="ok",
                node_id=node.id,
                path=tree.path(node),
                after={"level": node.level, "slug": node.slug, "name": node.name},
                now=now,
            )
        return Added(tree.path(created[0]), level, tuple(tree.path(n) for n in created))


def _insert(
    conn: sqlite3.Connection, tree: Tree, parent: Node, level: str, slug: str, name: str, now: float
) -> Node:
    node = Node(_new_id(), parent.id, level, slug, name, False)
    conn.execute(
        "INSERT INTO node (id, parent_id, level, slug, name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (node.id, node.parent_id, node.level, node.slug, node.name, int(now)),
    )
    tree.nodes[node.id] = node
    tree.children.setdefault(parent.id, []).append(node)
    return node


@dataclass(frozen=True, slots=True)
class Renamed:
    old_path: str
    new_path: str
    lineages_revoked: int


def rename(
    conn: sqlite3.Connection,
    path: str,
    name: str,
    slug: str | None = None,
    *,
    clock: Clock = time.time,
) -> Renamed:
    """Changes the display name and, with `slug`, the segment (BR-046: only the segment revokes)."""
    validate_name(name)
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        node = tree.resolve(path)
        _refuse_if_locked(tree, node)
        _refuse_if_locked_below(tree, node)
        old_path = tree.path(node)
        new_slug = node.slug
        if slug is not None:
            if node.level == "org":
                raise Refused("the organisation has no segment")
            new_slug = validate_segment(slug)
            if new_slug != node.slug and node.parent_id is not None:
                siblings = tree.kids(tree.nodes[node.parent_id])
                if any(k.slug == new_slug and k.id != node.id for k in siblings):
                    parent_path = tree.path(tree.nodes[node.parent_id])
                    raise Refused(f"segment already used under {parent_path}: {new_slug}")
        if name == node.name and new_slug == node.slug:  # no change (BR-066)
            return Renamed(old_path, old_path, 0)
        conn.execute("UPDATE node SET name = ?, slug = ? WHERE id = ?", (name, new_slug, node.id))
        revoked = 0
        if new_slug != node.slug:
            revoked = revoke_lineages(
                conn, tree, [r.id for r in tree.roles_below(node)], now, "path_changed"
            )
        new_path = old_path if new_slug == node.slug else _replace_last(old_path, new_slug)
        audit.write(
            conn,
            actor=audit.actor(),
            action="tree.rename",
            result="ok",
            node_id=node.id,
            path=new_path,
            before={"slug": node.slug, "name": node.name, "path": old_path},
            after={"slug": new_slug, "name": name, "path": new_path},
            detail={"lineages_revoked": revoked},
            now=now,
        )
        return Renamed(old_path, new_path, revoked)


@dataclass(frozen=True, slots=True)
class RenameImpact:
    path: str
    new_path: str
    urls: int  # access points whose URL changes (0 when the segment stays)
    lineages: int  # active OAuth lineages revoked with the old path (BR-046)


def rename_impact(
    conn: sqlite3.Connection, path: str, slug: str | None, *, clock: Clock = time.time
) -> RenameImpact:
    """What a rename does (BR-214): only a segment change moves URLs and cuts lineages."""
    tree = Tree.load(conn)
    node = tree.resolve(path)
    old_path = tree.path(node)
    if slug is None or slug == node.slug or node.level == "org":
        return RenameImpact(old_path, old_path, 0, 0)
    new_slug = validate_segment(slug)
    roles = tree.roles_below(node)
    return RenameImpact(
        old_path,
        _replace_last(old_path, new_slug),
        len(roles),
        active_lineages(conn, [r.id for r in roles], clock()),
    )


def _replace_last(path: str, segment: str) -> str:
    head, _, _ = path.rpartition("/")
    return f"{head}/{segment}" if head else segment


@dataclass(frozen=True, slots=True)
class MoveImpact:
    path: str
    new_path: str
    tokens_lost: int  # active tokens on the old ancestors, which stop covering
    tokens_gained: int  # active tokens on the new ancestors, which now cover
    lineages: int  # active OAuth lineages of the subtree, revoked
    roles: int  # URLs that change
    shares: tuple[ShareSignal, ...] = ()  # connections made shared (BR-079)


def _move_plan(
    conn: sqlite3.Connection, tree: Tree, node: Node, new_parent: Node, now: float
) -> MoveImpact:
    if node.level == "org":
        raise Refused("the organisation cannot be moved")
    if node.parent_id is None or new_parent.level != tree.nodes[node.parent_id].level:
        raise Refused(
            f"invalid level: a {LEVEL_LABELS[node.level]} moves under a "
            f"{LEVEL_LABELS[LEVELS[LEVELS.index(node.level) - 1]]}"
        )
    old_chain = {n.id for n in tree.ancestors(node)}
    new_chain = {n.id for n in tree.lineage_of(new_parent)}
    new_parent_path = tree.path(new_parent)
    new_path = node.slug if new_parent.level == "org" else f"{new_parent_path}/{node.slug}"
    roles = tree.roles_below(node)
    from hullgate import connections

    after = dict(tree.nodes)
    after[node.id] = Node(node.id, new_parent.id, node.level, node.slug, node.name, node.sensitive)
    return MoveImpact(
        path=tree.path(node),
        new_path=new_path,
        tokens_lost=active_operator_tokens(conn, old_chain - new_chain, now),
        tokens_gained=active_operator_tokens(conn, new_chain - old_chain, now),
        lineages=active_lineages(conn, [r.id for r in roles], now),
        roles=len(roles),
        shares=tuple(connections.shares_created(conn, tree.nodes, after)),
    )


def move_impact(
    conn: sqlite3.Connection, path: str, new_parent_path: str, *, clock: Clock = time.time
) -> MoveImpact:
    tree = Tree.load(conn)
    return _move_plan(conn, tree, tree.resolve(path), tree.resolve(new_parent_path), clock())


def move(
    conn: sqlite3.Connection,
    path: str,
    new_parent_path: str,
    *,
    clock: Clock = time.time,
) -> MoveImpact:
    """Changes the parent of a node, at the same level (BR-046): revokes the subtree's lineages."""
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        node = tree.resolve(path)
        new_parent = tree.resolve(new_parent_path)
        plan = _move_plan(conn, tree, node, new_parent, now)
        _refuse_if_locked(tree, node)
        _refuse_if_locked_below(tree, node)
        _refuse_if_locked(tree, new_parent)
        if node.parent_id == new_parent.id:
            raise Refused(f"already under {tree.path(new_parent)}: {plan.path}")
        if any(k.slug == node.slug for k in tree.kids(new_parent)):
            raise Refused(f"segment already used under {tree.path(new_parent)}: {node.slug}")
        conn.execute("UPDATE node SET parent_id = ? WHERE id = ?", (new_parent.id, node.id))
        revoked = revoke_lineages(
            conn, tree, [r.id for r in tree.roles_below(node)], now, "path_changed"
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action="tree.move",
            result="ok",
            node_id=node.id,
            path=plan.new_path,
            before={"path": plan.path},
            after={"path": plan.new_path},
            detail={
                "lineages_revoked": revoked,
                "tokens_lost": plan.tokens_lost,
                "tokens_gained": plan.tokens_gained,
            },
            now=now,
        )
        from hullgate import connections

        connections.audit_share_signals(conn, plan.shares, "tree.move", plan.new_path, now)
        return plan


@dataclass(frozen=True, slots=True)
class DeleteImpact:
    path: str
    descendants: int
    active_tokens: int  # active operator tokens and OAuth lineages of the subtree
    urls: int  # access points that stop answering
    placements: int = 0  # connection placements removed with their roles (BR-045, F-006)


def _delete_plan(
    conn: sqlite3.Connection, tree: Tree, node: Node, now: float, *, check_connections: bool = True
) -> DeleteImpact:
    if node.level == "org":
        raise Refused("the organisation cannot be deleted")
    nodes = tree.subtree(node)
    roles = tree.roles_below(node)
    from hullgate import connections

    if check_connections:
        connections.refuse_if_clients_carry_connections(conn, tree, nodes)
    marks, values = _placeholders(r.id for r in roles)
    placements = (
        int(
            conn.execute(
                f"SELECT count(*) FROM placement WHERE role_id IN ({marks})",  # noqa: S608
                values,
            ).fetchone()[0]
        )
        if values
        else 0
    )
    return DeleteImpact(
        path=tree.path(node),
        descendants=len(nodes) - 1,
        active_tokens=active_operator_tokens(conn, [n.id for n in nodes], now)
        + active_lineages(conn, [r.id for r in roles], now),
        urls=len(roles),
        placements=placements,
    )


def delete_impact(conn: sqlite3.Connection, path: str, *, clock: Clock = time.time) -> DeleteImpact:
    tree = Tree.load(conn)
    return _delete_plan(conn, tree, tree.resolve(path), clock())


def delete(conn: sqlite3.Connection, path: str, *, clock: Clock = time.time) -> DeleteImpact:
    """Deletes the subtree and revokes its tokens and lineages, in the transaction (BR-045)."""
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        node = tree.resolve(path)
        plan = _delete_plan(conn, tree, node, now)
        _refuse_if_locked(tree, node)
        locked = tree.locked_in_subtree(node)
        if locked is not None:
            raise Refused(f"locked by {tree.path(locked)}: subtree of {plan.path}")
        nodes = tree.subtree(node)
        ids = [n.id for n in nodes]
        tokens = revoke_operator_tokens(conn, tree, ids, now, "node_deleted")
        roles = [r.id for r in tree.roles_below(node)]
        lineages = revoke_lineages(conn, tree, roles, now, "node_deleted")
        from hullgate import connections

        connections.remove_placements_of_roles(conn, tree, roles, "node_deleted", now)
        marks, values = _placeholders(ids)
        conn.execute(f"UPDATE node SET deleted = 1 WHERE id IN ({marks})", values)  # noqa: S608
        audit.write(
            conn,
            actor=audit.actor(),
            action="tree.delete",
            result="ok",
            node_id=node.id,
            path=plan.path,
            before={"path": plan.path, "descendants": plan.descendants},
            detail={
                "tokens_revoked": tokens,
                "lineages_revoked": lineages,
                "urls": plan.urls,
                **({"placements_removed": plan.placements} if plan.placements else {}),
            },
            now=now,
        )
        return plan


@dataclass(frozen=True, slots=True)
class SensitiveChange:
    path: str
    sensitive: bool
    reopened_tokens: int  # unmark: active tokens of the ancestors that become accepted
    env_tokens: int = 0  # mark: active tokens on the environment, which will no longer pass
    long_role_tokens: int = 0  # mark: active tokens and lineages of the roles beyond 7 days
    shares: tuple[ShareSignal, ...] = ()  # connections made shared (BR-047, BR-079)


def sensitive_impact(
    conn: sqlite3.Connection, path: str, on: bool, *, clock: Clock = time.time
) -> SensitiveChange:
    tree = Tree.load(conn)
    node = tree.resolve(path)
    return _sensitive_plan(conn, tree, node, on, clock())


def _sensitive_plan(
    conn: sqlite3.Connection, tree: Tree, node: Node, on: bool, now: float
) -> SensitiveChange:
    if node.level != "env":
        raise Refused(
            f"sensitive: only an environment can be marked ({tree.path(node)} is a "
            f"{LEVEL_LABELS[node.level]})"
        )
    reopened = env_tokens = long_tokens = 0
    if not on and node.sensitive:
        reopened = active_operator_tokens(conn, [a.id for a in tree.ancestors(node)], now)
    if on and not node.sensitive:
        # No revocation: the 7-day ceiling is evaluated at issuance (BR-052); the operator sees
        # what changes (operator's decision).
        env_tokens = active_operator_tokens(conn, [node.id], now)
        roles = [r.id for r in tree.roles_below(node)]
        if roles:
            marks, values = _placeholders(roles)
            for table, column in (("operator_token", "node_id"), ("lineage", "role_id")):
                row = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE revoked = 0 AND expires_at > ? "  # noqa: S608
                    f"AND {column} IN ({marks})",
                    [int(now) + 7 * DAY, *values],
                ).fetchone()
                long_tokens += int(row[0])
    from hullgate import connections

    after = dict(tree.nodes)
    after[node.id] = Node(node.id, node.parent_id, node.level, node.slug, node.name, on)
    shares = tuple(connections.shares_created(conn, tree.nodes, after))
    return SensitiveChange(tree.path(node), on, reopened, env_tokens, long_tokens, shares)


def set_sensitive(
    conn: sqlite3.Connection, path: str, on: bool, *, clock: Clock = time.time
) -> SensitiveChange:
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        node = tree.resolve(path)
        plan = _sensitive_plan(conn, tree, node, on, now)
        _refuse_if_locked(tree, node)
        if node.sensitive == on:  # no change: nothing to write or audit (BR-066)
            return plan
        conn.execute("UPDATE node SET sensitive = ? WHERE id = ?", (int(on), node.id))
        audit.write(
            conn,
            actor=audit.actor(),
            action="tree.sensitive",
            result="ok",
            node_id=node.id,
            path=plan.path,
            before={"sensitive": node.sensitive},
            after={"sensitive": on},
            detail=(
                {"reopened_tokens": plan.reopened_tokens}
                if not on
                else {"env_tokens": plan.env_tokens, "long_role_tokens": plan.long_role_tokens}
            ),
            now=now,
        )
        from hullgate import connections

        connections.audit_share_signals(conn, plan.shares, "tree.sensitive", plan.path, now)
        return plan


# ---------------------------------------------------------------------------------------------
# Inherited settings (BR-048) and lock (BR-049)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Effective:
    key: str
    value: str
    origin: str  # path of the node that carries the value


def settings_get(conn: sqlite3.Connection, path: str) -> list[Effective]:
    tree = Tree.load(conn)
    node = tree.resolve(path)
    out = []
    for key in SETTINGS:
        value, origin = tree.effective(node, key)
        out.append(Effective(key, value, tree.path(origin)))
    return out


def _declared(key: str) -> Callable[[str], str]:
    validator = SETTINGS.get(key)
    if validator is None:
        raise Refused(f"unknown setting: {key} (declared: {', '.join(SETTINGS)})")
    return validator


def _check_settings_lock(tree: Tree, node: Node, key: str, unlocking: bool) -> None:
    """The lock freezes settings and structure; only the locking node can be unlocked."""
    locker = tree.locked_by(node)
    if locker is None:
        return
    if unlocking and key == "lock":
        origin = next((a for a in tree.ancestors(node) if tree.locked_by(a) is a), None)
        if origin is not None:
            raise Refused(
                f"locked by {tree.path(origin)}: only that node can be unlocked ({tree.path(node)})"
            )
        return
    raise Refused(f"locked by {tree.path(locker)}: {tree.path(node)}")


def settings_set(
    conn: sqlite3.Connection, path: str, key: str, value: str, *, clock: Clock = time.time
) -> Effective:
    validator = _declared(key)
    clean = validator(value)
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        node = tree.resolve(path)
        if key == "connection_expiry_days" and node.level != "org":
            raise Refused("connection_expiry_days is global: set it on /")
        before = tree.settings.get((node.id, key))
        if key == "lock" and clean == "false" and node.level != "org":
            # No `false` override below the organisation: it would mask the lock of an ancestor
            # set later. Unlocking removes the override; without an override, nothing to write
            # (the effective value is returned).
            if before is None:
                _check_settings_lock(tree, node, key, unlocking=False)
                value, origin = tree.effective(node, key)
                return Effective(key, value, tree.path(origin))
            return _unset_locked(conn, tree, node, key, before, now)
        if key == "lock" and clean == "true" and tree.locked_by(node) is node:
            raise Refused(f"already locked: {tree.path(node)}")
        _check_settings_lock(tree, node, key, unlocking=(key == "lock" and clean == "false"))
        if before == clean:  # no change: nothing to write or audit (BR-066)
            return Effective(key, clean, tree.path(node))
        conn.execute(
            "INSERT INTO setting (node_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (node_id, key) DO UPDATE SET value = excluded.value",
            (node.id, key, clean),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action="settings.changed" if key == "connection_expiry_days" else "settings.set",
            result="ok",
            node_id=node.id,
            path=tree.path(node),
            before={key: before},
            after={key: clean},
            now=now,
        )
        return Effective(key, clean, tree.path(node))


def settings_unset(
    conn: sqlite3.Connection, path: str, key: str, *, clock: Clock = time.time
) -> Effective:
    _declared(key)
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        node = tree.resolve(path)
        if node.level == "org":
            raise Refused(f"the organisation always carries a value: {key} (use set)")
        if key == "connection_expiry_days" and node.level != "org":
            raise Refused("connection_expiry_days is global: set it on /")
        before = tree.settings.get((node.id, key))
        return _unset_locked(conn, tree, node, key, before, now)


def _unset_locked(
    conn: sqlite3.Connection, tree: Tree, node: Node, key: str, before: str | None, now: float
) -> Effective:
    unlocking = key == "lock" and before == "true"
    _check_settings_lock(tree, node, key, unlocking=unlocking)
    if before is None:
        raise Refused(f"no override of {key} on {tree.path(node)}")
    conn.execute("DELETE FROM setting WHERE node_id = ? AND key = ?", (node.id, key))
    del tree.settings[(node.id, key)]
    value, origin = tree.effective(node, key)
    audit.write(
        conn,
        actor=audit.actor(),
        action="settings.unset",
        result="ok",
        node_id=node.id,
        path=tree.path(node),
        before={key: before},
        after={key: None, "effective": value, "origin": tree.path(origin)},
        now=now,
    )
    return Effective(key, value, tree.path(origin))


def lock(conn: sqlite3.Connection, path: str, *, clock: Clock = time.time) -> Effective:
    return settings_set(conn, path, "lock", "true", clock=clock)


def unlock(conn: sqlite3.Connection, path: str, *, clock: Clock = time.time) -> Effective:
    tree = Tree.load(conn)
    node = tree.resolve(path)
    if tree.locked_by(node) is None:
        raise Refused(f"not locked: {tree.path(node)}")
    return settings_set(conn, path, "lock", "false", clock=clock)
