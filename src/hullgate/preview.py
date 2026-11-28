"""Rights preview of a role (F-015 SCR-03, BR-183): what the role sees, and why.

`policy preview <role>` lists every tool of every connection placed on the role, from a fresh
upstream listing (`connection tools`, through `get_access`, audited the same way), and asks the
evaluator of `hullgate.policy` for each one: the verdict is the one `tools/list` and `tools/call`
take. No tool is called (BR-113). A connection that is not active, or whose upstream does not
answer, gets one `unavailable` line with its code and the others are listed; an exception of the
evaluator names the connection and makes the command fail (BR-185).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from hullgate import authorization, lifecycle, policy, relay
from hullgate.access_tokens import resource_for
from hullgate.cipher import ConnectionsKey
from hullgate.connections import AccessError
from hullgate.outbound import Outbound
from hullgate.tree import Clock, Refused, Tree
from hullgate.upstream_mcp import UpstreamClient, UpstreamError


@dataclass(frozen=True, slots=True)
class Tool:
    public_name: str
    connection: str
    upstream_name: str
    cls: str
    source: str
    exposed: bool
    reasons: tuple[str, ...]
    opened: bool
    grouped: dict[str, list[str]]
    argument_rule: dict[str, Any] | None
    pinned: bool
    sensitive_via: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "public_name": self.public_name,
            "connection": self.connection,
            "upstream_name": self.upstream_name,
            "class": self.cls,
            "source": self.source,
            "exposed": self.exposed,
            "reasons": list(self.reasons),
            "opened": self.opened,
            "grouped": self.grouped,
            "argument_rule": self.argument_rule,
            "pinned": self.pinned,
            "sensitive_via": list(self.sensitive_via),
        }


@dataclass(frozen=True, slots=True)
class Unavailable:
    connection: str
    code: str


@dataclass(frozen=True, slots=True)
class Preview:
    role: str
    tools: tuple[Tool, ...]  # exposed rows first, then hidden rows
    unavailable: tuple[Unavailable, ...]
    errors: tuple[str, ...] = field(default=())  # connections whose evaluation raised


def preview(
    run: lifecycle.Runner,
    key: ConnectionsKey,
    out: Outbound,
    client: UpstreamClient,
    role_path: str,
    *,
    clock: Clock = time.time,
) -> Preview:
    def read(conn: Any) -> tuple[str, str, list[relay.Carried]]:
        tree = Tree.load(conn)
        role = tree.resolve(role_path)
        if role.level != "role":
            raise Refused(f"policy preview: a role is expected ({tree.path(role)})")
        return role.id, tree.path(role), relay.carried(conn, tree, role.id)

    role_id, path, conns = run(read)
    rows: list[Tool] = []
    unavailable: list[Unavailable] = []
    errors: list[str] = []
    fetched: list[tuple[relay.Carried, relay.Listed]] = []
    for c in conns:
        if c.state != "active":
            unavailable.append(Unavailable(c.name, c.state))
            continue
        if c.url is None:
            unavailable.append(Unavailable(c.name, "upstream_undeclared"))
            continue
        try:
            listed = relay.tools(run, key, out, client, c.name, path, False, None, clock=clock)
        except UpstreamError as exc:
            unavailable.append(Unavailable(c.name, exc.code))
            continue
        except lifecycle.Unavailable:
            unavailable.append(Unavailable(c.name, "provider_unavailable"))
            continue
        except AccessError as exc:
            unavailable.append(Unavailable(c.name, exc.code))
            continue
        except policy.StateError:  # BR-185: the pinning state is unreadable, named
            errors.append(c.name)
            continue
        fetched.append((c, listed))
    # One state per role, read after the listings (a suspension they wrote applies, BR-181) and
    # inside the guarded block: a state that cannot be loaded names every connection listed and
    # fails the command, as the relay omits each in `policy_error` (BR-185; lot A review, N2)
    state: policy.RoleState | None = None
    if fetched:
        try:
            state = run(lambda conn: policy.ENGINE.load(conn, role_id))
        except Exception:
            errors.extend(c.name for c, _ in fetched)
    for c, listed in fetched:
        if state is None:
            break
        try:
            rows.extend(_rows(state, c, listed))
        except Exception:  # BR-185: named, and the command fails
            errors.append(c.name)
    rows.sort(key=lambda r: (not r.exposed, r.public_name))
    return Preview(path, tuple(rows), tuple(unavailable), tuple(errors))


def _rows(state: policy.RoleState, c: relay.Carried, listed: relay.Listed) -> list[Tool]:
    p = state.placements[c.id]
    out: list[Tool] = []
    for upstream, public in listed.names.items():
        definition = listed.definitions[upstream]
        cls, source = state.classify(c.id, upstream, definition)
        verdict = state.verdict(c.id, upstream, definition)
        rule = state.rule(c.id, upstream)
        out.append(
            Tool(
                public_name=public,
                connection=c.name,
                upstream_name=relay.safe_label(upstream, 256),
                cls=cls,
                source=source,
                exposed=verdict.exposed,
                reasons=verdict.reasons,
                opened=verdict.opened,
                grouped=policy.grouped(definition),
                argument_rule=None
                if rule is None
                else {"argument": rule.argument, "values": list(rule.values)},
                pinned=p.pinned(upstream),  # BR-180
                sensitive_via=p.sensitive_via,
            )
        )
    return out


# ---------------------------------------------------------------------------------------------
# Token variant (F-016 BR-217): does a token reach the role, and why
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reach:
    token: str
    kind: str  # "operator" or "oauth"
    role: str
    reaches: bool
    step: int  # the authorization step that answered (BR-055), 7 when it reaches
    reason: str  # "" when it reaches, else the refusal reason of that step

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "type": self.kind,
            "role": self.role,
            "reaches": self.reaches,
            "step": self.step,
            "reason": self.reason or None,
        }


def reach(
    conn: sqlite3.Connection, token_id: str, role_path: str, public_url: str, now: float
) -> Reach:
    """Steps 4 (expiry) to 7 of the authorization order (BR-055, BR-051) for the token picked by
    its identifier, on the role, as the access point plays them; the token's value is never read
    nor used, and nothing is written (no last use, no audit)."""
    tree = Tree.load(conn)
    role = tree.resolve(role_path)
    if role.level != "role":
        raise Refused(f"policy preview: a role is expected ({tree.path(role)})")
    path = tree.path(role)
    credential = _credential(conn, token_id)
    if credential is None:
        raise Refused(f"unknown token: {token_id}")
    outcome = authorization.authorize(
        conn,
        tree,
        role,
        resource_for(public_url, tree.public_id, path),
        "(not used)",
        [lambda _conn, _bearer: credential],
        now,
    )
    return Reach(token_id, credential.kind, path, outcome.ok, outcome.step, outcome.reason)


def _credential(conn: sqlite3.Connection, token_id: str) -> authorization.Credential | None:
    row = conn.execute(
        "SELECT t.node_id, t.expires_at, t.revoked, n.deleted FROM operator_token t"
        " JOIN node n ON n.id = t.node_id WHERE t.id = ?",
        (token_id,),
    ).fetchone()
    if row is not None:
        node_id, expires_at, revoked, deleted = row
        return authorization.Credential(
            kind="operator", id=token_id, actor=token_id, node_id=node_id,
            expires_at=float(expires_at), revoked=bool(revoked), node_deleted=bool(deleted),
            scopes=frozenset({authorization.SCOPE}), audience=None,
        )  # fmt: skip
    row = conn.execute(
        "SELECT l.role_id, l.client_id, l.resource, l.scope, l.expires_at, l.revoked, n.deleted"
        " FROM lineage l JOIN node n ON n.id = l.role_id WHERE l.id = ?",
        (token_id,),
    ).fetchone()
    if row is None:
        return None
    role_id, client_id, resource, scope, expires_at, revoked, deleted = row
    # an OAuth lineage: its access tokens carry the lineage's resource as audience, its scope
    return authorization.Credential(
        kind="oauth", id=token_id, actor=client_id, node_id=role_id,
        expires_at=float(expires_at), revoked=bool(revoked), node_deleted=bool(deleted),
        scopes=frozenset(str(scope).split()), audience=resource,
    )  # fmt: skip
