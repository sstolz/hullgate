"""MCP tool relay (F-008). Table of public names (BR-110), `connection tools` diagnostic
(§3.2), sessions and caches checked against the database (BR-112, BR-119) and, in lot B, the
`tools/list` / `tools/call` routing of a role's endpoint (`Relay`, BR-109 to BR-117,
BR-121 to BR-123, BR-126 to BR-129), under the action policy of F-015 (`hullgate.policy`).

**Order of a `tools/call`** (BR-113, §3.3). Stage 0: size of the arguments (BR-129). Stage 1,
in the database only, with no token and no network: public name → name table → connection placed
on the URL's role → state → declared upstream. Stage 2: `get_access` → upstream list if the cache
is cold or stale → name present in the list → policy (definition and arguments, policy state read
from the database at the request, BR-112) → upstream `tools/call`. Every refusal returns the same
"unknown tool" error; a policy refusal is audited with its reason (BR-185).

**Limits** (F-020). Before stage 0, the token check (BR-303): one count per token across its
roles, against the role's effective `token_rate_limit`. After the policy has accepted and before
the upstream `tools/call`, the connection check (BR-304): one count per connection across roles
and tokens, against the role's effective `connection_rate_limit`; placed there so that a name the
policy hides still answers "unknown tool" (BR-113), counted once across a 401 retry. A refusal is
the tool result `rate_limited (retry in N s)` (BR-116 v3), audited only through BR-312's
coalesced entry, never `relay.tool_call` (BR-122 v3).

**Progress and cancellation** (BR-120, lot C). A call whose client carries a progress token and
reads the response as a stream receives, in order and under its token, the progress the upstream
emits for that call; when the client closes the stream, the upstream exchange is cancelled
(`notifications/cancelled`, cut) and the call is audited `cancelled`, never replayed.

**Public name** (BR-110). `<prefix>__<cleaned upstream name>` (characters outside
`[A-Za-z0-9_-]` replaced by `_`), at most 64 characters for the whole name: beyond that, the
upstream part is truncated to 64 − prefix − 2 − 9, then suffixed with `_` and 8 hexadecimal digits
of the SHA-256 of the original upstream name; the second of two names that collide receives the
same suffix. The `tool_name` table is the source: the name given at the first announcement is
never recomputed. At most 1,000 names per connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import mcp_types

from hullgate import audit, lifecycle
from hullgate import limits as limits_mod
from hullgate.cipher import ConnectionsKey
from hullgate.connections import (
    UPSTREAM_UNDECLARED,
    Access,
    AccessError,
    NotAuthorized,
    NotPlaced,
    ReauthRequired,
    Row,
    TemporarilyUnavailable,
    _find,
    _placements,
    _service,
    display_name,
)
from hullgate.db import Database, StoreUnavailable, transaction
from hullgate.limits import RATE_LIMITED, Limits
from hullgate.outbound import Outbound
from hullgate.policy import (
    ENGINE,
    POLICY_ERROR,
    Allow,
    ArgumentRule,
    Engine,
    Policy,
    RoleState,
    StateError,
    Suspension,
    argument_set,
    check_pins,
    consult,
    narrowed,
    record_listing,
    sensitive_roles,
    suspension_audit,
    writable,
)
from hullgate.tree import CAP_MAX, Clock, Node, Refused, Tree, parse_cap, parse_rate
from hullgate.upstream_mcp import (
    CANCELLED,
    DEADLINE,
    INTERNAL,
    MAX_BYTES,
    REAUTHORIZE,
    REQUEST_TOO_LARGE,
    TOO_LARGE,
    UNAVAILABLE,
    UNCERTAIN,
    CallResult,
    Cancel,
    Progress,
    RpcError,
    Target,
    ToolList,
    Unauthorized,
    UpstreamClient,
    UpstreamError,
)

NAME_MAX = 64
SUFFIX_LEN = 9  # `_` + 8 hexadecimal digits
NAMES_PER_CONNECTION = 1000
AUDIT_NAME_MAX = 64
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

A_TOOLS_LIST = "connection.tools_list"
A_TOOLS_PURGE = "connection.tools_purge"
A_TOOL_NAME_INVALID = "connection.tool_name_invalid"
A_TOOLS_REFUSED = "connection.tools_refused"

# BR-184: the tool ran, its result is withheld, the call did not fail (the client decides,
# BR-117). Fixed text, starting with the code of BR-116's closed list.
WITHHELD = f"{TOO_LARGE} (the tool ran; its result was withheld: above the role's response cap)"

OVERFLOW = "too_many_names"
INVALID = "invalid_name"


# ---------------------------------------------------------------------------------------------
# Public names (BR-110)
# ---------------------------------------------------------------------------------------------


def clean(upstream: str) -> str:
    return _UNSAFE.sub("_", upstream)


def suffix(upstream: str) -> str:
    return "_" + hashlib.sha256(upstream.encode("utf-8")).hexdigest()[:8]


def public_name(prefix: str, upstream: str, *, collided: bool = False) -> str | None:
    """Candidate public name; None for an upstream name that is empty or only `_`."""
    part = clean(upstream)
    if not part.strip("_"):
        return None
    room = NAME_MAX - len(prefix) - 2
    if not collided and len(part) <= room:
        return f"{prefix}__{part}"
    return f"{prefix}__{part[: room - SUFFIX_LEN]}{suffix(upstream)}"


def safe_label(value: str, limit: int = AUDIT_NAME_MAX) -> str:
    """Value from an upstream or a client, truncated and filtered before it is written or
    shown (BR-122): never a control character nor a terminal sequence."""
    return _UNSAFE.sub("_", value[:limit])


@dataclass(frozen=True, slots=True)
class Assigned:
    names: dict[str, str]  # upstream name → public name, for the tools of the list
    created: tuple[str, ...]  # upstream names newly recorded
    invalid: tuple[str, ...]  # upstream names ignored (empty or only `_`), as they are


def names_of(conn: sqlite3.Connection, connection_id: str) -> dict[str, str]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT upstream_name, public_name FROM tool_name WHERE connection_id = ?",
            (connection_id,),
        )
    }


def assign(
    conn: sqlite3.Connection,
    connection_id: str,
    prefix: str,
    upstream_names: Iterable[str],
    now: float,
) -> Assigned:
    """Public names of the tools of a list, in the caller's transaction: names already recorded
    are read back, never recomputed; new ones are recorded. More than 1,000 names for the
    connection: `UpstreamError(upstream_unavailable, "too_many_names")`, nothing recorded."""
    known = names_of(conn, connection_id)
    taken = set(known.values())
    names: dict[str, str] = {}
    created: list[str] = []
    invalid: list[str] = []
    for upstream in dict.fromkeys(upstream_names):  # a repeated name counts once
        if upstream in known:
            names[upstream] = known[upstream]
            continue
        candidate = public_name(prefix, upstream)
        if candidate is None:
            invalid.append(upstream)
            continue
        if candidate in taken:
            candidate = public_name(prefix, upstream, collided=True)
            if candidate is None or candidate in taken:
                invalid.append(upstream)
                continue
        taken.add(candidate)
        names[upstream] = candidate
        created.append(upstream)
    if len(known) + len(created) > NAMES_PER_CONNECTION:
        raise UpstreamError("upstream_unavailable", OVERFLOW)
    conn.executemany(
        "INSERT INTO tool_name (connection_id, upstream_name, public_name, created_at)"
        " VALUES (?, ?, ?, ?)",
        [(connection_id, u, names[u], int(now)) for u in created],
    )
    return Assigned(names, tuple(created), tuple(invalid))


def purge(conn: sqlite3.Connection, connection_id: str, current: Iterable[str]) -> int:
    """Deletes the names absent from the current list (BR-110 remedy); returns the count deleted."""
    keep = set(current)
    gone = [u for u in names_of(conn, connection_id) if u not in keep]
    conn.executemany(
        "DELETE FROM tool_name WHERE connection_id = ? AND upstream_name = ?",
        [(connection_id, u) for u in gone],
    )
    return len(gone)


# ---------------------------------------------------------------------------------------------
# Checked against the database (BR-112, BR-119)
# ---------------------------------------------------------------------------------------------


def still_valid(conn: sqlite3.Connection, keys: Sequence[tuple[tuple[str, str], str]]) -> set[Any]:
    """(role, connection) keys whose session still holds, read from the database: live role,
    placement, live and `active` connection, same MCP URL (BR-119)."""
    tree = Tree.load(conn)
    ok: set[Any] = set()
    for (role_id, connection_id), url in keys:
        role = tree.nodes.get(role_id)
        if role is None or role.level != "role":
            continue
        row = conn.execute(
            "SELECT c.state, s.mcp_url FROM connection c JOIN service s ON s.id = c.service_id"
            " JOIN placement p ON p.connection_id = c.id AND p.role_id = ?"
            " WHERE c.id = ? AND c.removed = 0",
            (role_id, connection_id),
        ).fetchone()
        if row is None or row[0] != "active" or row[1] != url:
            continue
        ok.add((role_id, connection_id))
    return ok


def reconcile(run: lifecycle.Runner, client: UpstreamClient) -> list[tuple[str, str]]:
    """At each request served (lot B): every session key is checked against the database,
    whatever the request's key; what no longer holds is closed (BR-119, BR-045 clarified)."""
    keys = client.held()
    if not keys:
        return []
    valid = run(lambda c: still_valid(c, keys))
    return client.close_where(lambda k, _url: k in valid)


# ---------------------------------------------------------------------------------------------
# `connection tools <c> --role <path> [--purge-names --confirm <c>]` (§3.2)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Listed:
    connection: str
    role: str
    version: str
    tools: tuple[tuple[str, str], ...]  # (public name, filtered upstream name)
    invalid: int
    purged: int | None
    definitions: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # upstream name → definition
    names: dict[str, str] = field(default_factory=dict)  # upstream name → public name


@dataclass(frozen=True, slots=True)
class _Plan:
    row: Row
    name: str
    role: Node
    role_path: str
    target: Target
    prefix: str


def _refused(run: lifecycle.Runner, name: str, role_path: str, reason: str, now: float) -> None:
    def write(conn: sqlite3.Connection) -> None:
        with transaction(conn):
            audit.write(
                conn,
                actor=audit.actor(),
                action=A_TOOLS_REFUSED,
                result="refused",
                path=name,
                reason=reason,
                detail={"role": role_path},
                now=now,
            )

    run(write)


def argument_at_entry(
    run: lifecycle.Runner,
    key: ConnectionsKey,
    out: Outbound,
    client: UpstreamClient,
    text: str,
    role_path: str,
    tool: str,
    argument: str,
    values: Sequence[str],
    *,
    clock: Clock = time.time,
    prepared: ToolList | None = None,
) -> ArgumentRule:
    """`policy arg` (BR-178), as the CLI and the admin API both call it (F-016 BR-211): the
    refusals that need no upstream first, then a fresh listing at entry (unavailable → refused,
    nothing recorded), then the rule validated on the listed definition."""
    run(lambda conn: writable(conn, text, role_path))
    try:
        listed = tools(
            run, key, out, client, text, role_path, False, None, clock=clock, prepared=prepared
        )
    except UpstreamError as exc:
        raise Refused(f"upstream unavailable ({exc.code}): no rule recorded") from None
    except (lifecycle.Unavailable, AccessError):
        raise Refused("connection unavailable: no rule recorded") from None
    definition = listed.definitions.get(tool)
    return run(
        lambda conn: argument_set(
            conn, text, role_path, tool, argument, values, definition, clock=clock
        )
    )


def tools(
    run: lifecycle.Runner,
    key: ConnectionsKey,
    out: Outbound,
    client: UpstreamClient,
    text: str,
    role_path: str,
    purge_names: bool,
    confirm: str | None,
    *,
    clock: Clock = time.time,
    prepared: ToolList | None = None,
) -> Listed:
    """Diagnostic of a placed connection: lists, bypassing the cache, the tools the upstream
    announces with their public name, with no tool call. Every database check comes before any
    exchange (code 2, no exchange); the token comes from `get_access(connection, role)`; audited."""

    def check(conn: sqlite3.Connection) -> _Plan:
        tree = Tree.load(conn)
        row = _find(conn, tree, text)
        name = display_name(tree.nodes, row)
        role = tree.resolve(role_path)
        if role.level != "role":
            raise Refused(f"connection tools: --role names a role ({tree.path(role)})")
        path = tree.path(role)
        if purge_names and (confirm is None or confirm.strip("/") != name):
            raise Refused(f"confirmation required: --confirm {name}")
        if role.id not in _placements(conn, row.id):
            raise Refused(f"not placed: {name} on {path}")
        if row.state != "active":
            raise Refused(f"{name} is not active (state {row.state})")
        service = _service(conn, row.service_id)
        if service.upstream_type is None or service.mcp_url is None:
            raise Refused(f"{name}: {UPSTREAM_UNDECLARED} (service upstream {service.id})")
        return _Plan(row, name, role, path, Target(service.mcp_url, service.mcp_internal),
                     row.prefix)  # fmt: skip

    plan = run(check)
    # Composite acts prepare provider access/listing before acquiring their write lock.
    # Reuse that fresh response, but retain ALL ordinary name, catalogue and pinning checks.
    if prepared is not None:
        listing = prepared
    else:
        access = lifecycle.access(run, key, out, plan.row.id, plan.role.id, clock=clock)
        session_key = (plan.role.id, plan.row.id)
        try:
            try:
                listing = client.list_tools(session_key, plan.target, access.authorization)
            except RpcError as rpc:  # JSON-RPC error from the upstream: its message is never reused
                raise UpstreamError("upstream_unavailable", f"JSON-RPC error {rpc.code}") from None
        except UpstreamError as exc:
            _refused(run, plan.name, plan.role_path, exc.reason, clock())
            if purge_names:
                raise Refused(
                    f"current list unavailable ({exc.code}): nothing is deleted"
                ) from None
            raise
        finally:
            # the CLI holds no session (BR-119): the end is sent before the process exits
            client.close(session_key, wait=True)
    upstream_names = [t["name"] for t in listing.tools if isinstance(t.get("name"), str)]
    definitions: dict[str, dict[str, Any]] = {}
    for t in listing.tools:
        if isinstance(t.get("name"), str):
            definitions.setdefault(t["name"], t)

    def write(conn: sqlite3.Connection) -> Listed:
        with transaction(conn):
            now = clock()
            purged = purge(conn, plan.row.id, upstream_names) if purge_names else None
            assigned = assign(conn, plan.row.id, plan.prefix, upstream_names, now)
            accepted = {u: definitions[u] for u in assigned.names}
            record_listing(
                conn,
                plan.row.service_id,
                accepted,
                now,
                actor=audit.actor(),
                node_id=plan.role.id,
                path=plan.name,
                role_path=plan.role_path,
            )
            for bad in assigned.invalid:
                audit.write(
                    conn,
                    actor=audit.actor(),
                    action=A_TOOL_NAME_INVALID,
                    result="ignored",
                    path=plan.name,
                    reason=INVALID,
                    detail={"upstream_name": safe_label(bad), "role": plan.role_path},
                    now=now,
                )
            if purged is not None:
                audit.write(
                    conn,
                    actor=audit.actor(),
                    action=A_TOOLS_PURGE,
                    result="ok",
                    path=plan.name,
                    detail={"role": plan.role_path, "purged": purged},
                    now=now,
                )
            audit.write(
                conn,
                actor=audit.actor(),
                action=A_TOOLS_LIST,
                result="ok",
                path=plan.name,
                detail={
                    "role": plan.role_path,
                    "version": listing.version,
                    "tools": len(assigned.names),
                    "created": len(assigned.created),
                    "invalid": len(assigned.invalid),
                },
                now=now,
            )
            listed = tuple(sorted((pub, safe_label(up, 256)) for up, pub in assigned.names.items()))
            return Listed(
                plan.name, plan.role_path, listing.version, listed, len(assigned.invalid), purged,
                accepted, dict(assigned.names),
            )  # fmt: skip

    try:
        listed = run(write)
    except UpstreamError as exc:
        _refused(run, plan.name, plan.role_path, exc.reason, clock())
        raise Refused(
            f"{plan.name}: limit of 1,000 names reached (connection tools {plan.name} --role "
            f"{plan.role_path} --purge-names --confirm {plan.name})"
        ) from None
    # BR-181, BR-187: a listing of a placement is compared to its baselines, in its own
    # transaction; a failure fails the command, nothing is shown
    try:
        found = pins(run, plan.row.id, plan.role.id, listed.definitions, clock())
    except StateError as exc:  # named for the operator (BR-185; lot C review, N2)
        raise StateError(str(exc), plan.name) from None
    trace_suspensions(run, found, audit.actor(), plan.name, clock())
    return listed


def pins(run: lifecycle.Runner, connection_id: str, role_id: str,
         definitions: Mapping[str, Mapping[str, Any]], now: float) -> list[Suspension]:  # fmt: skip
    """`check_pins` in its own transaction: the state of record of a suspension (BR-181). Any
    failure but an unavailable database is a `StateError`: the listing fails closed."""
    try:
        return run(lambda conn: check_pins(conn, connection_id, role_id, definitions, now))
    except StoreUnavailable:
        raise
    except Exception as exc:
        log.exception("policy: pinning state unreadable or not written")
        raise StateError(str(exc)) from exc


def trace_suspensions(run: lifecycle.Runner, found: Sequence[Suspension], actor: str, path: str,
                      now: float) -> None:  # fmt: skip
    """One `tool.suspended` per (placement, tool, hash), after the state is written; an entry that
    cannot be written leaves `suspended_at` set (BR-181)."""
    for s in found:

        def write(conn: sqlite3.Connection, s: Suspension = s) -> None:
            with transaction(conn):
                audit.write(conn, actor=actor, path=path, now=now, **suspension_audit(s))

        try:
            run(write)
        except (StoreUnavailable, sqlite3.Error) as exc:
            print(f"audit not written (tool.suspended): {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# Endpoint relay (lot B)
# ---------------------------------------------------------------------------------------------

LIST_DEADLINE_S = 30.0  # BR-109, H21: overall deadline of the client's list
CACHE_TTL_S = 300.0  # BR-112
SIGNAL_PAUSE_MIN_S = 60.0  # BR-128, H18
SIGNAL_PAUSE_MAX_S = 15 * 60.0
MASK = "[masked]"  # BR-126
LIST_THREAD = "hullgate-list"

A_CALL = "relay.tool_call"
A_OMITTED = "relay.list_omitted"
A_UPSTREAM_REQUEST = "relay.upstream_request_refused"
A_SIGNAL_PAUSE = "relay.signal_pause"

REFUSED = "refused"
TOOL_ERROR = "tool_error"

# Reasons for omitting a connection from `tools/list` (BR-111), closed list.
M_DOWN = "outage"
M_DEADLINE = "deadline"
M_VERSION = "version"
M_SESSION = "session"
M_PAGINATION = "pagination"
M_POLICY_ERROR = POLICY_ERROR  # BR-185: evaluator exception at announcement

log = logging.getLogger("hullgate.relay")


class UnknownTool(Exception):
    """Name outside the list, the placement or the state, masked or refused by the policy: one
    single error for the client (BR-113, BR-123); `reason`, the reason of a policy refusal, is read
    only by the audit (BR-185)."""

    def __init__(self, name: str, reason: str | None = None) -> None:
        super().__init__(name)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Caller:
    """Role of the authorized URL and actor of the incoming token (never a request parameter)."""

    role_id: str
    role_path: str
    actor: str
    token_key: str = ""  # what the token's calls count by (BR-303); the actor when empty


class _RateLimited(Exception):
    """A tool-call limit refused the call (BR-303, BR-304); `delay` in seconds."""

    def __init__(self, delay: float) -> None:
        super().__init__(delay)
        self.delay = delay


@dataclass(frozen=True, slots=True)
class Carried:
    """Connection placed on a role, read from the database at the request."""

    id: str
    name: str
    prefix: str
    kind: str
    service_id: str
    state: str
    url: str | None
    internal: bool
    sensitive: bool

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        """What discards a cache entry when it changes (BR-112)."""
        return (self.state, self.url, self.internal, self.sensitive)

    @property
    def target(self) -> Target:
        if self.url is None:
            raise UpstreamError(UNAVAILABLE, UPSTREAM_UNDECLARED)
        return Target(self.url, self.internal)

    def serves(self) -> bool:
        return self.state == "active" and self.url is not None


def carried(conn: sqlite3.Connection, tree: Tree, role_id: str) -> list[Carried]:
    """Live connections placed on the role, in the order of their placement."""
    rows = conn.execute(
        "SELECT c.id, c.client_node_id, c.name, c.prefix, c.kind, c.service_id, c.state,"
        " s.upstream_type,"
        " s.mcp_url, s.mcp_internal FROM placement p"
        " JOIN connection c ON c.id = p.connection_id AND c.removed = 0"
        " JOIN service s ON s.id = c.service_id"
        " WHERE p.role_id = ? ORDER BY p.created_at, c.prefix",
        (role_id,),
    ).fetchall()
    out: list[Carried] = []
    for cid, client, short, prefix, kind, service_id, state, utype, url, internal in rows:
        node = tree.nodes.get(client)
        out.append(
            Carried(
                cid,
                f"{node.slug if node is not None else '?'}/{short}",
                prefix,
                kind,
                service_id,
                state,
                url if utype is not None else None,
                bool(internal),
                bool(sensitive_roles(conn, tree, cid)),
            )
        )
    return out


def _rate_values(conn: sqlite3.Connection, role_id: str) -> tuple[int, int] | None:
    """Effective `token_rate_limit` and `connection_rate_limit` of the role called, per minute
    (BR-303, BR-304, BR-048); None when the role is gone (stage 1 answers then)."""
    tree = Tree.load(conn)
    role = tree.nodes.get(role_id)
    if role is None or role.level != "role":
        return None
    return (
        parse_rate(tree.effective(role, "token_rate_limit")[0]),
        parse_rate(tree.effective(role, "connection_rate_limit")[0]),
    )


@dataclass
class _Entry:
    fingerprint: tuple[Any, ...]
    version: str
    tools: tuple[dict[str, Any], ...]  # upstream definitions, in the order of the list
    names: dict[str, str]  # upstream name → public name
    filled: float


@dataclass
class _Pause:
    not_before: float = 0.0
    step: int = 0


def _mask(value: Any, secret: str) -> Any:
    """BR-126: each exact occurrence of the token value in the texts becomes `[masked]`;
    nothing else is changed."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, MASK) if secret in value else value
    if isinstance(value, list):
        return [_mask(v, secret) for v in value]
    if isinstance(value, dict):
        return {k: _mask(v, secret) for k, v in value.items()}
    return value


def _bearer_value(authorization: str) -> str:
    scheme, _, value = authorization.partition(" ")
    return value if scheme.lower() == "bearer" else authorization


def error_result(code: str, retry_after: float | None = None) -> dict[str, Any]:
    """Result of a gateway failure (BR-116): code from the closed list, with no token, URL or
    body of the upstream."""
    text = WITHHELD if code == TOO_LARGE else code
    if retry_after is not None and retry_after > 0:
        text = f"{code} (retry in {math.ceil(retry_after)} s)"
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _omission_reason(exc: UpstreamError) -> str:
    if exc.code == DEADLINE:
        return M_DEADLINE
    if exc.reason in (M_VERSION, M_SESSION, M_PAGINATION, OVERFLOW):
        return exc.reason
    return M_DOWN


def _compact(value: Any) -> bytes:
    """Compact JSON serialization, the one the upstream receives (BR-129)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Relay:
    """Relay of the server process: one outbound OAuth client and one upstream client, built
    once (BR-125); cache of definitions per (role, connection) (BR-112); "not before" signalling
    dates per connection (BR-128). Blocking: the endpoint calls it outside the event loop."""

    def __init__(
        self,
        db: Database,
        key: ConnectionsKey,
        out: Outbound,
        client: UpstreamClient,
        *,
        policy: Policy | Engine = ENGINE,
        clock: Clock = time.time,
        limits: Limits | None = None,
    ) -> None:
        self.db = db
        self.limits = limits if limits is not None else Limits()  # F-020, in memory
        self.key = key
        self.out = out
        self.client = client
        self.policy = policy  # ENGINE; another `Policy` only serves the tests of the seam
        self.clock = clock  # time of the writes and of `get_access`
        self.run: lifecycle.Runner = db.call
        self._cache: dict[tuple[str, str], _Entry] = {}
        self._filling: dict[tuple[str, str], threading.Lock] = {}
        self._pauses: dict[str, _Pause] = {}
        self._lock = threading.Lock()
        self._local = threading.local()  # current call served as a stream (audit reading, BR-120)
        client.on_upstream_request = self._upstream_request

    def __repr__(self) -> str:
        return "Relay()"

    # -- clocks of the upstream client (injected by the tests) ---------------------------------

    def _mono(self) -> float:
        return self.client.transport.clock.monotonic()

    def _wall(self) -> float:
        return self.client.transport.clock.wall()

    def cached(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._cache)

    # -- checked against the database, at each request served (BR-112, BR-119, BR-045) --------

    def reconcile(self) -> None:
        cached = self.cached()
        held = self.client.held()
        if not cached and not held:
            return

        def read(conn: sqlite3.Connection) -> tuple[set[Any], dict[tuple[str, str], Any]]:
            tree = Tree.load(conn)
            prints: dict[tuple[str, str], Any] = {}
            for role_id in {k[0] for k in cached}:
                role = tree.nodes.get(role_id)
                found = carried(conn, tree, role_id) if role and role.level == "role" else []
                for c in found:
                    prints[(role_id, c.id)] = c.fingerprint if c.serves() else None
            return (still_valid(conn, held) if held else set()), prints

        valid, prints = self.run(read)
        with self._lock:
            for k in cached:
                entry = self._cache.get(k)
                if entry is not None and prints.get(k) != entry.fingerprint:
                    del self._cache[k]
        if held:
            self.client.close_where(lambda k, _url: k in valid)

    # -- policy state, read at each request (BR-112, F-015) -----------------------------------

    def _state(self, conn: sqlite3.Connection, tree: Tree, role_id: str) -> Policy:
        if isinstance(self.policy, Engine):
            return self.policy.load(conn, role_id, tree)
        return self.policy

    def _policy(self, role_id: str) -> Policy | None:
        """Policy state of the role, read after the lists of the request are filled so that a
        suspension they have just written already applies (BR-181); an unreadable state is
        `None`: each connection is then omitted as `policy_error` (BR-185)."""

        def read(conn: sqlite3.Connection) -> Policy | None:
            try:
                return self._state(conn, Tree.load(conn), role_id)
            except Exception:
                log.exception("policy: unreadable state")
                return None

        return self.run(read)

    # -- tools/list (BR-109 to BR-112, BR-123, BR-179, BR-185) --------------------------------

    def list_tools(self, caller: Caller) -> list[dict[str, Any]]:
        conns = self.run(lambda conn: carried(conn, Tree.load(conn), caller.role_id))
        started = time.monotonic()
        results: dict[str, _Entry | UpstreamError | UnknownTool | StateError] = {}
        workers: list[tuple[Carried, threading.Thread]] = []
        for c in conns:
            if not c.serves():
                continue

            def fetch(c: Carried = c) -> None:
                try:
                    results[c.id] = self._listing(c, caller)
                except (UpstreamError, UnknownTool, StateError) as exc:
                    results[c.id] = exc
                except Exception:  # gateway failure: omitted, never propagated
                    log.exception("tool list: unexpected error")
                    results[c.id] = UpstreamError(INTERNAL, "internal error")

            t = threading.Thread(target=fetch, name=LIST_THREAD, daemon=True)
            t.start()
            workers.append((c, t))
        deadline = started + LIST_DEADLINE_S
        for _, t in workers:
            t.join(max(0.0, deadline - time.monotonic()))
        policy = self._policy(caller.role_id) if workers else None
        tools: list[dict[str, Any]] = []
        for c, t in workers:
            got = None if t.is_alive() else results.get(c.id)
            elapsed = time.monotonic() - started
            if got is None:
                self._audit_omitted(caller, c, M_DEADLINE, DEADLINE, elapsed)
                continue
            if isinstance(got, UnknownTool):
                continue  # race with a placement removal: the connection is no longer there
            if isinstance(got, UpstreamError):
                self._audit_omitted(caller, c, _omission_reason(got), got.code, elapsed)
                continue
            if isinstance(got, StateError):  # BR-181: pinning state missing, list fails closed
                self._audit_omitted(caller, c, M_POLICY_ERROR, REFUSED, elapsed)
                continue
            announced = self._announced(caller, c, got, policy)
            if announced is None:  # BR-185: evaluator exception, connection emptied and traced
                self._audit_omitted(caller, c, M_POLICY_ERROR, REFUSED, elapsed)
                continue
            tools.extend(announced)
        return tools

    def _announced(
        self, caller: Caller, c: Carried, got: _Entry, policy: Policy | None
    ) -> list[dict[str, Any]] | None:
        """The exposed tools of a connection, enum narrowed (BR-179); None if the policy raises."""
        if policy is None:
            return None
        if isinstance(policy, RoleState) and c.id not in policy.placements:
            return []  # race with a placement removal: the connection is no longer there
        out: list[dict[str, Any]] = []
        for definition in got.tools:
            upstream = definition["name"]
            public = got.names.get(upstream)
            if public is None:
                continue
            try:
                decision = policy.decide(caller.role_id, c.id, upstream, definition, None)
            except Exception:
                log.exception("policy: exception at announcement")
                return None
            if not isinstance(decision, Allow):
                continue
            rule = policy.rule(c.id, upstream) if isinstance(policy, RoleState) else None
            shown = narrowed(definition, rule) if rule is not None else dict(definition)
            out.append({**shown, "name": public})
        return out

    def _listing(self, c: Carried, caller: Caller) -> _Entry:
        """A fresh cache entry without contacting the upstream, otherwise one fetch per key."""
        entry = self._fresh(c, caller)
        if entry is not None:
            return entry
        return self._with_token(c, caller, lambda access: self._entry(c, caller, access))

    def _fresh(self, c: Carried, caller: Caller) -> _Entry | None:
        with self._lock:
            entry = self._cache.get((caller.role_id, c.id))
        if entry is None or entry.fingerprint != c.fingerprint:
            return None
        if self._mono() - entry.filled >= CACHE_TTL_S:
            return None
        return entry

    def _entry(self, c: Carried, caller: Caller, access: Access) -> _Entry:
        key = (caller.role_id, c.id)
        with self._lock:
            filling = self._filling.setdefault(key, threading.Lock())
        with filling:  # one fetch at a time per key (BR-112)
            entry = self._fresh(c, caller)
            if entry is not None:
                return entry
            try:
                listing = self.client.list_tools(key, c.target, access.authorization)
            except RpcError as rpc:
                raise UpstreamError(UNAVAILABLE, f"JSON-RPC error {rpc.code}") from None
            entry = self._named(c, caller, listing.version, listing.tools)
            with self._lock:
                self._cache[key] = entry
            return entry

    def _named(
        self, c: Carried, caller: Caller, version: str, tools: Sequence[dict[str, Any]]
    ) -> _Entry:
        defs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for t in tools:
            name = t.get("name")
            if not isinstance(name, str) or name in seen:
                continue  # a repeated name counts once (BR-110)
            try:
                mcp_types.Tool.model_validate(t)
            except ValueError:
                raise UpstreamError(UNAVAILABLE, "invalid tool list") from None
            seen.add(name)
            defs.append(t)

        def write(conn: sqlite3.Connection) -> Assigned:
            with transaction(conn):
                now = self.clock()
                got = assign(conn, c.id, c.prefix, [d["name"] for d in defs], now)
                accepted = {d["name"]: d for d in defs if d["name"] in got.names}
                record_listing(conn, c.service_id, accepted, now, actor=caller.actor,
                               node_id=caller.role_id, path=c.name,
                               role_path=caller.role_path)  # fmt: skip
                for bad in got.invalid:
                    audit.write(
                        conn,
                        actor=caller.actor,
                        action=A_TOOL_NAME_INVALID,
                        result="ignored",
                        node_id=caller.role_id,
                        path=c.name,
                        reason=INVALID,
                        detail={"upstream_name": safe_label(bad), "role": caller.role_path},
                        now=now,
                    )
                return got

        got = self.run(write)
        # BR-181, BR-187: the filled listing is compared to the placement's baselines in its own
        # transaction; a failure propagates and the listing fails closed (nothing cached)
        accepted = {d["name"]: d for d in defs if d["name"] in got.names}
        found = pins(self.run, c.id, caller.role_id, accepted, self.clock())
        trace_suspensions(self.run, found, lifecycle.GATEWAY, c.name, self.clock())
        return _Entry(c.fingerprint, version, tuple(defs), dict(got.names), self._mono())

    # -- tools/call (BR-113 to BR-117, BR-126 to BR-129, BR-178, BR-185) ----------------------

    def call_tool(
        self,
        caller: Caller,
        name: str,
        arguments: Mapping[str, Any] | None,
        *,
        progress: Progress | None = None,
        cancel: Cancel | None = None,
    ) -> dict[str, Any]:
        """`progress`: the client carries a progress token and reads the response as a stream;
        `cancel`: set when it closes the stream of the call (BR-120)."""
        self._local.stream = progress is not None
        try:
            return self._call_tool(caller, name, arguments, progress, cancel)
        finally:
            self._local.stream = False

    def _call_tool(
        self,
        caller: Caller,
        name: str,
        arguments: Mapping[str, Any] | None,
        progress: Progress | None,
        cancel: Cancel | None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        # token check, before anything else (BR-303); the values are read from the base at each
        # call, an unreadable base answering as stage 1's read does (F-020 §3.9)
        values = self.run(lambda c: _rate_values(c, caller.role_id))
        token_value, connection_value = values or (0, 0)
        token_key = caller.token_key or caller.actor
        if values is not None:
            delay = self.limits.tokens.admit(token_key, token_value)
            if delay is not None:
                return self._rate_limited(limits_mod.TOKEN, token_key, caller, delay)
        counted: list[bool] = []
        args = dict(arguments or {})
        sent = len(_compact(args))
        if sent > MAX_BYTES:  # stage 0 (BR-129)
            self._audit_call(caller, None, name, None, None, REQUEST_TOO_LARGE, started, sent, 0)
            return error_result(REQUEST_TOO_LARGE)
        found = self.run(lambda c: self._stage1(c, caller.role_id, name))
        if found is None:  # stage 1: 0 get_access, 0 exchange
            self._audit_call(caller, None, name, None, None, REFUSED, started, sent, 0)
            raise UnknownTool(name)
        c, upstream, loaded, cap = found
        held: list[Access] = []
        version: list[str] = []

        def attempt(access: Access) -> CallResult:
            held[:] = [access]
            if cancel is not None and cancel.is_set():  # gone before the list (review 470, m2)
                raise UpstreamError(CANCELLED, "cancelled by the client")
            policy = loaded
            entry = self._fresh(c, caller)
            if entry is None:  # filled here: a suspension it writes already applies (BR-181)
                try:
                    entry = self._entry(c, caller, access)
                except StateError:  # unreadable pinning state: policy refusal (BR-185)
                    raise UnknownTool(name, POLICY_ERROR) from None
                policy = self._policy(caller.role_id)
            version[:] = [entry.version]
            definition = next((d for d in entry.tools if d["name"] == upstream), None)
            if definition is None or entry.names.get(upstream) != name:
                raise UnknownTool(name)
            if policy is None:
                raise UnknownTool(name, POLICY_ERROR)
            ok, reason = consult(policy, caller.role_id, c.id, upstream, definition, args)
            if not ok:
                raise UnknownTool(name, reason)
            if not counted and values is not None:  # connection check, once per call (BR-304)
                counted.append(True)
                delay = self.limits.connections.admit(c.id, connection_value)
                if delay is not None:
                    raise _RateLimited(delay)
            return self.client.call_tool(
                (caller.role_id, c.id), c.target, access.authorization, upstream, args,
                definition.get("inputSchema"), progress=progress, cancel=cancel,
            )  # fmt: skip

        try:
            if cancel is not None and cancel.is_set():  # gone before `get_access` (m2)
                raise UpstreamError(CANCELLED, "cancelled by the client")
            outcome = self._with_token(c, caller, attempt)
        except UnknownTool as refusal:
            self._audit_call(caller, c, name, upstream, None, REFUSED, started, sent, 0,
                             refusal.reason)  # fmt: skip
            raise
        except _RateLimited as limited:
            return self._rate_limited(limits_mod.CONNECTION, c.id, caller, limited.delay)
        except RpcError as rpc:
            failure = UpstreamError(UNAVAILABLE, f"JSON-RPC error {rpc.code}")
            return self._failed(caller, c, name, upstream, version, failure, started, sent)
        except UpstreamError as exc:
            return self._failed(caller, c, name, upstream, version, exc, started, sent)
        except Exception:
            log.exception("tool call: unexpected error")
            failure = UpstreamError(INTERNAL, "internal error")
            return self._failed(caller, c, name, upstream, version, failure, started, sent)
        result = outcome.result
        try:
            mcp_types.CallToolResult.model_validate(result)
        except ValueError:
            failure = UpstreamError(UNAVAILABLE, "invalid response")
            v = [outcome.version]
            return self._failed(caller, c, name, upstream, v, failure, started, sent)
        size = len(_compact(result))
        if size > cap:  # BR-184: replaced, never truncated nor repeated; the tool ran
            self._audit_call(caller, c, name, upstream, outcome.version, TOO_LARGE, started,
                             sent, size, cap=cap)  # fmt: skip
            return error_result(TOO_LARGE)
        masked: dict[str, Any] = _mask(result, _bearer_value(held[0].authorization))
        issue = TOOL_ERROR if result.get("isError") is True else "ok"
        self._audit_call(caller, c, name, upstream, outcome.version, issue, started, sent, size)
        return masked

    def _rate_limited(self, surface: str, key: str, caller: Caller, delay: float) -> dict[str, Any]:
        """BR-303, BR-304: the tool result `rate_limited (retry in N s)`; audited through BR-312's
        coalesced entry only, with no `relay.tool_call` (BR-122 v3)."""
        suppressed = self.limits.coalescer.refused(surface, key)
        if suppressed is not None:
            self._write_audit(
                **limits_mod.refusal_entry(
                    surface,
                    key,
                    suppressed,
                    actor=caller.actor,
                    node_id=caller.role_id,
                    path=caller.role_path,
                )
            )
        return error_result(RATE_LIMITED, float(limits_mod.whole_seconds(delay)))

    def _failed(
        self,
        caller: Caller,
        c: Carried,
        name: str,
        upstream: str,
        version: list[str],
        exc: UpstreamError,
        started: float,
        sent: int,
    ) -> dict[str, Any]:
        v = version[0] if version else None
        self._audit_call(caller, c, name, upstream, v, exc.code, started, sent, 0, exc.reason)
        if exc.code == CANCELLED:
            # BR-120: audited "cancelled", never a known outcome; the client no longer reads the
            # stream, the code returned stays in the closed list of BR-116
            return error_result(UNCERTAIN)
        return error_result(exc.code, exc.retry_after)

    def _stage1(
        self, conn: sqlite3.Connection, role_id: str, name: str
    ) -> tuple[Carried, str, Policy | None, int] | None:
        """Stage 1 of BR-113, in the database only: public name → connection placed on this role,
        active, upstream declared; the role's policy state and its response cap (BR-184) are read
        in the same read (BR-112). A state or a cap that cannot be read gives a `None` policy: the
        call is refused `policy_error` (BR-185)."""
        row = conn.execute(
            "SELECT t.connection_id, t.upstream_name FROM tool_name t"
            " JOIN placement p ON p.connection_id = t.connection_id AND p.role_id = ?"
            " WHERE t.public_name = ?",
            (role_id, name),
        ).fetchone()
        if row is None:
            return None
        tree = Tree.load(conn)
        role = tree.nodes.get(role_id)
        if role is None or role.level != "role":
            return None
        c = next((x for x in carried(conn, tree, role_id) if x.id == row[0]), None)
        if c is None or not c.serves():
            return None
        cap = CAP_MAX
        try:
            policy: Policy | None = self._state(conn, tree, role_id)
            cap = parse_cap(tree.effective(role, "response_cap")[0])
        except Exception:
            log.exception("policy: unreadable state")
            policy = None
        return c, str(row[1]), policy, cap

    # -- token, 401 and "not before" signalling date (BR-114, BR-127, BR-128) -----------------

    def _access(self, c: Carried, caller: Caller) -> Access:
        try:
            return lifecycle.access(self.run, self.key, self.out, c.id, caller.role_id,
                                    clock=self.clock)  # fmt: skip
        except NotPlaced:
            raise UnknownTool(c.id) from None
        except (NotAuthorized, ReauthRequired):
            raise UpstreamError(REAUTHORIZE, "connection to reauthorize") from None
        except TemporarilyUnavailable as exc:
            raise UpstreamError(UNAVAILABLE, "renewal unavailable",
                                retry_after=exc.retry_after) from None  # fmt: skip
        except (
            AccessError
        ):  # access_key_unavailable, access_integrity_error (already audited by F-006)
            raise UpstreamError(UNAVAILABLE, "access unavailable") from None
        except StoreUnavailable:
            raise UpstreamError(UNAVAILABLE, "database unavailable") from None

    def _with_token[T](self, c: Carried, caller: Caller, act: Callable[[Access], T]) -> T:
        access = self._access(c, caller)
        try:
            done = act(access)
        except Unauthorized:
            pass
        else:
            self._succeeded(c.id)
            return done
        # 401 before any content (BR-127)
        if self._paused(c.id):  # BR-128: no signal, no retry
            raise UpstreamError(REAUTHORIZE, "401 from the MCP server")
        self._signal(c, access)
        if c.kind != "oauth":  # provided token: BR-085, no retry
            raise UpstreamError(REAUTHORIZE, "401 from the MCP server")
        access = self._access(c, caller)
        try:
            done = act(access)
        except Unauthorized:
            self._pause(c, caller)
            raise UpstreamError(REAUTHORIZE, "401 from the MCP server") from None
        self._succeeded(c.id)
        return done

    def _signal(self, c: Carried, access: Access) -> None:
        try:
            lifecycle.signal(self.run, self.key, c.id, access.generation, lifecycle.MCP_401,
                             clock=self.clock)  # fmt: skip
        except StoreUnavailable:
            log.error("signal_refusal not written (%s)", c.name)

    def _paused(self, connection_id: str) -> bool:
        with self._lock:
            pause = self._pauses.get(connection_id)
            return pause is not None and pause.not_before > self._wall()

    def _succeeded(self, connection_id: str) -> None:
        with self._lock:
            self._pauses.pop(connection_id, None)

    def _pause(self, c: Carried, caller: Caller) -> None:
        with self._lock:
            pause = self._pauses.setdefault(c.id, _Pause())
            delay = min(SIGNAL_PAUSE_MAX_S, SIGNAL_PAUSE_MIN_S * float(2 ** min(pause.step, 8)))
            pause.step += 1
            pause.not_before = self._wall() + delay
        self._write_audit(
            actor=caller.actor,
            action=A_SIGNAL_PAUSE,
            result="ok",
            node_id=caller.role_id,
            path=c.name,
            reason="second 401 on a fresh token",
            detail={"role": caller.role_path, "pause_s": int(delay)},
        )

    # -- requests from the upstream to the client (BR-121) -------------------------------------

    def _upstream_request(self, key: tuple[str, str], method: str, count: int) -> None:
        """One entry per exchange: method of the first request, number of requests (m4)."""
        self._write_audit(
            actor=lifecycle.GATEWAY,
            action=A_UPSTREAM_REQUEST,
            result="refused",
            node_id=key[0],
            detail={"connection_id": key[1], "method": safe_label(method), "count": count},
        )

    # -- audit (BR-122) ------------------------------------------------------------------------

    def _audit_call(
        self,
        caller: Caller,
        c: Carried | None,
        name: str,
        upstream: str | None,
        version: str | None,
        issue: str,
        started: float,
        sent: int,
        received: int,
        reason: str | None = None,
        *,
        cap: int | None = None,
    ) -> None:
        detail: dict[str, object] = {
            "role": caller.role_path,
            "public_name": safe_label(name),
            "issue": issue,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "request_bytes": sent,
            "response_bytes": received,
        }
        if c is not None:
            detail["connection"] = c.name
            detail["upstream_name"] = safe_label(upstream or "", 256)
        if version is not None:
            detail["version"] = version
        if cap is not None:
            detail["response_cap"] = cap  # the role's effective cap that replaced it (BR-184)
        if getattr(self._local, "stream", False):
            detail["stream"] = True  # streamed response, progress relayed (BR-120; AC-OP)
        if issue in ("ok", TOOL_ERROR):
            result = "ok"
        elif issue == REFUSED:
            result = "refused"
        else:
            result = "error"
        self._write_audit(
            actor=caller.actor,
            action=A_CALL,
            result=result,
            node_id=caller.role_id,
            path=c.name if c is not None else None,
            reason=reason,
            detail=detail,
        )

    def _audit_omitted(
        self, caller: Caller, c: Carried, cause: str, issue: str, elapsed: float
    ) -> None:
        self._write_audit(
            actor=caller.actor,
            action=A_OMITTED,
            result="omitted",
            node_id=caller.role_id,
            path=c.name,
            reason=cause,
            detail={
                "role": caller.role_path,
                "connection": c.name,
                "issue": issue,
                "duration_ms": int(elapsed * 1000),
            },
        )

    def _write_audit(self, **fields: Any) -> None:
        """An entry that cannot be written does not prevent the result (BR-122, H9)."""

        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                audit.write(conn, now=self.clock(), **fields)

        try:
            self.run(write)
        except (StoreUnavailable, sqlite3.Error) as exc:
            print(f"relay audit not written ({fields.get('action')}): {exc}", file=sys.stderr)
