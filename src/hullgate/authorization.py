"""Steps 3 to 7 of the authorization order of a role's access point (BR-050, BR-051, BR-055).

Steps 1 (path of an existing role) and 2 (vault available) are played by the access point
before calling `authorize`. The first failing step answers; no later one takes place. Token
families plug in through resolvers: lot A has only operator tokens, lot B will add the OAuth
JWTs (exact audience, scope).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from hullgate import audit
from hullgate.tree import Node, Tree

SCOPE = "mcp"


@dataclass(frozen=True, slots=True)
class Credential:
    """Token presented, recognized and authentic (step 4); the later steps read its fields."""

    kind: str  # "operator" or "oauth"
    id: str
    actor: str  # operator token identifier or OAuth client_id, never a value
    node_id: str  # node where the token is placed (the role for an OAuth token)
    expires_at: float
    revoked: bool
    node_deleted: bool
    scopes: frozenset[str]
    audience: str | None  # OAuth tokens only: the URL of an access point
    # BR-303: what a token's tool calls count by — `operator:<id>`, or `lineage:<id>` for OAuth so
    # that a refresh does not reset the count (the `client_id` is shared across lineages)
    token_key: str = ""


Resolver = Callable[[sqlite3.Connection, str], Credential | None]


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    step: int
    status: int = 200
    error: str = ""
    reason: str = ""
    actor: str = audit.ANONYMOUS
    token_key: str = ""  # accepted token only (BR-303)


ACCEPTED = Outcome(True, 7)


def _refuse(step: int, reason: str, actor: str = audit.ANONYMOUS) -> Outcome:
    if step == 3:
        return Outcome(False, 3, 401, "", reason, actor)
    if step == 7:
        return Outcome(False, 7, 403, "insufficient_scope", reason, actor)
    return Outcome(False, step, 401, "invalid_token", reason, actor)


def covers(tree: Tree, token_node_id: str, role: Node) -> bool:
    """BR-050: a token covers its node and its descendants, compared by identifiers."""
    return any(n.id == token_node_id for n in tree.lineage_of(role))


def authorize(
    conn: sqlite3.Connection,
    tree: Tree,
    role: Node,
    endpoint_url: str,
    bearer: str | None,
    resolvers: Sequence[Resolver],
    now: float,
) -> Outcome:
    # (3) token present
    if bearer is None:
        return _refuse(3, "missing_token")
    # (4) well formed and authentic, not expired
    credential = next(
        (c for c in (resolve(conn, bearer) for resolve in resolvers) if c is not None), None
    )
    if credential is None or credential.expires_at <= now:
        actor = credential.actor if credential is not None else audit.ANONYMOUS
        return _refuse(4, "invalid_token", actor)
    actor = credential.actor
    # (5) neither attached to a deleted node, nor revoked
    if credential.node_deleted or credential.node_id not in tree.nodes:
        return _refuse(5, "node_deleted", actor)
    if credential.revoked:
        return _refuse(5, "revoked", actor)
    # (6) exact audience (OAuth tokens), prefix by segment (all), sensitive (BR-051)
    if credential.audience is not None and credential.audience != endpoint_url:
        return _refuse(6, "audience_mismatch", actor)
    if not covers(tree, credential.node_id, role):
        return _refuse(6, "out_of_scope", actor)
    if tree.is_sensitive(role) and credential.node_id != role.id:
        return _refuse(6, "sensitive_requires_role_token", actor)
    # (7) scope `mcp` (N2)
    if SCOPE not in credential.scopes:
        return _refuse(7, "insufficient_scope", actor)
    return Outcome(True, 7, actor=actor, token_key=credential.token_key or f"token:{credential.id}")
