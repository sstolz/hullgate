"""Action policy (F-015): tool classes, the tool catalogue and its queue, the exposure decision,
lists and argument rules per placement, and the operator's `policy` and `tool` operations.

**Seam** (BR-123). `decide(role, connection, upstream name, upstream definition, arguments |
None)` returns `ALLOW`, `HIDE` or `Refuse(reason)`. The relay consults it for each tool at a
listing (`arguments` None) and at each call (`arguments` present), reads a hidden or refused tool
as "unknown tool" only, and reads an exception as a refusal (`policy_error`, fail closed).

**Engine.** The real policy is `ENGINE`: at each request the relay loads a role's whole policy
state once (`Engine.load`, a `RoleState`), never caches it, and asks that state; `tools/list`,
`tools/call` and `policy preview` take the verdict from the same pure evaluator (`evaluate`, the
table of F-015 §3.2). A test double that allows everything has no place here.

**Operations.** Every `policy.*` and `tool.*` write is a function of this module, audited in the
transaction that writes it (BR-188); the CLI is a thin layer and HULL-11 calls the same
functions. Every write on a placement is refused under a lock (BR-049).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from hullgate import audit
from hullgate.connections import Row, _find, _placements, _role, display_name
from hullgate.db import transaction
from hullgate.tree import MODES, Clock, Node, Refused, Tree

# ---------------------------------------------------------------------------------------------
# Decisions (BR-123 seam)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Allow:
    pass


@dataclass(frozen=True, slots=True)
class Hide:
    pass


@dataclass(frozen=True, slots=True)
class Refuse:
    reason: str = ""


Decision = Allow | Hide | Refuse
ALLOW = Allow()
HIDE = Hide()


class Policy(Protocol):
    def decide(
        self,
        role_id: str,
        connection_id: str,
        upstream_name: str,
        definition: Mapping[str, Any],
        arguments: Mapping[str, Any] | None,
    ) -> Decision: ...


def consult(
    policy: Policy,
    role_id: str,
    connection_id: str,
    upstream_name: str,
    definition: Mapping[str, Any],
    arguments: Mapping[str, Any] | None,
) -> tuple[bool, str | None]:
    """(allowed, reason): true only for an explicit `ALLOW`; the reason of a refusal is kept for
    the audit (BR-185); an exception refuses with `policy_error` (BR-123)."""
    try:
        decision = policy.decide(role_id, connection_id, upstream_name, definition, arguments)
    except Exception:  # fail closed
        return False, POLICY_ERROR
    if isinstance(decision, Allow):
        return True, None
    if isinstance(decision, Refuse) and decision.reason:
        return False, decision.reason
    return False, None


# ---------------------------------------------------------------------------------------------
# Classes and their sources (BR-170, BR-171, BR-173)
# ---------------------------------------------------------------------------------------------

CLASSES = MODES  # `read` < `write` < `destructive`
RANK = {c: i for i, c in enumerate(CLASSES)}
DEFAULT_CLASS = "destructive"
SOURCES = ("manual", "annotations", "default")
LISTS = ("open", "deny", "allowlist")
DESTRUCTIVE_PREFIXES = ("delete_", "remove_", "merge_", "share_")  # BR-173 heuristic

# Refusal reasons, closed list, in the order of §3.2 (BR-185)
SUSPENDED = "suspended"
DENIED = "denied"
NOT_ALLOWLISTED = "not_allowlisted"
WRITE_CLOSED = "write_closed"
ABOVE_MODE = "above_mode"
ARGUMENT_VALUE = "argument_value"
POLICY_ERROR = "policy_error"
REASONS = (SUSPENDED, DENIED, NOT_ALLOWLISTED, WRITE_CLOSED, ABOVE_MODE, ARGUMENT_VALUE,
           POLICY_ERROR)  # fmt: skip

# Audit actions (BR-188)
A_MODE = "policy.mode"
A_LIST_ADD = "policy.list_add"
A_LIST_REMOVE = "policy.list_remove"
A_ARGUMENT_SET = "policy.argument_set"
A_ARGUMENT_REMOVE = "policy.argument_remove"
A_PIN = "policy.pin"
A_CLASSIFY = "tool.classify"
A_ACCEPT = "tool.accept"
A_DISCOVERED = "tool.discovered"
A_SUSPENDED = "tool.suspended"
A_VALIDATE = "tool.validate"
ACTIONS = (A_MODE, A_LIST_ADD, A_LIST_REMOVE, A_ARGUMENT_SET, A_ARGUMENT_REMOVE, A_PIN,
           A_CLASSIFY, A_ACCEPT, A_DISCOVERED, A_SUSPENDED, A_VALIDATE)  # fmt: skip


PIN_VALUES = ("on", "off", "inherit")  # `policy pin` (BR-180)
PIN_FIELDS = ("title", "description", "inputSchema", "outputSchema", "annotations")  # BR-181


def admits(mode: str, cls: str) -> bool:
    """A mode admits its own class and the lower ones (BR-170)."""
    return RANK[cls] <= RANK[mode]


def annotation_class(definition: Mapping[str, Any]) -> str | None:
    """Class the MCP annotations declare (BR-171), or None (no usable annotation).

    `destructiveHint: true`, alone or with `readOnlyHint: true` → `destructive` (stricter than the
    MCP schema); `readOnlyHint: true` without `destructiveHint: true` → `read`; `destructiveHint:
    false` with `readOnlyHint` false or absent → `write`; anything else → None. An absent
    `destructiveHint` is true in the MCP schema, so it never gives `write`."""
    notes = definition.get("annotations")
    if not isinstance(notes, Mapping):
        return None
    read_only = notes.get("readOnlyHint", None)
    destructive = notes.get("destructiveHint", None)
    if destructive is True:
        return "destructive"
    if read_only is True and (destructive is None or destructive is False):
        return "read"
    if destructive is False and (read_only is None or read_only is False):
        return "write"
    return None


def class_of(manual: str | None, annotated: str | None) -> tuple[str, str]:
    """(class, source): manual, otherwise annotations, otherwise `destructive` (BR-171)."""
    if manual is not None:
        return manual, "manual"
    if annotated is not None:
        return annotated, "annotations"
    return DEFAULT_CLASS, "default"


def proposal(upstream_name: str) -> str | None:
    """Name heuristic of the queue (BR-173): proposes, never imposes."""
    return DEFAULT_CLASS if upstream_name.startswith(DESTRUCTIVE_PREFIXES) else None


def grouped(definition: Mapping[str, Any]) -> dict[str, list[str]]:
    """Enumerated values of top-level string arguments (specification §Outils regroupés)."""
    schema = definition.get("inputSchema")
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    out: dict[str, list[str]] = {}
    if not isinstance(props, Mapping):
        return out
    for name, prop in props.items():
        if not isinstance(prop, Mapping):
            continue
        enum = prop.get("enum")
        if isinstance(enum, list) and enum and all(isinstance(v, str) for v in enum):
            out[str(name)] = [str(v) for v in enum]
    return out


# ---------------------------------------------------------------------------------------------
# The evaluator (§3.2, BR-174, BR-175), pure over its inputs
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Facts:
    cls: str
    mode: str  # effective mode of the placement (BR-177)
    sensitive: bool  # BR-186
    opened: bool = False  # in `open`
    denied: bool = False  # in `deny`
    allowlisted: bool = False  # in `allowlist`
    allowlist_in_force: bool = False  # sensitive, or a non-empty `allowlist`
    suspended: bool = False  # BR-181 (lot B)


@dataclass(frozen=True, slots=True)
class Verdict:
    exposed: bool
    reasons: tuple[str, ...]  # every applicable reason, in the order of §3.2
    opened: bool  # exposed by `open` beyond what the rest would give


def evaluate(f: Facts) -> Verdict:
    reasons: list[str] = []
    if f.suspended:
        reasons.append(SUSPENDED)
    if f.denied:
        reasons.append(DENIED)
    if not f.opened:  # `open` skips steps 4 to 5b
        reasons.extend(_bounds(f))
    exposed = not reasons
    beyond = exposed and f.opened and bool(_bounds(f))
    return Verdict(exposed, tuple(reasons), beyond)


def _bounds(f: Facts) -> list[str]:
    out: list[str] = []
    if f.allowlist_in_force and not f.allowlisted:
        out.append(NOT_ALLOWLISTED)
    if f.sensitive and RANK[f.cls] > RANK["read"]:
        out.append(WRITE_CLOSED)
    if not f.sensitive and not admits(f.mode, f.cls):
        out.append(ABOVE_MODE)
    return out


def value_allowed(rule: ArgumentRule, arguments: Mapping[str, Any]) -> bool:
    """BR-178: the named top-level argument is present and is a string equal to an allowed
    value; nothing else passes."""
    if rule.argument not in arguments:
        return False
    value = arguments[rule.argument]
    return isinstance(value, str) and value in rule.values


def narrowed(definition: Mapping[str, Any], rule: ArgumentRule) -> dict[str, Any]:
    """BR-179: the announced copy with `inputSchema.properties.<argument>.enum` reduced to the
    allowed values, upstream order kept; the definition is untouched without such an enum."""
    schema = definition.get("inputSchema")
    if not isinstance(schema, Mapping):
        return dict(definition)
    props = schema.get("properties")
    if not isinstance(props, Mapping):
        return dict(definition)
    prop = props.get(rule.argument)
    if not isinstance(prop, Mapping) or not isinstance(prop.get("enum"), list):
        return dict(definition)
    enum = [v for v in prop["enum"] if isinstance(v, str) and v in rule.values]
    return {
        **definition,
        "inputSchema": {
            **schema,
            "properties": {**props, rule.argument: {**prop, "enum": enum}},
        },
    }


# ---------------------------------------------------------------------------------------------
# Definition pinning (BR-180, BR-181, BR-187), pure
# ---------------------------------------------------------------------------------------------


def pinned_whole(placement_pin: str | None, pinning: str, sensitive: bool) -> bool:
    """The placement pin `on`, otherwise (pin at `inherit`) the effective `pinning` `on`, or
    `auto` on a sensitive placement (BR-180). An explicit `off` holds wherever it resolves."""
    if placement_pin is not None:
        return placement_pin == "on"
    return pinning == "on" or (pinning == "auto" and sensitive)


def is_pinned(tool_pin: bool, has_rule: bool, placement_pin: str | None, pinning: str,
              sensitive: bool) -> bool:  # fmt: skip
    """BR-180: a tool pin or an argument rule pins the tool, otherwise the placement does."""
    return tool_pin or has_rule or pinned_whole(placement_pin, pinning, sensitive)


def canonical(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The hashed part of an upstream definition (BR-181): the fields present among `title`,
    `description`, `inputSchema`, `outputSchema`, `annotations`."""
    return {k: definition[k] for k in PIN_FIELDS if k in definition}


def definition_hash(definition: Mapping[str, Any], *, lenient: bool = False) -> tuple[str, str]:
    """(SHA-256, canonical JSON text) of the upstream definition, never the narrowed copy.

    Text that UTF-8 cannot encode (a lone surrogate) raises: the relay then fails the listing
    closed (BR-185). `lenient` is for the operator's readers (`tool diff`): such a character is
    hashed through `surrogatepass`, which leaves every encodable text's hash unchanged, so the
    hostile definition reads as a change instead of ending the command (lot B review, N2)."""
    text = json.dumps(canonical(definition), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)  # fmt: skip
    data = text.encode("utf-8", "surrogatepass" if lenient else "strict")
    return hashlib.sha256(data).hexdigest(), text


# ---------------------------------------------------------------------------------------------
# State of a role, loaded once per request
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArgumentRule:
    argument: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlacementState:
    connection_id: str
    role_id: str
    service_id: str
    mode: str  # effective
    mode_origin: str  # `placement` or the path that carries the setting
    own_mode: str | None
    pin: str | None
    sensitive: bool
    sensitive_via: tuple[str, ...]  # sensitive roles of a shared connection (D3)
    lists: dict[str, frozenset[str]]
    rules: dict[str, ArgumentRule]
    pinning: str = "auto"  # effective setting on the role (BR-048)
    tool_pins: frozenset[str] = frozenset()
    baselined: frozenset[str] = frozenset()  # tools holding a baseline
    suspended: frozenset[str] = frozenset()  # `suspended_at` set: the state of record (BR-181)

    @property
    def allowlist_in_force(self) -> bool:
        return self.sensitive or bool(self.lists["allowlist"])

    @property
    def whole(self) -> bool:
        """Pinned whole: every tool of the placement is pinned (BR-180)."""
        return pinned_whole(self.pin, self.pinning, self.sensitive)

    def pinned(self, upstream_name: str) -> bool:
        return is_pinned(upstream_name in self.tool_pins, upstream_name in self.rules, self.pin,
                         self.pinning, self.sensitive)  # fmt: skip

    def facts(self, upstream_name: str, cls: str) -> Facts:
        return Facts(
            cls=cls,
            mode=self.mode,
            sensitive=self.sensitive,
            opened=upstream_name in self.lists["open"],
            denied=upstream_name in self.lists["deny"],
            allowlisted=upstream_name in self.lists["allowlist"],
            allowlist_in_force=self.allowlist_in_force,
            suspended=upstream_name in self.suspended,
        )


@dataclass(frozen=True, slots=True)
class RoleState:
    """The policy state of one role, read in one pass; implements `Policy`."""

    role_id: str
    placements: dict[str, PlacementState]
    manual: dict[tuple[str, str], str] = field(default_factory=dict)

    def classify(self, connection_id: str, upstream_name: str,
                 definition: Mapping[str, Any]) -> tuple[str, str]:  # fmt: skip
        p = self.placements[connection_id]
        return class_of(self.manual.get((p.service_id, upstream_name)),
                        annotation_class(definition))  # fmt: skip

    def verdict(self, connection_id: str, upstream_name: str,
                definition: Mapping[str, Any]) -> Verdict:  # fmt: skip
        cls, _ = self.classify(connection_id, upstream_name, definition)
        return evaluate(self.placements[connection_id].facts(upstream_name, cls))

    def rule(self, connection_id: str, upstream_name: str) -> ArgumentRule | None:
        p = self.placements.get(connection_id)
        return p.rules.get(upstream_name) if p is not None else None

    def pinned(self, connection_id: str, upstream_name: str) -> bool:
        return self.placements[connection_id].pinned(upstream_name)

    def decide(
        self,
        role_id: str,
        connection_id: str,
        upstream_name: str,
        definition: Mapping[str, Any],
        arguments: Mapping[str, Any] | None,
    ) -> Decision:
        if role_id != self.role_id or connection_id not in self.placements:
            raise LookupError("placement absent from the loaded state")
        v = self.verdict(connection_id, upstream_name, definition)
        if not v.exposed:
            return HIDE if arguments is None else Refuse(v.reasons[0])
        if arguments is None:
            return ALLOW
        rule = self.rule(connection_id, upstream_name)
        if rule is not None and not value_allowed(rule, arguments):
            return Refuse(ARGUMENT_VALUE)
        return ALLOW


class Engine:
    """The real policy: `load` reads a role's state from the database at each request."""

    def __repr__(self) -> str:
        return "Engine()"

    def load(self, conn: sqlite3.Connection, role_id: str, tree: Tree | None = None) -> RoleState:
        t = tree if tree is not None else Tree.load(conn)
        role = t.nodes.get(role_id)
        placements: dict[str, PlacementState] = {}
        if role is None:
            return RoleState(role_id, placements)
        rows = conn.execute(
            "SELECT p.connection_id, p.mode, p.pin, c.service_id FROM placement p"
            " JOIN connection c ON c.id = p.connection_id AND c.removed = 0"
            " WHERE p.role_id = ?",
            (role_id,),
        ).fetchall()
        lists: dict[str, dict[str, set[str]]] = {r[0]: {k: set() for k in LISTS} for r in rows}
        for cid, which, name in conn.execute(
            "SELECT connection_id, list, upstream_name FROM placement_tool WHERE role_id = ?",
            (role_id,),
        ):
            if cid in lists:
                lists[cid][which].add(name)
        rules: dict[str, dict[str, ArgumentRule]] = {r[0]: {} for r in rows}
        for cid, name, argument, values in conn.execute(
            "SELECT connection_id, upstream_name, argument, allowed_values FROM argument_rule"
            " WHERE role_id = ?",
            (role_id,),
        ):
            if cid in rules:
                rules[cid][name] = stored_rule(argument, values)
        pins: dict[str, dict[str, set[str]]] = {
            r[0]: {"tool": set(), "baselined": set(), "suspended": set()} for r in rows
        }
        for cid, name, tool_pin, baselined, suspended in conn.execute(
            "SELECT connection_id, upstream_name, tool_pin, sha256 IS NOT NULL,"
            " suspended_at IS NOT NULL FROM pinned_definition WHERE role_id = ?",
            (role_id,),
        ):
            if cid in pins:
                for which, on in (("tool", tool_pin), ("baselined", baselined),
                                  ("suspended", suspended)):  # fmt: skip
                    if on:
                        pins[cid][which].add(name)
        pinning, _ = t.effective(role, "pinning")
        services = {r[3] for r in rows}
        manual: dict[tuple[str, str], str] = {}
        if services:
            marks = ",".join("?" * len(services))
            for sid, name, cls in conn.execute(
                "SELECT service_id, upstream_name, manual_class FROM tool_catalog"  # noqa: S608
                f" WHERE manual_class IS NOT NULL AND service_id IN ({marks})",
                sorted(services),
            ):
                manual[(sid, name)] = cls
        inherited, origin = t.effective(role, "mode")
        for cid, own, pin, sid in rows:
            via = sensitive_roles(conn, t, cid)
            sensitive = bool(via)
            placements[cid] = PlacementState(
                connection_id=cid,
                role_id=role_id,
                service_id=sid,
                mode=own or inherited,
                mode_origin="placement" if own else t.path(origin),
                own_mode=own,
                pin=pin,
                sensitive=sensitive,
                sensitive_via=tuple(sorted(t.path(r) for r in via))
                if sensitive and not t.is_sensitive(role)
                else (),
                lists={k: frozenset(v) for k, v in lists[cid].items()},
                rules=rules[cid],
                pinning=pinning,
                tool_pins=frozenset(pins[cid]["tool"]),
                baselined=frozenset(pins[cid]["baselined"]),
                suspended=frozenset(pins[cid]["suspended"]),
            )
        return RoleState(role_id, placements, manual)


ENGINE = Engine()


def stored_rule(argument: str, values: str) -> ArgumentRule:
    """A stored argument rule; a row that cannot be loaded is a `StateError`, never an uncaught
    decoding error, so every reader of the state fails closed the same way (BR-185)."""
    try:
        loaded = json.loads(values)
    except ValueError:
        raise StateError("argument rule unreadable") from None
    if not isinstance(loaded, list) or not all(isinstance(v, str) for v in loaded):
        raise StateError("argument rule unreadable")
    return ArgumentRule(argument, tuple(loaded))


def sensitive_roles(conn: sqlite3.Connection, tree: Tree, connection_id: str) -> list[Node]:
    """Sensitive roles that carry the connection: a placement is sensitive iff this is not empty
    (BR-186, the maximum of the connection's placements, D3)."""
    roles = [tree.nodes[i] for i in _placements(conn, connection_id) if i in tree.nodes]
    return [r for r in roles if tree.is_sensitive(r)]


# ---------------------------------------------------------------------------------------------
# Tool catalogue (BR-173), fed by every listing
# ---------------------------------------------------------------------------------------------


def record_listing(
    conn: sqlite3.Connection,
    service_id: str,
    definitions: Mapping[str, Mapping[str, Any]],
    now: float,
    *,
    actor: str,
    node_id: str | None,
    path: str,
    role_path: str,
) -> list[str]:
    """In the caller's transaction: every name of the listing (names BR-110 accepted) enters the
    catalogue, a new one also the queue (`tool.discovered`); the annotation class is refreshed;
    a catalogue name the listing no longer announces leaves the queue marked `removed`. Returns
    the new names."""
    known = {
        r[0]: (r[1], bool(r[2]))
        for r in conn.execute(
            "SELECT upstream_name, annotation_class, removed FROM tool_catalog"
            " WHERE service_id = ?",
            (service_id,),
        )
    }
    created: list[str] = []
    for name, definition in definitions.items():
        annotated = annotation_class(definition)
        if name not in known:
            conn.execute(
                "INSERT INTO tool_catalog (service_id, upstream_name, annotation_class, queued,"
                " removed, first_seen) VALUES (?, ?, ?, 1, 0, ?)",
                (service_id, name, annotated, int(now)),
            )
            created.append(name)
            audit.write(
                conn,
                actor=actor,
                action=A_DISCOVERED,
                result="ok",
                node_id=node_id,
                path=path,
                detail={"service": service_id, "tool": _label(name), "role": role_path},
                now=now,
            )
            continue
        was, removed = known[name]
        if was != annotated or removed:
            conn.execute(
                "UPDATE tool_catalog SET annotation_class = ?, removed = 0"
                " WHERE service_id = ? AND upstream_name = ?",
                (annotated, service_id, name),
            )
    gone = [n for n, (_, removed) in known.items() if not removed and n not in definitions]
    conn.executemany(
        "UPDATE tool_catalog SET removed = 1, queued = 0"
        " WHERE service_id = ? AND upstream_name = ?",
        [(service_id, n) for n in gone],
    )
    return created


class StateError(Exception):
    """The pinning state of a placement could not be read or written at a listing (BR-181), or a
    stored policy row cannot be loaded: the listing fails closed, read as `policy_error`
    (BR-185). `connection` names the connection when the reader knows it (lot B review, N2)."""

    def __init__(self, message: str = "", connection: str | None = None) -> None:
        super().__init__(message)
        self.connection = connection


@dataclass(frozen=True, slots=True)
class Suspension:
    """One suspension a listing wrote (BR-181, BR-187): the trace `tool.suspended` reads."""

    connection_id: str
    role_id: str
    upstream_name: str
    baseline: str | None  # None: a new tool (BR-187)
    sha256: str


def check_pins(
    conn: sqlite3.Connection,
    connection_id: str,
    role_id: str,
    definitions: Mapping[str, Mapping[str, Any]],
    now: float,
) -> list[Suspension]:
    """At each filled listing of a placement, in a transaction of its own (BR-181): the first
    listing under a pin records each pinned tool's baseline; a pinned tool whose definition no
    longer hashes to it is suspended; on a placement pinned whole that already holds baselines, a
    pinned tool without one is suspended as new (BR-187). A suspension is never lifted here, even
    when the upstream reverts; a tool the listing no longer announces is left as is. A failure
    raises: the caller fails the listing closed. Returns the suspensions written, one per
    (placement, tool, hash)."""
    out: list[Suspension] = []
    with transaction(conn):
        tree = Tree.load(conn)
        p = ENGINE.load(conn, role_id, tree).placements.get(connection_id)
        if p is None:
            return out
        rows = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT upstream_name, sha256, drift_sha256, suspended_at FROM pinned_definition"
                " WHERE connection_id = ? AND role_id = ?",
                (connection_id, role_id),
            )
        }
        holds = bool(p.baselined)
        for name, definition in definitions.items():
            if not p.pinned(name):
                continue  # a baseline stays while the pin is lifted, compared again if it returns
            digest, text = definition_hash(definition)
            baseline, drift, suspended_at = rows.get(name, (None, None, None))
            if baseline is None and suspended_at is None and not (p.whole and holds):
                conn.execute(
                    "INSERT INTO pinned_definition (connection_id, role_id, upstream_name, sha256,"
                    " definition) VALUES (?, ?, ?, ?, ?) ON CONFLICT (connection_id, role_id,"
                    " upstream_name) DO UPDATE SET sha256 = excluded.sha256,"
                    " definition = excluded.definition",
                    (connection_id, role_id, name, digest, text),
                )
                continue
            if digest == baseline or (suspended_at is not None and digest == drift):
                continue  # same hash, or this change is already suspended
            conn.execute(
                "INSERT INTO pinned_definition (connection_id, role_id, upstream_name,"
                " drift_sha256, drift_definition, suspended_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (connection_id, role_id, upstream_name) DO UPDATE SET"
                " drift_sha256 = excluded.drift_sha256,"
                " drift_definition = excluded.drift_definition,"
                " suspended_at = coalesce(pinned_definition.suspended_at, excluded.suspended_at)",
                (connection_id, role_id, name, digest, text, int(now)),
            )
            out.append(Suspension(connection_id, role_id, name, baseline, digest))
    return out


def suspension_audit(s: Suspension) -> dict[str, Any]:
    """Fields of the `tool.suspended` entry of a suspension: a trace, never the state."""
    return {
        "action": A_SUSPENDED,
        "result": "ok",
        "node_id": s.role_id,
        "before": {"sha256": s.baseline},
        "after": {"sha256": s.sha256, "suspended": True},
        "detail": {
            "connection_id": s.connection_id,
            "tool": _label(s.upstream_name),
            "new": s.baseline is None,
        },
    }


def _label(value: str) -> str:
    from hullgate.relay import safe_label  # circular: the relay imports this module

    return safe_label(value, 256)


@dataclass(frozen=True, slots=True)
class Queued:
    service: str
    name: str
    cls: str
    source: str
    proposal: str | None
    removed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "name": self.name,
            "class": self.cls,
            "source": self.source,
            "proposal": self.proposal,
            "removed": self.removed,
        }


def catalogue(
    conn: sqlite3.Connection,
    service_id: str | None = None,
    *,
    after: tuple[str, str] | None = None,
    limit: int | None = None,
) -> list[Queued]:
    return _catalogue_view(conn, service_id, queued=False, after=after, limit=limit)


def queue(conn: sqlite3.Connection, service_id: str | None = None) -> list[Queued]:
    return _catalogue_view(conn, service_id, queued=True)


def _catalogue_view(
    conn: sqlite3.Connection,
    service_id: str | None,
    *,
    queued: bool,
    after: tuple[str, str] | None = None,
    limit: int | None = None,
) -> list[Queued]:
    """`tool queue`: the tools waiting for a classification, per service and name."""
    sql = (
        "SELECT service_id, upstream_name, manual_class, annotation_class, removed"
        " FROM tool_catalog WHERE queued = ?"
    )
    args: tuple[int | str, ...] = (int(queued),)
    if service_id is not None:
        _service_known(conn, service_id)
        sql += " AND service_id = ?"
        args += (service_id,)
    if after is not None:
        sql += " AND (service_id, upstream_name) > (?, ?)"
        args += after
    sql += " ORDER BY 1, 2"
    if limit is not None:
        sql += " LIMIT ?"
        args += (limit,)
    out = []
    for sid, name, manual, annotated, removed in conn.execute(sql, args):
        cls, source = class_of(manual, annotated)
        out.append(Queued(sid, name, cls, source, proposal(name), bool(removed)))
    return out


def _service_known(conn: sqlite3.Connection, service_id: str) -> None:
    if conn.execute("SELECT 1 FROM service WHERE id = ?", (service_id,)).fetchone() is None:
        raise Refused(f"unknown service: {service_id}")


def _catalogued(
    conn: sqlite3.Connection, service_id: str, name: str
) -> tuple[str | None, str | None, bool]:
    row = conn.execute(
        "SELECT manual_class, annotation_class, queued FROM tool_catalog"
        " WHERE service_id = ? AND upstream_name = ?",
        (service_id, name),
    ).fetchone()
    if row is None:
        raise Refused(f"unknown tool for service {service_id}: {_label(name)} (connection tools)")
    return row[0], row[1], bool(row[2])


@dataclass(frozen=True, slots=True)
class Classified:
    service: str
    name: str
    cls: str
    source: str


def classify(
    conn: sqlite3.Connection, service_id: str, name: str, value: str, *, clock: Clock = time.time
) -> Classified:
    """`tool classify <service> <tool> read|write|destructive|clear` (BR-172, D10)."""
    if value not in (*CLASSES, "clear"):
        raise Refused(f"invalid class: {value!r} ({', '.join(CLASSES)}, clear)")
    with transaction(conn):
        now = clock()
        _service_known(conn, service_id)
        manual, annotated, queued = _catalogued(conn, service_id, name)
        before, _src = class_of(manual, annotated)
        new_manual = None if value == "clear" else value
        after, source = class_of(new_manual, annotated)
        if RANK[after] < RANK[before]:
            tree = Tree.load(conn)
            locker = _lock_over_service(conn, tree, service_id)
            if locker is not None:
                raise Refused(
                    f"locked by {tree.path(locker)}: lowering {_label(name)} from {before} to "
                    f"{after} would expose it under the lock"
                )
        if new_manual == manual and not queued:
            return Classified(service_id, name, after, source)  # no-op: nothing written
        conn.execute(
            "UPDATE tool_catalog SET manual_class = ?, queued = 0"
            " WHERE service_id = ? AND upstream_name = ?",
            (new_manual, service_id, name),
        )
        # BR-173, BR-188: an exit from the queue by `classify` is audited, even when the manual
        # class is unchanged (review 1 of lot A, N1)
        before_state: dict[str, Any] = {"manual_class": manual, "class": before}
        after_state: dict[str, Any] = {"manual_class": new_manual, "class": after}
        if new_manual == manual:
            before_state["queued"], after_state["queued"] = True, False
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_CLASSIFY,
            result="ok",
            path=service_id,
            before=before_state,
            after=after_state,
            detail={"service": service_id, "tool": _label(name)},
            now=now,
        )
        return Classified(service_id, name, after, source)


def _lock_over_service(conn: sqlite3.Connection, tree: Tree, service_id: str) -> Node | None:
    """A lock above a role where a live connection of the service is placed, or None."""
    for (role_id,) in conn.execute(
        "SELECT p.role_id FROM placement p JOIN connection c ON c.id = p.connection_id"
        " WHERE c.removed = 0 AND c.service_id = ? ORDER BY p.role_id",
        (service_id,),
    ):
        role = tree.nodes.get(role_id)
        if role is not None:
            locker = tree.locked_by(role)
            if locker is not None:
                return locker
    return None


def accept(
    conn: sqlite3.Connection, service_id: str, name: str, *, clock: Clock = time.time
) -> Classified:
    """`tool accept`: keeps the class of its current source, writes only the exit from the queue
    (BR-173)."""
    with transaction(conn):
        now = clock()
        _service_known(conn, service_id)
        manual, annotated, queued = _catalogued(conn, service_id, name)
        cls, source = class_of(manual, annotated)
        if not queued:
            raise Refused(f"not in the queue: {service_id} {_label(name)}")
        conn.execute(
            "UPDATE tool_catalog SET queued = 0 WHERE service_id = ? AND upstream_name = ?",
            (service_id, name),
        )
        audit.write(
            conn,
            actor=audit.actor(),
            action=A_ACCEPT,
            result="ok",
            path=service_id,
            before={"queued": True},
            after={"queued": False, "class": cls, "source": source},
            detail={"service": service_id, "tool": _label(name)},
            now=now,
        )
        return Classified(service_id, name, cls, source)


# ---------------------------------------------------------------------------------------------
# Placement policy: show, mode, lists, argument rules (BR-176 to BR-178)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    row: Row
    role: Node
    name: str
    path: str


def _target(conn: sqlite3.Connection, tree: Tree, text: str, role_path: str,
            *, writing: bool) -> Target:  # fmt: skip
    row = _find(conn, tree, text)
    role = _role(tree, role_path)
    name = display_name(tree.nodes, row)
    path = tree.path(role)
    if role.id not in _placements(conn, row.id):
        raise Refused(f"not placed: {name} on {path}")
    if writing:
        locker = tree.locked_by(role)
        if locker is not None:
            raise Refused(f"locked by {tree.path(locker)}: policy of {name} on {path}")
    return Target(row, role, name, path)


@dataclass(frozen=True, slots=True)
class Shown:
    connection: str
    role: str
    mode: str
    mode_origin: str
    pin: str
    sensitive: bool
    lists: dict[str, list[str]]
    arguments: dict[str, dict[str, Any]]
    pinned_tools: list[str] = field(default_factory=list)
    # the upstream names as recorded, in the order of the labels above (each list, the tool
    # pins; the argument rules by label): an act on an entry names the tool, not its label
    # (F-016 slice 3, review 579 N1); not part of `policy show --json`, which prints labels only
    names: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "role": self.role,
            "mode": self.mode,
            "mode_origin": self.mode_origin,
            "pin": self.pin,
            "sensitive": self.sensitive,
            **self.lists,
            "pinned_tools": self.pinned_tools,
            "arguments": self.arguments,
        }


def writable(conn: sqlite3.Connection, text: str, role_path: str) -> None:
    """The refusals of a policy write that need no upstream (placement, lock, BR-049), played
    before the listing `policy arg` makes at entry."""
    _target(conn, Tree.load(conn), text, role_path, writing=True)


def show(conn: sqlite3.Connection, text: str, role_path: str) -> Shown:
    """`policy show`: mode, pin, the three lists and the argument rules, no upstream exchange."""
    tree = Tree.load(conn)
    t = _target(conn, tree, text, role_path, writing=False)
    try:
        p = ENGINE.load(conn, t.role.id, tree).placements[t.row.id]
    except StateError as exc:  # named for the operator (BR-185; lot C review, N2)
        raise StateError(str(exc), t.name) from None
    lists = {k: _labelled(p.lists[k]) for k in LISTS}
    pins = _labelled(p.tool_pins)
    return Shown(
        t.name,
        t.path,
        p.mode,
        p.mode_origin,
        p.pin or "inherit",
        p.sensitive,
        {k: [label for label, _ in pairs] for k, pairs in lists.items()},
        {
            _label(n): {"argument": r.argument, "values": list(r.values)}
            for n, r in sorted(p.rules.items())
        },
        [label for label, _ in pins],
        {
            **{k: [name for _, name in pairs] for k, pairs in lists.items()},
            "pinned_tools": [name for _, name in pins],
            "arguments": {_label(n): n for n in sorted(p.rules)},
        },
    )


def _labelled(names: Iterable[str]) -> list[tuple[str, str]]:
    """(label, upstream name) pairs ordered by label, as `policy show` prints them."""
    return sorted((_label(n), n) for n in names)


def shown_under(conn: sqlite3.Connection, path: str = "/") -> list[Shown]:
    """`policy show` of every placement on a role under `path` (the admin API's read of a
    subtree, F-016 slice 3), ordered by role then connection; no upstream exchange."""
    tree = Tree.load(conn)
    scope = {n.id for n in tree.subtree(tree.resolve(path))}
    out: list[Shown] = []
    rows = conn.execute(
        "SELECT p.role_id, n.slug, c.name FROM placement p"
        " JOIN connection c ON c.id = p.connection_id"
        " JOIN node n ON n.id = c.client_node_id WHERE c.removed = 0"
    ).fetchall()
    for role_id, client_slug, name in rows:
        role = tree.nodes.get(role_id)
        if role is None or role_id not in scope:
            continue
        out.append(show(conn, f"{client_slug}/{name}", tree.path(role)))
    out.sort(key=lambda s: (s.role, s.connection))
    return out


def _write(conn: sqlite3.Connection, t: Target, action: str, before: object, after: object,
           now: float, **detail: object) -> None:  # fmt: skip
    audit.write(
        conn,
        actor=audit.actor(),
        action=action,
        result="ok",
        node_id=t.role.id,
        path=t.path,
        before=before,
        after=after,
        detail={"connection": t.name, "connection_id": t.row.id, **detail},
        now=now,
    )


def set_mode(
    conn: sqlite3.Connection, text: str, role_path: str, value: str, *, clock: Clock = time.time
) -> str:
    """`policy mode … read|write|destructive|inherit` (BR-177); returns the effective mode."""
    if value not in (*CLASSES, "inherit"):
        raise Refused(f"invalid mode: {value!r} ({', '.join(CLASSES)}, inherit)")
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        t = _target(conn, tree, text, role_path, writing=True)
        own = conn.execute(
            "SELECT mode FROM placement WHERE connection_id = ? AND role_id = ?",
            (t.row.id, t.role.id),
        ).fetchone()[0]
        new = None if value == "inherit" else value
        if own != new:
            conn.execute(
                "UPDATE placement SET mode = ? WHERE connection_id = ? AND role_id = ?",
                (new, t.row.id, t.role.id),
            )
            _write(conn, t, A_MODE, {"mode": own}, {"mode": new}, now)
        if new is not None:
            return new
        inherited, _ = tree.effective(t.role, "mode")
        return inherited


def _members(conn: sqlite3.Connection, t: Target, which: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT upstream_name FROM placement_tool"
            " WHERE connection_id = ? AND role_id = ? AND list = ?",
            (t.row.id, t.role.id, which),
        )
    }


def _which(which: str) -> str:
    if which not in LISTS:
        raise Refused(f"unknown list: {which!r} ({', '.join(LISTS)})")
    return which


def list_add(
    conn: sqlite3.Connection,
    text: str,
    role_path: str,
    which: str,
    names: Sequence[str],
    *,
    clock: Clock = time.time,
) -> list[str]:
    """`policy add … open|deny|allowlist <tool>...` (BR-176): every name must be in the
    catalogue of the connection's service; one `policy.list_add` per added name. Returns the
    names added (a name already listed is left as is)."""
    _which(which)
    if not names:
        raise Refused("policy add: at least one tool")
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        t = _target(conn, tree, text, role_path, writing=True)
        for name in names:
            _catalogued(conn, t.row.service_id, name)
        return _add(conn, t, which, list(dict.fromkeys(names)), now)


def _add(conn: sqlite3.Connection, t: Target, which: str, names: Iterable[str],
         now: float, **detail: object) -> list[str]:  # fmt: skip
    present = _members(conn, t, which)
    added = [n for n in names if n not in present]
    for name in added:
        conn.execute(
            "INSERT INTO placement_tool (connection_id, role_id, list, upstream_name)"
            " VALUES (?, ?, ?, ?)",
            (t.row.id, t.role.id, which, name),
        )
        _write(conn, t, A_LIST_ADD, {"listed": False}, {"listed": True}, now,
               list=which, tool=_label(name), upstream_name=name, **detail)  # fmt: skip
    return added


def list_add_class(
    conn: sqlite3.Connection,
    text: str,
    role_path: str,
    which: str,
    cls: str,
    *,
    clock: Clock = time.time,
) -> list[str]:
    """`policy add … allowlist --class read`: lists, once, the tools of the connection's service
    whose current class is `read` (D9); a read that appears later needs its own gesture."""
    if which != "allowlist" or cls != "read":
        raise Refused("--class: only `allowlist --class read`")
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        t = _target(conn, tree, text, role_path, writing=True)
        reads = [
            name
            for name, manual, annotated in conn.execute(
                "SELECT upstream_name, manual_class, annotation_class FROM tool_catalog"
                " WHERE service_id = ? AND removed = 0 ORDER BY upstream_name",
                (t.row.service_id,),
            )
            if class_of(manual, annotated)[0] == "read"
        ]
        return _add(conn, t, which, reads, now, by_class="read")


def add_to_list(
    conn: sqlite3.Connection,
    text: str,
    role_path: str,
    which: str,
    names: Sequence[str],
    by_class: str | None,
    *,
    clock: Clock = time.time,
) -> list[str]:
    """`policy add`, as the CLI and the admin API both call it (F-016 BR-211): tool names, or
    `--class read` on the allowlist, never both."""
    if by_class is None:
        return list_add(conn, text, role_path, which, names, clock=clock)
    if names:
        raise Refused("policy add: --class or tool names, not both")
    return list_add_class(conn, text, role_path, which, by_class, clock=clock)


def list_remove(
    conn: sqlite3.Connection,
    text: str,
    role_path: str,
    which: str,
    names: Sequence[str],
    *,
    clock: Clock = time.time,
) -> list[str]:
    """`policy remove … open|deny|allowlist <tool>...`: a name not in the list is refused."""
    _which(which)
    if not names:
        raise Refused("policy remove: at least one tool")
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        t = _target(conn, tree, text, role_path, writing=True)
        present = _members(conn, t, which)
        missing = [n for n in names if n not in present]
        if missing:
            raise Refused(f"not in {which}: {', '.join(_label(n) for n in missing)}")
        removed = list(dict.fromkeys(names))
        for name in removed:
            conn.execute(
                "DELETE FROM placement_tool WHERE connection_id = ? AND role_id = ? AND list = ?"
                " AND upstream_name = ?",
                (t.row.id, t.role.id, which, name),
            )
            _write(conn, t, A_LIST_REMOVE, {"listed": True}, {"listed": False}, now,
                   list=which, tool=_label(name), upstream_name=name)  # fmt: skip
        return removed


def check_rule(definition: Mapping[str, Any] | None, name: str, argument: str,
               values: Sequence[str]) -> None:  # fmt: skip
    """Entry validation of `policy arg` against the current upstream definition (BR-178)."""
    if definition is None:
        raise Refused(f"unknown tool upstream: {_label(name)} (not in the current listing)")
    if not values:
        raise Refused("policy arg: at least one value")
    schema = definition.get("inputSchema")
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(props, Mapping) or argument not in props:
        raise Refused(f"argument absent from the schema of {_label(name)}: {_label(argument)}")
    prop = props[argument]
    enum = prop.get("enum") if isinstance(prop, Mapping) else None
    if isinstance(enum, list):
        outside = [v for v in values if v not in enum]
        if outside:
            raise Refused(
                f"value outside the enum of {_label(argument)}: "
                f"{', '.join(_label(v) for v in outside)}"
            )


def argument_set(
    conn: sqlite3.Connection,
    text: str,
    role_path: str,
    name: str,
    argument: str,
    values: Sequence[str],
    definition: Mapping[str, Any] | None,
    *,
    clock: Clock = time.time,
) -> ArgumentRule:
    """`policy arg …`: one rule per (placement, tool), replaced by a new one; validated on the
    upstream definition listed at entry (`definition`), never recorded unvalidated. The rule
    pins the tool (BR-180): when it holds no baseline yet, the entry definition becomes its
    baseline in the same transaction (BR-181), so the schema the rule was validated on is the one
    a later listing compares, and a parameter renamed before that listing suspends the tool."""
    rule = ArgumentRule(argument, tuple(dict.fromkeys(values)))
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        t = _target(conn, tree, text, role_path, writing=True)
        _catalogued(conn, t.row.service_id, name)
        check_rule(definition, name, argument, values)
        try:
            before = _rule(conn, t, name)
        except StateError as exc:  # named for the operator (BR-185; lot C review, N2)
            raise StateError(str(exc), t.name) from None
        baseline = _baseline_at_entry(conn, t, name, definition)
        if before == rule:
            return rule
        conn.execute(
            "INSERT INTO argument_rule (connection_id, role_id, upstream_name, argument,"
            " allowed_values) VALUES (?, ?, ?, ?, ?) ON CONFLICT (connection_id, role_id,"
            " upstream_name) DO UPDATE SET argument = excluded.argument,"
            " allowed_values = excluded.allowed_values",
            (t.row.id, t.role.id, name, argument, json.dumps(list(rule.values))),
        )
        extra = {"baseline": baseline} if baseline is not None else {}
        _write(conn, t, A_ARGUMENT_SET, _rule_dict(before), _rule_dict(rule), now,
               tool=_label(name), upstream_name=name, **extra)  # fmt: skip
        return rule


def _baseline_at_entry(conn: sqlite3.Connection, t: Target, name: str,
                       definition: Mapping[str, Any] | None) -> str | None:  # fmt: skip
    """The entry definition as baseline of a tool that holds none and is not suspended; returns
    the SHA-256 recorded, or None when a baseline or a suspension was already there."""
    if definition is None:
        return None
    row = conn.execute(
        "SELECT sha256, suspended_at FROM pinned_definition"
        " WHERE connection_id = ? AND role_id = ? AND upstream_name = ?",
        (t.row.id, t.role.id, name),
    ).fetchone()
    if row is not None and (row[0] is not None or row[1] is not None):
        return None
    try:
        digest, text = definition_hash(definition)
    except UnicodeError:  # a lone surrogate: no baseline can hold it (lot C review, N1)
        raise Refused(
            f"{t.name}: the upstream definition of {_label(name)} cannot be encoded:"
            " nothing written"
        ) from None
    conn.execute(
        "INSERT INTO pinned_definition (connection_id, role_id, upstream_name, sha256,"
        " definition) VALUES (?, ?, ?, ?, ?) ON CONFLICT (connection_id, role_id,"
        " upstream_name) DO UPDATE SET sha256 = excluded.sha256,"
        " definition = excluded.definition",
        (t.row.id, t.role.id, name, digest, text),
    )
    return digest


def argument_remove(
    conn: sqlite3.Connection, text: str, role_path: str, name: str, *, clock: Clock = time.time
) -> ArgumentRule:
    with transaction(conn):
        now = clock()
        tree = Tree.load(conn)
        t = _target(conn, tree, text, role_path, writing=True)
        before = _rule(conn, t, name)
        if before is None:
            raise Refused(f"no argument rule: {_label(name)} on {t.name} for {t.path}")
        conn.execute(
            "DELETE FROM argument_rule WHERE connection_id = ? AND role_id = ?"
            " AND upstream_name = ?",
            (t.row.id, t.role.id, name),
        )
        _write(
            conn,
            t,
            A_ARGUMENT_REMOVE,
            _rule_dict(before),
            None,
            now,
            tool=_label(name),
            upstream_name=name,
        )
        return before


def set_pin(
    conn: sqlite3.Connection, text: str, role_path: str, value: str, *, clock: Clock = time.time
) -> str | None:
    """`policy pin … on|off|inherit` (BR-180): the placement pin, NULL for `inherit`; audited
    `policy.pin`, nothing written when unchanged. Returns the new own value."""
    if value not in PIN_VALUES:
        raise Refused(f"invalid pin: {value!r} ({', '.join(PIN_VALUES)})")
    with transaction(conn):
        now = clock()
        t = _target(conn, Tree.load(conn), text, role_path, writing=True)
        own = conn.execute(
            "SELECT pin FROM placement WHERE connection_id = ? AND role_id = ?",
            (t.row.id, t.role.id),
        ).fetchone()[0]
        new = None if value == "inherit" else value
        if own != new:
            conn.execute(
                "UPDATE placement SET pin = ? WHERE connection_id = ? AND role_id = ?",
                (new, t.row.id, t.role.id),
            )
            _write(conn, t, A_PIN, {"pin": own or "inherit"}, {"pin": value}, now)
        return new


def _tool_pin(conn: sqlite3.Connection, t: Target, name: str) -> bool:
    row = conn.execute(
        "SELECT tool_pin FROM pinned_definition"
        " WHERE connection_id = ? AND role_id = ? AND upstream_name = ?",
        (t.row.id, t.role.id, name),
    ).fetchone()
    return row is not None and bool(row[0])


def pin_tool(
    conn: sqlite3.Connection, text: str, role_path: str, name: str, *, clock: Clock = time.time
) -> bool:
    """`policy pin-tool`: pins one tool of the catalogue on the placement (BR-180); it adds to a
    placement that is not pinned and exempts nothing. False when it was already pinned so."""
    with transaction(conn):
        now = clock()
        t = _target(conn, Tree.load(conn), text, role_path, writing=True)
        _catalogued(conn, t.row.service_id, name)
        if _tool_pin(conn, t, name):
            return False
        conn.execute(
            "INSERT INTO pinned_definition (connection_id, role_id, upstream_name, tool_pin)"
            " VALUES (?, ?, ?, 1) ON CONFLICT (connection_id, role_id, upstream_name)"
            " DO UPDATE SET tool_pin = 1",
            (t.row.id, t.role.id, name),
        )
        _write(
            conn,
            t,
            A_PIN,
            {"tool_pin": False},
            {"tool_pin": True},
            now,
            tool=_label(name),
            upstream_name=name,
        )
        return True


def unpin_tool(
    conn: sqlite3.Connection, text: str, role_path: str, name: str, *, clock: Clock = time.time
) -> None:
    """`policy unpin-tool`: lifts a tool pin; the baseline and a suspension stay (BR-181)."""
    with transaction(conn):
        now = clock()
        t = _target(conn, Tree.load(conn), text, role_path, writing=True)
        if not _tool_pin(conn, t, name):
            raise Refused(f"no tool pin: {_label(name)} on {t.name} for {t.path}")
        conn.execute(
            "UPDATE pinned_definition SET tool_pin = 0"
            " WHERE connection_id = ? AND role_id = ? AND upstream_name = ?",
            (t.row.id, t.role.id, name),
        )
        conn.execute(
            "DELETE FROM pinned_definition WHERE connection_id = ? AND role_id = ?"
            " AND upstream_name = ? AND sha256 IS NULL AND suspended_at IS NULL",
            (t.row.id, t.role.id, name),
        )
        _write(
            conn,
            t,
            A_PIN,
            {"tool_pin": True},
            {"tool_pin": False},
            now,
            tool=_label(name),
            upstream_name=name,
        )


def _rule(conn: sqlite3.Connection, t: Target, name: str) -> ArgumentRule | None:
    row = conn.execute(
        "SELECT argument, allowed_values FROM argument_rule"
        " WHERE connection_id = ? AND role_id = ? AND upstream_name = ?",
        (t.row.id, t.role.id, name),
    ).fetchone()
    return None if row is None else stored_rule(row[0], row[1])


def _rule_dict(rule: ArgumentRule | None) -> dict[str, Any] | None:
    # the argument and the allowed values are the operator's configuration, never a call's value
    return None if rule is None else {"argument": rule.argument, "values": list(rule.values)}


def import_argument(
    conn: sqlite3.Connection,
    text: str,
    role_path: str,
    name: str,
    argument: str,
    values: Sequence[str],
    *,
    clock: Clock = time.time,
) -> None:
    """Restore an exported rule on a credentialless connection (BR-070, BR-236).

    Export carries no schema or baseline. The first authorised listing pins definitions again;
    until then a connection without credentials exposes nothing. Existing authorised connections
    use the fresh entry validation instead (the import orchestrator supplies it).
    """
    rule = ArgumentRule(argument, tuple(dict.fromkeys(values)))
    if not argument or not values:
        raise Refused("argument rule: a name and at least one value are required")
    with transaction(conn):
        t = _target(conn, Tree.load(conn), text, role_path, writing=True)
        _catalogued(conn, t.row.service_id, name)
        before = _rule(conn, t, name)
        if before == rule:
            return
        conn.execute(
            "INSERT INTO argument_rule (connection_id, role_id, upstream_name, argument,"
            " allowed_values) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (connection_id, role_id, upstream_name)"
            " DO UPDATE SET argument = excluded.argument, allowed_values = excluded.allowed_values",
            (t.row.id, t.role.id, name, argument, json.dumps(list(rule.values))),
        )
        _write(
            conn,
            t,
            A_ARGUMENT_SET,
            _rule_dict(before),
            _rule_dict(rule),
            clock(),
            tool=_label(name),
            upstream_name=name,
        )
