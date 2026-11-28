"""History and undo (BR-219–221), shared by the CLI and admin API.

The audit is append-only. A reversal calls the ordinary operations under one write lock,
then appends a reversible change.undo envelope pointing to the original entry.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from hullgate import audit, connections, lifecycle, policy, relay, tree
from hullgate.cipher import ConnectionsKey
from hullgate.db import transaction
from hullgate.outbound import Outbound
from hullgate.upstream_mcp import RpcError, Target, ToolList, UpstreamClient, UpstreamError

UNDOABLE = frozenset(
    {
        "settings.set",
        "settings.unset",
        "settings.changed",
        "tree.sensitive",
        "connection.place",
        "connection.unplace",
        "connection.prefix",
        "policy.mode",
        "policy.list_add",
        "policy.list_remove",
        "policy.argument_set",
        "policy.argument_remove",
        "policy.pin",
    }
)


class Refusal(tree.Refused):
    def __init__(self, code: str, message: str, *, status: int = 409, **extra: Any):
        super().__init__(message)
        self.code, self.status, self.extra = code, status, extra


@dataclass
class Operations:
    conn: sqlite3.Connection
    key: ConnectionsKey
    outbound: Outbound
    upstream: UpstreamClient
    clock: tree.Clock = time.time

    listings: dict[tuple[str, str], ToolList] = field(default_factory=dict)
    expected: str = ""
    raced: bool = False
    stale_code: str = "undo_stale"

    def start(self, code: str) -> None:
        self.stale_code = code
        self.expected = state_digest(self.conn)
        self.raced = False
        self.listings.clear()

    def run[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        # Never discard a rotated credential because another writer raced preparation.
        # Commit the provider outcome first, remember the race, then refuse the composite act.
        # Lifecycle exempts outcome records before entering this outer transaction; lease
        # decisions and the later composite mutation still check the request's authorization.
        with transaction(self.conn):
            self.raced |= state_digest(self.conn) != self.expected
            result = fn(self.conn)
            self.expected = state_digest(self.conn)
            return result

    def recheck(self, **extra: Any) -> None:
        if self.raced or state_digest(self.conn) != self.expected:
            raise Refusal(
                self.stale_code, "State changed during preparation; preview again.", **extra
            )

    def prepare_argument(self, name: str, path: str, *, check_writable: bool = True) -> None:
        if (name, path) in self.listings:
            return
        t = tree.Tree.load(self.conn)
        row = connections._find(self.conn, t, name)
        service = connections._service(self.conn, row.service_id)
        if not service.mcp_url or not service.upstream_type:
            raise tree.Refused(f"{name}: {connections.UPSTREAM_UNDECLARED}")
        role = t.find(path)
        # Restoring an unplacement (or importing a new role) has no placement yet.
        # Access is obtained for the connection; argument_set rechecks the final placement.
        if (
            check_writable
            and role
            and connections.placement_document(self.conn, row.id, role.id) is not None
        ):
            policy.writable(self.conn, name, path)
        session_key = ("prepare-" + secrets.token_hex(16), row.id)
        try:
            access = lifecycle.access(
                self.run, self.key, self.outbound, row.id, None, clock=self.clock
            )
            listing = self.upstream.list_tools(
                session_key, Target(service.mcp_url, service.mcp_internal), access.authorization
            )
            self.listings[name, path] = listing
        except (UpstreamError, RpcError):
            raise tree.Refused("upstream unavailable: no rule recorded") from None
        except (lifecycle.Unavailable, connections.AccessError):
            raise tree.Refused("connection unavailable: no rule recorded") from None
        finally:
            self.upstream.close(session_key, wait=True)

    def argument(self, name: str, path: str, tool: str, rule: dict[str, Any]) -> None:
        relay.argument_at_entry(
            lifecycle.Direct(self.conn),
            self.key,
            self.outbound,
            self.upstream,
            name,
            path,
            tool,
            rule["argument"],
            rule["values"],
            clock=self.clock,
            prepared=self.listings[name, path],
        )

    def placement_policy(
        self, name: str, path: str, wanted: dict[str, Any], *, imported: bool = False
    ) -> None:
        c = self.conn
        t = tree.Tree.load(c)
        row = connections._find(c, t, name)
        current = connections.placement_document(c, row.id, t.resolve(path).id)
        if current is None:
            raise tree.Refused(f"not placed: {name} on {path}")
        policy.set_mode(c, name, path, wanted["mode"] or "inherit", clock=self.clock)
        policy.set_pin(c, name, path, wanted["pin"] or "inherit", clock=self.clock)
        for which in policy.LISTS:
            added = sorted(set(wanted[which]) - set(current[which]))
            removed = sorted(set(current[which]) - set(wanted[which]))
            if removed:
                policy.list_remove(c, name, path, which, removed, clock=self.clock)
            if added:
                policy.list_add(c, name, path, which, added, clock=self.clock)
        for tool in sorted(set(current["pinned_tools"]) - set(wanted["pinned_tools"])):
            policy.unpin_tool(c, name, path, tool, clock=self.clock)
        for tool in sorted(set(wanted["pinned_tools"]) - set(current["pinned_tools"])):
            policy.pin_tool(c, name, path, tool, clock=self.clock)
        old = {r["tool"]: r for r in current["arguments"]}
        new = {r["tool"]: r for r in wanted["arguments"]}
        for tool in sorted(old.keys() - new.keys()):
            policy.argument_remove(c, name, path, tool, clock=self.clock)
        for tool, rule in new.items():
            if old.get(tool) != rule:
                if imported and not row.has_tokens:
                    policy.import_argument(
                        c, name, path, tool, rule["argument"], rule["values"], clock=self.clock
                    )
                else:
                    self.argument(name, path, tool, rule)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


# Include credentials and policy baselines in the hash, never in the plan. A new token changes
# deletion/sensitivity impacts. Audit reads, session activity and sign-in do not stale a preview.
STATE_TABLES = (
    "node",
    "setting",
    "service",
    "application",
    "connection",
    "placement",
    "placement_tool",
    "argument_rule",
    "pinned_definition",
    "tool_catalog",
    "operator_token",
    "lineage",
    "access_jti",
    "pending_auth",
    "tool_name",
)


def state_digest(conn: sqlite3.Connection) -> str:
    state = {}
    for table in STATE_TABLES:
        rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()  # noqa: S608
        state[table] = [[v.hex() if isinstance(v, bytes) else v for v in row] for row in rows]
    return digest(state)


def decoded(entry: audit.Entry) -> dict[str, Any]:
    def value(text: str | None) -> Any:
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    return {
        **entry.as_dict(),
        **{k: value(getattr(entry, k)) for k in ("before", "after", "detail")},
    }


def action(e: dict[str, Any]) -> str:
    return str((e.get("detail") or {}).get("undo_action", e["action"]))


def eligibility(e: dict[str, Any]) -> dict[str, Any]:
    detail = e.get("detail") or {}
    act = action(e)
    if detail.get("import_id"):
        reason = "part_of_import"
    elif e["result"] != "ok":
        reason = "no_change"
    elif act not in UNDOABLE:
        reason = {
            "tree.delete": "deleted_node_never_returns",
            "tree.rename": "cut_lineages_do_not_return",
            "tree.move": "cut_lineages_do_not_return",
            "token.issue": "credential_cannot_be_restored",
            "token.revoke": "credential_cannot_be_restored",
        }.get(act, "not_reversible")
    elif (
        act in ("connection.place", "connection.unplace")
        and (e["before"] is None and e["after"] is None)
    ) or (act.startswith("policy.") and "tool" in detail and "upstream_name" not in detail):
        reason = "history_snapshot_unavailable"
    else:
        reason = None
    return {"undoable": reason is None, "undo_reason": reason}


def entry(conn: sqlite3.Connection, seq: int) -> dict[str, Any]:
    if type(seq) is not int or not 0 < seq <= 2**63 - 1:
        raise Refusal("invalid_request", "seq: a positive SQLite integer is required", status=422)
    entries, _ = audit.query(
        conn, audit.Filter(), before=seq + 1 if seq < 2**63 - 1 else None, limit=1
    )
    if not entries or entries[0].seq != seq:
        raise Refusal("not_found", "Unknown history entry.", status=404)
    return decoded(entries[0])


def current(conn: sqlite3.Connection, e: dict[str, Any]) -> Any:
    t = tree.Tree.load(conn)
    act, d = action(e), e.get("detail") or {}
    path = e["path"]
    node = t.nodes.get(e["node_id"]) if e["node_id"] else None
    if e["node_id"] and (node is None or t.path(node) != path):
        return {"missing_target": True}
    if act.startswith("settings."):
        if node is None:
            return {"missing_target": True}
        key = next(k for k in e["before"] if k not in ("effective", "origin"))
        value = t.settings.get((node.id, key))
        doc: dict[str, Any] = {key: value}
        if isinstance(e["after"], dict) and "effective" in e["after"]:
            effective, origin = t.effective(node, key)
            doc.update(effective=effective, origin=t.path(origin))
        return doc
    if act == "tree.sensitive":
        return {"sensitive": node.sensitive} if node else {"missing_target": True}
    try:
        row = connections._find(conn, t, path if act == "connection.prefix" else d["connection"])
    except tree.Refused:
        return {"missing_target": True}
    if d.get("connection_id", row.id) != row.id:
        return {"missing_target": True}
    if act == "connection.prefix":
        return {"prefix": row.prefix}
    if node is None:
        return {"missing_target": True}
    p = connections.placement_document(conn, row.id, node.id)
    if act in ("connection.place", "connection.unplace"):
        return p
    if p is None:
        return {"missing_target": True}
    if act == "policy.mode":
        return {"mode": p["mode"]}
    if act == "policy.pin":
        return (
            {"tool_pin": d["upstream_name"] in p["pinned_tools"]}
            if "tool" in d
            else {"pin": p["pin"] or "inherit"}
        )
    if act in ("policy.list_add", "policy.list_remove"):
        return {"listed": d["upstream_name"] in p[d["list"]]}
    rules = {r["tool"]: {k: v for k, v in r.items() if k != "tool"} for r in p["arguments"]}
    return rules.get(d["upstream_name"])


def plan(conn: sqlite3.Connection, seq: int, *, clock: tree.Clock = time.time) -> dict[str, Any]:
    e = entry(conn, seq)
    allowed = eligibility(e)
    result = {**e, **allowed}
    if not allowed["undoable"]:
        return result
    value = current(conn, e)
    result.update(current=value, expected=e["after"], stale=value != e["after"], impact=None)
    if action(e) == "tree.sensitive" and not result["stale"]:
        impact = asdict(
            tree.sensitive_impact(conn, e["path"], e["before"]["sensitive"], clock=clock)
        )
        result["impact"] = {
            "values": impact,
            "digest": digest(impact),
            "notice": "Restores the flag only; affected tokens are not restored.",
        }
    return result


def undo(
    ops: Operations, seq: int, *, impact: str | None = None, confirm_share: bool = False
) -> dict[str, Any]:
    conn = ops.conn
    try:
        with transaction(conn):
            ops.start("undo_stale")
            initial = plan(conn, seq, clock=ops.clock)
        if initial["undoable"] and not initial["stale"]:
            act, before = action(initial), initial["before"]
            if before and (
                act in ("policy.argument_set", "policy.argument_remove")
                or (act in ("connection.place", "connection.unplace") and before["arguments"])
            ):
                ops.prepare_argument(initial["detail"]["connection"], initial["path"])
        with transaction(conn):
            p = plan(conn, seq, clock=ops.clock)
            ops.recheck(current=p.get("current"), expected=p.get("expected"))
            if not p["undoable"]:
                raise Refusal(p["undo_reason"], "This change cannot be undone: " + p["undo_reason"])
            if p["stale"]:
                raise Refusal(
                    "undo_stale",
                    "The changed value has moved since this entry.",
                    current=p["current"],
                    expected=p["expected"],
                )
            if p["impact"] and impact != p["impact"]["digest"]:
                raise Refusal(
                    "impact_changed", "Read and confirm the current impact.", impact=p["impact"]
                )
            with audit.change_context(undo_of=seq):
                _restore(ops, p, confirm_share)
                after = current(conn, p)
                audit.write(
                    conn,
                    actor=audit.actor(),
                    action="change.undo",
                    result="ok",
                    node_id=p["node_id"],
                    path=p["path"],
                    before=p["current"],
                    after=after,
                    detail={
                        **(p["detail"] or {}),
                        "undo_action": action(p),
                        "undo_of": seq,
                        "confirm_share": confirm_share,
                    },
                    now=ops.clock(),
                )
            return {"seq": conn.execute("SELECT max(seq) FROM audit").fetchone()[0]}
    except tree.Refused as exc:
        with transaction(conn):
            audit.write(
                conn,
                actor=audit.actor(),
                action="change.undo",
                result="refused",
                reason=exc.code if isinstance(exc, Refusal) else "refused",
                detail={"undo_of": seq},
                now=ops.clock(),
            )
        raise


def _restore(ops: Operations, e: dict[str, Any], confirm_share: bool) -> None:
    c, clock = ops.conn, ops.clock
    act, d, before, path = action(e), e.get("detail") or {}, e["before"], e["path"]
    if act.startswith("settings."):
        key = next(k for k in before if k not in ("effective", "origin"))
        if before[key] is None:
            tree.settings_unset(c, path, key, clock=clock)
        else:
            tree.settings_set(c, path, key, before[key], clock=clock)
    elif act == "tree.sensitive":
        tree.set_sensitive(c, path, before["sensitive"], clock=clock)
    elif act == "connection.prefix":
        connections.set_prefix(c, path, before["prefix"], clock=clock)
    elif act in ("connection.place", "connection.unplace"):
        if before is None:
            connections.unplace(c, d["connection"], path, clock=clock)
        else:
            connections.place(c, d["connection"], path, confirm_share, clock=clock)
            ops.placement_policy(d["connection"], path, before)
    elif act == "policy.mode":
        policy.set_mode(c, d["connection"], path, before["mode"] or "inherit", clock=clock)
    elif act == "policy.pin":
        if "tool" in d:
            fn = policy.pin_tool if before["tool_pin"] else policy.unpin_tool
            fn(c, d["connection"], path, d["upstream_name"], clock=clock)
        else:
            policy.set_pin(c, d["connection"], path, before["pin"], clock=clock)
    elif act in ("policy.list_add", "policy.list_remove"):
        list_fn = policy.list_add if before["listed"] else policy.list_remove
        list_fn(c, d["connection"], path, d["list"], [d["upstream_name"]], clock=clock)
    elif before is None:
        policy.argument_remove(c, d["connection"], path, d["upstream_name"], clock=clock)
    else:
        ops.argument(d["connection"], path, d["upstream_name"], before)
