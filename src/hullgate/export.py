"""YAML export of the tree (BR-070 v2): deterministic, with no secret and no internal identifier.

Content: format version (`format`), levels, names, segments, sensitivity, organisation values and
setting overrides (`lock` included); since F-006 (format 2), the services, the outbound OAuth
applications (`client_id`, without secret) and the connections (name, service, prefix,
application, state, placements), sorted by name; since F-010 (format 3), keys and levels in
English (BR-131); since F-015 (format 4, BR-070 v4), each placement is a mapping (`path`, `mode`,
`pin`, `open`, `deny`, `allowlist`, `pinned_tools`, `arguments`, lists sorted) and each service
carries `tools`, its catalogue names with their manual class when there is one, so that an import
on a fresh instance writes the names before the lists that cite them (BR-176) — what the import
with preview (#11) will read back; a reimport requires reauthenticating the connections and pins
the definitions again; since F-014 (format 5, BR-070 v5), the organisation's public identifier
(`id`, first key under `organisation:`, BR-160), so that a tree restored by import keeps its URLs.
No token, no fingerprint, no secret, no inbound OAuth client (BR-061), no
audit entry, no pinning baseline, no queue state.
Strings are emitted between double quotes with JSON escapes, which are valid YAML; children are
sorted by segment, keys in a fixed order.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hullgate import connections, policy
from hullgate.tree import Node, Tree

FORMAT = 5  # format version, read by the import of #11 (S23); 5 since F-014 (BR-070)
LISTS = ("open", "deny", "allowlist")
CHILD_KEYS = {"org": "clients", "client": "projects", "project": "environments", "env": "roles"}
LEVEL_KEYS = {
    "org": "organisation",
    "client": "client",
    "project": "project",
    "env": "environment",
    "role": "role",
}


def _q(value: str) -> str:
    # YAML rejects these raw BMP noncharacters, though the tree accepts them. Keep astral
    # characters literal: JSON's surrogate-pair escapes are separate surrogates in PyYAML.
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("\ufffe", "\\ufffe")
        .replace("\uffff", "\\uffff")
    )


def _settings(tree: Tree, node: Node, indent: str) -> list[str]:
    overrides = sorted((k, v) for (nid, k), v in tree.settings.items() if nid == node.id)
    if not overrides:
        return [f"{indent}settings: {{}}"]
    return [f"{indent}settings:", *(f"{indent}  {k}: {_q(v)}" for k, v in overrides)]


def _node(tree: Tree, node: Node, indent: str, first: str) -> list[str]:
    """A node and its children; `first` prefixes the first line (`- ` in a list)."""
    rest = indent + " " * len(first)
    lines = [f"{indent}{first}level: {LEVEL_KEYS[node.level]}"]
    if node.level != "org":
        lines.append(f"{rest}segment: {_q(node.slug)}")
    lines.append(f"{rest}name: {_q(node.name)}")
    if node.level == "env":
        lines.append(f"{rest}sensitive: {'true' if node.sensitive else 'false'}")
    lines.extend(_settings(tree, node, rest))
    child_key = CHILD_KEYS.get(node.level)
    if child_key is not None:
        kids = tree.kids(node)
        if not kids:
            lines.append(f"{rest}{child_key}: []")
        else:
            lines.append(f"{rest}{child_key}:")
            for kid in kids:
                lines.extend(_node(tree, kid, rest + "  ", "- "))
    return lines


def _list(values: list[str]) -> str:
    return "[" + ", ".join(_q(v) for v in values) + "]"


def _catalogue(conn: sqlite3.Connection, service_id: str) -> list[str]:
    """`tools` of a service: the catalogue names, sorted, with the manual class when there is
    one (BR-172, BR-173); neither the annotation class nor the queue state."""
    rows = conn.execute(
        "SELECT upstream_name, manual_class FROM tool_catalog WHERE service_id = ?",
        (service_id,),
    ).fetchall()
    if not rows:
        return ["    tools: []"]
    lines = ["    tools:"]
    for name, manual in sorted(rows):
        lines.append(f"      - name: {_q(name)}")
        if manual is not None:
            lines.append(f"        class: {_q(manual)}")
    return lines


def _placement(conn: sqlite3.Connection, connection_id: str, role_id: str, path: str) -> list[str]:
    """One placement and its policy (BR-176 to BR-180), no baseline nor suspension."""
    row = conn.execute(
        "SELECT mode, pin FROM placement WHERE connection_id = ? AND role_id = ?",
        (connection_id, role_id),
    ).fetchone()
    mode, pin = (row[0], row[1]) if row is not None else (None, None)
    members: dict[str, list[str]] = {k: [] for k in LISTS}
    for which, name in conn.execute(
        "SELECT list, upstream_name FROM placement_tool WHERE connection_id = ? AND role_id = ?",
        (connection_id, role_id),
    ):
        members[which].append(name)
    pinned = [
        r[0]
        for r in conn.execute(
            "SELECT upstream_name FROM pinned_definition WHERE connection_id = ? AND role_id = ?"
            " AND tool_pin = 1",
            (connection_id, role_id),
        )
    ]
    rules = conn.execute(
        "SELECT upstream_name, argument, allowed_values FROM argument_rule"
        " WHERE connection_id = ? AND role_id = ?",
        (connection_id, role_id),
    ).fetchall()
    lines = [
        f"      - path: {_q(path)}",
        f"        mode: {_q(mode or 'inherit')}",
        f"        pin: {_q(pin or 'inherit')}",
    ]
    lines += [f"        {k}: {_list(sorted(members[k]))}" for k in LISTS]
    lines.append(f"        pinned_tools: {_list(sorted(pinned))}")
    if not rules:
        lines.append("        arguments: []")
        return lines
    lines.append("        arguments:")
    for name, argument, values in sorted(rules):
        lines += [
            f"          - tool: {_q(name)}",
            f"            argument: {_q(argument)}",
            f"            values: {_list(sorted(policy.stored_rule(argument, values).values))}",
        ]
    return lines


def _library(conn: sqlite3.Connection, tree: Tree) -> list[str]:
    services, applications, conns = connections.exported(conn)
    roles = {tree.path(n): n.id for n in tree.nodes.values() if n.level == "role"}
    lines = ["services: []"] if not services else ["services:"]
    for s in services:
        lines += [
            f"  - id: {_q(s.id)}",
            f"    model: {_q(s.model)}",
            f"    url: {_q(s.base_url)}",
            f"    scopes: {_list(s.scopes.split())}",
            f"    internal_host: {'true' if s.internal_host else 'false'}",
            *_catalogue(conn, s.id),
        ]
    lines += ["applications: []"] if not applications else ["applications:"]
    for a in applications:
        lines += [
            f"  - id: {_q(a.id)}",
            f"    service: {_q(a.service_id)}",
            f"    mode: {_q(a.mode)}",
            f"    client_id: {_q(a.client_id)}",
        ]
    lines += ["connections: []"] if not conns else ["connections:"]
    for c in conns:
        lines += [
            f"  - name: {_q(c.name)}",
            f"    service: {_q(c.service)}",
            f"    prefix: {_q(c.prefix)}",
            f"    application: {'null' if c.application is None else _q(c.application)}",
            f"    state: {_q(c.state)}",
        ]
        if not c.placements:
            lines.append("    placements: []")
            continue
        lines.append("    placements:")
        for path in c.placements:
            try:
                lines += _placement(conn, c.id, roles[path], path)
            except policy.StateError as exc:  # named for the operator (BR-185; lot C review, N2)
                raise policy.StateError(str(exc), c.name) from None
    return lines


def export_yaml(conn: sqlite3.Connection) -> str:
    tree = Tree.load(conn)
    lines = ["# hullgate: tree export (BR-070)", f"format: {FORMAT}", "organisation:"]
    lines.append(f"  id: {_q(tree.public_id)}")  # public, not internal (BR-160, F-014 §3.3)
    lines.extend(_node(tree, tree.org, "  ", ""))
    lines.extend(_library(conn, tree))
    return "\n".join(lines) + "\n"


def export_document(conn: sqlite3.Connection) -> dict[str, Any]:
    """The public export model without serialising or parsing YAML under the store lock."""
    tree = Tree.load(conn)

    def node(n: Node) -> dict[str, Any]:
        value: dict[str, Any] = {
            "level": LEVEL_KEYS[n.level],
            "name": n.name,
            "settings": {k: v for (nid, k), v in tree.settings.items() if nid == n.id},
        }
        if n.level != "org":
            value["segment"] = n.slug
        if n.level == "env":
            value["sensitive"] = n.sensitive
        if n.level in CHILD_KEYS:
            value[CHILD_KEYS[n.level]] = [node(kid) for kid in tree.kids(n)]
        return value

    def placement(connection_id: str, path: str) -> dict[str, Any]:
        value = connections.placement_document(conn, connection_id, roles[path]) or {}
        return {
            **value,
            "mode": value.get("mode") or "inherit",
            "pin": value.get("pin") or "inherit",
        }

    services, applications, conns = connections.exported(conn)
    roles = {tree.path(n): n.id for n in tree.nodes.values() if n.level == "role"}
    return {
        "format": FORMAT,
        "organisation": {"id": tree.public_id, **node(tree.org)},
        "services": [
            {
                "id": s.id,
                "model": s.model,
                "url": s.base_url,
                "scopes": s.scopes.split(),
                "internal_host": s.internal_host,
                "tools": [
                    {"name": name, **({"class": manual} if manual else {})}
                    for name, manual in conn.execute(
                        "SELECT upstream_name, manual_class FROM tool_catalog"
                        " WHERE service_id = ? ORDER BY upstream_name",
                        (s.id,),
                    )
                ],
            }
            for s in services
        ],
        "applications": [
            {"id": a.id, "service": a.service_id, "mode": a.mode, "client_id": a.client_id}
            for a in applications
        ],
        "connections": [
            {
                "name": c.name,
                "service": c.service,
                "prefix": c.prefix,
                "application": c.application,
                "state": c.state,
                "placements": [{**placement(c.id, path), "path": path} for path in c.placements],
            }
            for c in conns
        ],
    }
