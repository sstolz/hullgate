"""Bounded YAML import, read-only plans and atomic application (BR-222–225, BR-236)."""

from __future__ import annotations

import contextlib
import re
import secrets
import sqlite3
from collections.abc import Hashable
from dataclasses import asdict
from typing import Any

import yaml

from hullgate import audit, connections, export, lifecycle, policy, tree
from hullgate.changes import Operations, Refusal, digest, state_digest
from hullgate.db import StoreUnavailable, transaction
from hullgate.migrations import ORG_ID

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_ITEMS = 50_000


def invalid(location: str, message: str) -> Refusal:
    return Refusal("import_invalid", f"{location}: {message}", status=422, location=location)


class Loader(yaml.SafeLoader):
    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Hashable, Any]:
        result: dict[Hashable, Any] = {}
        for k, v in node.value:
            key = self.construct_object(k, deep=deep)  # type: ignore[no-untyped-call]
            if not isinstance(key, str) or key in result:
                raise invalid(f"line {k.start_mark.line + 1}", "duplicate or non-text key")
            string(key, f"line {k.start_mark.line + 1}")
            result[key] = self.construct_object(v, deep=deep)  # type: ignore[no-untyped-call]
        return result


def parse(text: str) -> dict[str, Any]:
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise invalid("document", "text cannot be encoded as UTF-8") from None
    if len(encoded) > MAX_BYTES:
        raise Refusal(
            "import_too_large", "document: maximum size is 1 MiB", status=413, location="document"
        )
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(text, Loader=Loader), 1):
            if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None):
                raise invalid(
                    f"line {event.start_mark.line + 1}", "aliases and anchors are forbidden"
                )
            if getattr(event, "tag", None):
                raise invalid(f"line {event.start_mark.line + 1}", "tags are forbidden")
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
            if isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
            if depth > MAX_DEPTH or count > MAX_ITEMS:
                raise invalid(
                    f"line {event.start_mark.line + 1}", "document complexity limit exceeded"
                )
        doc = yaml.load(text, Loader=Loader)  # noqa: S506 — subclass of SafeLoader
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise invalid(
            f"line {mark.line + 1}, column {mark.column + 1}" if mark else "document",
            "invalid safe YAML",
        ) from None
    except (ValueError, RecursionError):
        raise invalid("document", "invalid or excessively complex YAML") from None
    _validate(doc)
    return dict(doc)


def mapping(value: Any, keys: set[str], at: str, optional: set[str] | None = None) -> None:
    if not isinstance(value, dict):
        raise invalid(at, "a mapping is required")
    unknown = value.keys() - keys
    if unknown:
        raise invalid(at + "." + sorted(unknown)[0], "unknown key")
    missing = keys - value.keys() - (optional or set())
    if missing:
        raise invalid(at + "." + sorted(missing)[0], "missing key")


def string(value: Any, at: str) -> None:
    if not isinstance(value, str):
        raise invalid(at, "text is required")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise invalid(at, "text cannot be encoded as UTF-8") from None


def sequence(value: Any, at: str) -> None:
    if not isinstance(value, list):
        raise invalid(at, "a sequence is required")


def unique(values: list[Any], key: str, at: str) -> None:
    found: set[str] = set()
    for i, item in enumerate(values):
        if not isinstance(item, dict) or key not in item:
            raise invalid(f"{at}[{i}]", f"mapping with {key} is required")
        string(item[key], f"{at}[{i}].{key}")
        if item[key] in found:
            raise invalid(f"{at}[{i}].{key}", "duplicate identity")
        found.add(item[key])


def strings(value: Any, at: str) -> None:
    sequence(value, at)
    for i, item in enumerate(value):
        string(item, f"{at}[{i}]")
    if len(value) != len(set(value)):
        raise invalid(at, "duplicate item")


def _validate(doc: Any) -> None:
    mapping(doc, {"format", "organisation", "services", "applications", "connections"}, "document")
    if type(doc["format"]) is not int or doc["format"] != export.FORMAT:
        raise invalid("format", f"expected export format {export.FORMAT}")

    def node(n: Any, level: str, at: str) -> None:
        child = export.CHILD_KEYS.get(level)
        keys = {"level", "name", "settings", "id" if level == "org" else "segment"}
        if child:
            keys.add(child)
        if level == "env":
            keys.add("sensitive")
        mapping(n, keys, at)
        if n["level"] != export.LEVEL_KEYS[level]:
            raise invalid(at + ".level", "invalid level")
        string(n["name"], at + ".name")
        identity = "id" if level == "org" else "segment"
        string(n[identity], at + "." + identity)
        if level == "org" and not re.fullmatch(r"[a-z2-7]{12}", n["id"]):
            raise invalid(at + ".id", "expected 12 characters [a-z2-7]")
        if level == "env" and type(n["sensitive"]) is not bool:
            raise invalid(at + ".sensitive", "a boolean is required")
        if not isinstance(n["settings"], dict):
            raise invalid(at + ".settings", "a mapping is required")
        for k, v in n["settings"].items():
            string(v, at + ".settings." + k)
            if k not in tree.SETTINGS:
                raise invalid(at + ".settings." + k, "unknown setting")
        if child:
            sequence(n[child], at + "." + child)
            unique(n[child], "segment", at + "." + child)
            for i, item in enumerate(n[child]):
                node(item, tree.LEVELS[tree.LEVELS.index(level) + 1], f"{at}.{child}[{i}]")

    node(doc["organisation"], "org", "organisation")
    for group, identity in (("services", "id"), ("applications", "id"), ("connections", "name")):
        sequence(doc[group], group)
        unique(doc[group], identity, group)
        for i, item in enumerate(doc[group]):
            at = f"{group}[{i}]"
            if group == "services":
                mapping(item, {"id", "model", "url", "scopes", "internal_host", "tools"}, at)
                for k in ("id", "model", "url"):
                    string(item[k], at + "." + k)
                strings(item["scopes"], at + ".scopes")
                if type(item["internal_host"]) is not bool:
                    raise invalid(at + ".internal_host", "a boolean is required")
                sequence(item["tools"], at + ".tools")
                unique(item["tools"], "name", at + ".tools")
                for j, tool in enumerate(item["tools"]):
                    loc = f"{at}.tools[{j}]"
                    mapping(tool, {"name", "class"}, loc, {"class"})
                    if "class" in tool and tool["class"] not in policy.CLASSES:
                        raise invalid(loc + ".class", "unknown class")
            elif group == "applications":
                mapping(item, {"id", "service", "mode", "client_id"}, at)
                for k, v in item.items():
                    string(v, at + "." + k)
                if item["mode"] not in ("declared", "dynamic"):
                    raise invalid(at + ".mode", "unknown application mode")
            else:
                mapping(
                    item, {"name", "service", "prefix", "application", "state", "placements"}, at
                )
                for k in ("name", "service", "prefix", "state"):
                    string(item[k], at + "." + k)
                if item["application"] is not None:
                    string(item["application"], at + ".application")
                if item["state"] not in connections.STATE_LABELS:
                    raise invalid(at + ".state", "unknown connection state")
                sequence(item["placements"], at + ".placements")
                unique(item["placements"], "path", at + ".placements")
                for j, p in enumerate(item["placements"]):
                    loc = f"{at}.placements[{j}]"
                    mapping(
                        p,
                        {
                            "path",
                            "mode",
                            "pin",
                            "open",
                            "deny",
                            "allowlist",
                            "pinned_tools",
                            "arguments",
                        },
                        loc,
                    )
                    if p["mode"] not in (*policy.CLASSES, "inherit"):
                        raise invalid(loc + ".mode", "unknown mode")
                    if p["pin"] not in policy.PIN_VALUES:
                        raise invalid(loc + ".pin", "unknown pin")
                    for k in (*policy.LISTS, "pinned_tools"):
                        strings(p[k], loc + "." + k)
                    sequence(p["arguments"], loc + ".arguments")
                    unique(p["arguments"], "tool", loc + ".arguments")
                    for z, arg in enumerate(p["arguments"]):
                        argloc = f"{loc}.arguments[{z}]"
                        mapping(arg, {"tool", "argument", "values"}, argloc)
                        string(arg["argument"], argloc + ".argument")
                        strings(arg["values"], argloc + ".values")


def flatten(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {
        k: {}
        for k in ("node", "setting", "service", "application", "connection", "placement", "tool")
    }

    def node(n: dict[str, Any], path: str, level: str) -> None:
        result["node"][path] = {k: n[k] for k in ("name", "level", "sensitive") if k in n}
        for key, value in n["settings"].items():
            result["setting"][path + ":" + key] = {"path": path, "key": key, "value": value}
        for child in n.get(export.CHILD_KEYS.get(level, ""), []):
            node(
                child,
                (path.rstrip("/") + "/" + child["segment"]).lstrip("/"),
                tree.LEVELS[tree.LEVELS.index(level) + 1],
            )

    node(doc["organisation"], "/", "org")
    for s in doc["services"]:
        result["service"][s["id"]] = {k: v for k, v in s.items() if k != "tools"}
        scopes = set(s["scopes"])
        if s["model"] == "gitea":
            scopes.add("openid")
        result["service"][s["id"]]["scopes"] = sorted(scopes)
        for tool in s["tools"]:
            result["tool"][s["id"] + ":" + tool["name"]] = {"service": s["id"], **tool}
    for a in doc["applications"]:
        result["application"][a["id"]] = a
    for c in doc["connections"]:
        result["connection"][c["name"]] = {
            k: v for k, v in c.items() if k not in ("placements", "state")
        }
        for p in c["placements"]:
            result["placement"][c["name"] + ":" + p["path"]] = {"connection": c["name"], **p}
    return result


def preview(conn: sqlite3.Connection, text: str, *, clock: tree.Clock) -> dict[str, Any]:
    doc = parse(text)
    # BEGIN is a read snapshot: no row, temporary table, sequence or audit write.
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        return _preview(conn, text, doc, clock)
    finally:
        if own:
            conn.execute("ROLLBACK")


def _preview(
    conn: sqlite3.Connection, text: str, doc: dict[str, Any], clock: tree.Clock
) -> dict[str, Any]:
    t = tree.Tree.load(conn)
    oid = doc["organisation"]["id"]
    if len(t.nodes) > 1 and oid != t.public_id:
        raise Refusal(
            "import_foreign_organisation",
            "organisation.id: belongs to another instance",
            status=422,
            location="organisation.id",
        )
    old = flatten(export.export_document(conn))
    new = flatten(doc)
    changes: list[dict[str, Any]] = []
    if oid != t.public_id:
        changes.append(
            {
                "kind": "organisation_id",
                "action": "change",
                "path": "organisation.id",
                "before": t.public_id,
                "after": oid,
                "impact": {},
            }
        )
    for kind in old:
        for path in sorted(old[kind].keys() | new[kind].keys()):
            before: Any = old[kind].get(path)
            after: Any = new[kind].get(path)
            if kind == "setting" and (after or before)["path"] not in new["node"]:
                continue  # a retained omitted node keeps its overrides, including its locks
            if kind == "tool" and (after or before)["service"] not in new["service"]:
                continue
            if before == after:
                continue
            act = "create" if before is None else "delete" if after is None else "change"
            if kind == "setting" and after is None:
                act = "change"  # restoration of inheritance, BR-224
            # Catalogue names are observations, not deletable through policy operations. A
            # missing manual class clears the override; a name remains available for discovery.
            if kind == "tool" and after is None:
                if "class" not in before:
                    continue
                act, after = "change", {k: v for k, v in before.items() if k != "class"}
            impact: dict[str, Any] = {}
            if kind == "node" and act == "delete":
                impact = asdict(
                    tree._delete_plan(conn, t, t.resolve(path), clock(), check_connections=False)
                )
                n = t.resolve(path)
                impact["lineages_revoked"] = tree.active_lineages(
                    conn, [r.id for r in t.roles_below(n)], clock()
                )
            if (
                kind == "node"
                and before
                and after
                and before.get("sensitive") != after.get("sensitive")
            ):
                impact = asdict(tree.sensitive_impact(conn, path, after["sensitive"], clock=clock))
            if kind == "connection" and act == "delete":
                impact = {
                    "placements_removed": sum(
                        p["connection"] == path for p in old["placement"].values()
                    )
                }
            if kind == "placement" and act == "delete":
                impact = {"placements_removed": 1}
            if kind in ("application", "service") and act == "delete":
                impact = {
                    "connections": sum(
                        c["application" if kind == "application" else "service"] == path
                        for c in old["connection"].values()
                    )
                }
            if kind == "placement" and act == "create":
                role = after["path"]
                origin = after["connection"].split("/")[0]

                def sensitivity(p: str) -> bool:
                    env = p.rsplit("/", 1)[0]
                    return bool(
                        new["node"].get(env, old["node"].get(env, {})).get("sensitive", False)
                    )

                reasons = ["other_client"] if role.split("/")[0] != origin else []
                others = {**old["placement"], **new["placement"]}.values()
                if any(
                    p["connection"] == after["connection"]
                    and sensitivity(p["path"]) != sensitivity(role)
                    for p in others
                ):
                    reasons.append("sensitivity")
                impact = {"shares": reasons}
            changes.append(
                {
                    "kind": kind,
                    "action": act,
                    "path": path,
                    "before": before,
                    "after": after,
                    "impact": impact,
                }
            )
    return {
        "digest": digest({"document": text, "state": state_digest(conn), "changes": changes}),
        "changes": changes,
        "reauthorize": sum(c["kind"] == "connection" and c["action"] == "create" for c in changes),
        "apply_deletions": False,
    }


def adopt(conn: sqlite3.Connection, value: str, *, clock: tree.Clock) -> None:
    """BR-225's explicit empty-tree exception; keep the general SQL update guard intact."""
    t = tree.Tree.load(conn)
    if len(t.nodes) != 1:
        raise Refusal(
            "import_foreign_organisation", "organisation.id: tree is not empty", status=422
        )
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'node_public_id_permanent'"
    ).fetchone()[0]
    conn.execute("DROP TRIGGER node_public_id_permanent")
    conn.execute("UPDATE node SET public_id = ? WHERE id = ?", (value, ORG_ID))
    conn.execute(ddl)
    audit.write(
        conn,
        actor=audit.actor(),
        action="organisation.import_id",
        result="ok",
        path="/",
        before={"id": t.public_id},
        after={"id": value},
        now=clock(),
    )


def apply(
    ops: Operations,
    text: str,
    quoted: str,
    *,
    apply_deletions: bool = False,
    confirm_share: bool = False,
) -> dict[str, Any]:
    conn, clock = ops.conn, ops.clock
    deferred: list[lifecycle._Erased] = []
    import_id = secrets.token_hex(16)
    location = "document"
    services: dict[str, connections.PreparedService] = {}
    leases: dict[str, lifecycle._Lease] = {}
    try:
        doc = parse(text)  # bounded input work before acquiring any database lock
        with transaction(conn):
            plan = _preview(conn, text, doc, clock)
            if quoted != plan["digest"]:
                raise Refusal("import_stale", "document or state changed; preview again")
            changes = [c for c in plan["changes"] if c["action"] != "delete" or apply_deletions]
            ops.start("import_stale")
        for c in sorted(changes, key=_order):
            location = c["path"]
            after = c["after"]
            if c["kind"] == "service" and after:
                services[c["path"]] = connections.prepare_service(
                    ops.outbound,
                    c["path"],
                    after["url"],
                    after["model"],
                    ",".join(after["scopes"]),
                    after["internal_host"],
                )
            elif c["kind"] == "placement" and after and after["arguments"]:
                old = {r["tool"]: r for r in (c["before"] or {}).get("arguments", [])}
                changed = any(old.get(r["tool"]) != r for r in after["arguments"])
                rows = connections._live_rows(conn)
                t = tree.Tree.load(conn)
                row = next(
                    (
                        r
                        for r in rows
                        if connections.display_name(t.nodes, r) == after["connection"]
                    ),
                    None,
                )
                if changed and row and row.has_tokens:
                    # Earlier imported settings may unlock this role. The ordinary writable
                    # check runs after those settings, inside the configuration transaction.
                    ops.prepare_argument(after["connection"], after["path"], check_writable=False)
            elif c["kind"] == "connection" and c["action"] == "delete":
                leases[c["path"]] = lifecycle.prepare_removal(
                    lifecycle.Direct(conn), c["path"], clock=clock
                )
        with transaction(conn):
            ops.recheck()
            with audit.change_context(import_id=import_id):
                for c in sorted(changes, key=_order):
                    location = c["path"]
                    _execute(ops, c, confirm_share, deferred, services, leases)
                audit.write(
                    conn,
                    actor=audit.actor(),
                    action="import.apply",
                    result="ok",
                    detail={"changes": len(changes), "apply_deletions": apply_deletions},
                    now=clock(),
                )
        if deferred:
            # BR-075: commit the erasure before truncating pages containing ciphertext.
            with contextlib.suppress(sqlite3.Error, StoreUnavailable):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # No remote revocation before COMMIT: a late refusal cannot erase a provider's token.
        for erased in deferred:
            with audit.change_context(import_id=import_id):
                lifecycle._finish_upstream(lifecycle.Direct(conn), ops.outbound, erased, clock)
        return {"import_id": import_id, "changes": len(changes)}
    except tree.Refused as exc:
        with transaction(conn):
            audit.write(
                conn,
                actor=audit.actor(),
                action="import.apply",
                result="refused",
                reason=exc.code if isinstance(exc, Refusal) else "refused",
                detail={"location": location},
                now=clock(),
            )
        if isinstance(exc, Refusal):
            exc.extra.setdefault("location", location)
            raise
        if isinstance(exc, connections.ShareRefused):
            raise Refusal(
                "share_confirmation_required",
                f"{location}: {exc}",
                location=location,
                reasons=list(exc.reasons),
            ) from None
        raise Refusal("refused", f"{location}: {exc}", location=location) from None


def _order(c: dict[str, Any]) -> tuple[int, int, str]:
    kind, act = c["kind"], c["action"]
    if (
        kind == "setting"
        and c["before"]
        and c["before"]["key"] == "lock"
        and (c["after"] is None or c["after"]["value"] == "false")
    ):
        return (0, c["path"].count("/"), c["path"])
    if act == "delete":
        return (
            {"placement": 20, "connection": 21, "node": 22, "application": 23, "service": 24}[kind],
            -c["path"].count("/"),
            c["path"],
        )
    if kind == "setting" and (c["after"] or c["before"])["key"] == "lock":
        return (30, -c["path"].count("/"), c["path"])
    return (
        {
            "organisation_id": 1,
            "node": 2,
            "setting": 3,
            "service": 4,
            "application": 5,
            "tool": 6,
            "connection": 7,
            "placement": 8,
        }[kind],
        c["path"].count("/"),
        c["path"],
    )


def _execute(
    ops: Operations,
    c: dict[str, Any],
    confirm_share: bool,
    deferred: list[lifecycle._Erased],
    services: dict[str, connections.PreparedService],
    leases: dict[str, lifecycle._Lease],
) -> None:
    conn, clock = ops.conn, ops.clock
    kind, act, path, before, after = (c[k] for k in ("kind", "action", "path", "before", "after"))
    if kind == "organisation_id":
        adopt(conn, after, clock=clock)
    elif kind == "node":
        if act == "delete":
            tree.delete(conn, path, clock=clock)
        else:
            t = tree.Tree.load(conn)
            if act == "create" and t.find(path) is None:
                parent, _, segment = path.rpartition("/")
                tree.add(conn, parent or "/", after["level"], after["name"], segment, clock=clock)
                if after["level"] == "environment":
                    # The document is the complete desired role list. The default role is
                    # recreated by its own planned item when explicitly requested.
                    tree.delete(conn, path + "/principal", clock=clock)
            else:
                tree.rename(conn, path, after["name"], clock=clock)
            if "sensitive" in after:
                tree.set_sensitive(conn, path, after["sensitive"], clock=clock)
    elif kind == "setting":
        p = after or before
        if after is None:
            tree.settings_unset(conn, p["path"], p["key"], clock=clock)
        else:
            tree.settings_set(conn, p["path"], p["key"], p["value"], clock=clock)
    elif kind == "service":
        if act == "delete":
            connections.service_remove(conn, path, clock=clock)
        else:
            connections.write_service(conn, services[path], update=act == "change", clock=clock)
    elif kind == "application":
        if act in ("change", "delete"):
            connections.application_remove(conn, path, clock=clock)
        if after:
            connections.application_declare(
                conn,
                ops.key,
                path,
                after["service"],
                after["client_id"],
                None,
                clock=clock,
                mode=after["mode"],
            )
            # A restored dynamic registration is a declaration of its public identity only.
            # No provider registration or secret is recovered.
    elif kind == "tool":
        service, name = after["service"], after["name"]
        connections._service(conn, service)
        conn.execute(
            "INSERT OR IGNORE INTO tool_catalog (service_id, upstream_name, first_seen)"
            " VALUES (?, ?, ?)",
            (service, name, int(clock())),
        )
        policy.classify(conn, service, name, after.get("class", "clear"), clock=clock)
    elif kind == "connection":
        if act == "delete":
            lifecycle.remove(
                lifecycle.Direct(conn),
                ops.key,
                ops.outbound,
                path,
                path,
                clock=clock,
                deferred=deferred,
                prepared_lease=leases[path],
            )
        elif act == "create":
            connections.import_connection(
                conn, path, after["service"], after["prefix"], after["application"], clock=clock
            )
        else:
            if any(before[k] != after[k] for k in ("service", "application")):
                raise tree.Refused(
                    "connection service and application are fixed; remove and declare it explicitly"
                )
            connections.set_prefix(conn, path, after["prefix"], clock=clock)
    elif kind == "placement":
        p = after or before
        name, role = p["connection"], p["path"]
        if act == "delete":
            connections.unplace(conn, name, role, clock=clock)
        else:
            if act == "create":
                connections.place(conn, name, role, confirm_share, clock=clock)
            wanted = {
                **p,
                "mode": None if p["mode"] == "inherit" else p["mode"],
                "pin": None if p["pin"] == "inherit" else p["pin"],
            }
            ops.placement_policy(name, role, wanted, imported=True)
