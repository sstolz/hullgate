"""Shared fixtures of the F-015 tests: a fake upstream carrying Gitea tool names and the
annotations `gitea-mcp` publishes (§12: reads carry `readOnlyHint: true`, writes `readOnlyHint:
false` without `destructiveHint`, so `destructive` by default), and small readers of the policy
tables. Built on the relay test bed (`tests.relay_bed`)."""

from __future__ import annotations

import json
from typing import Any

from tests.fake_upstream import DESTRUCTIVE, READ, Upstream, tool
from tests.relay_bed import Bed
from tests.support import Gateway

GITEA_WRITE: dict[str, Any] = {"readOnlyHint": False}  # no destructiveHint: `destructive`


def method_schema(*methods: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": list(methods)},
            "index": {"type": "integer"},
            "body": {"type": "string"},
        },
        "required": ["method"],
    }


def gitea_tools() -> list[dict[str, Any]]:
    return [
        tool("list_issues", annotations=READ),
        tool("get_pull_request", annotations=READ),
        tool("issue_write", method_schema("create", "update", "add_comment"), GITEA_WRITE),
        tool("pull_request_write", method_schema("create", "update", "merge"), GITEA_WRITE),
        tool(
            "pull_request_review_write",
            method_schema("create", "submit", "delete"),
            GITEA_WRITE,
        ),
        tool("delete_file", annotations=DESTRUCTIVE),
    ]


def gitea(bed: Bed, prefix: str, *roles: str, confirm: bool = False) -> Upstream:
    up = bed.connection(prefix, *roles, confirm=confirm)
    up.tools = gitea_tools()
    return up


def catalogue(gw: Gateway, name: str, role: str) -> None:
    """Feed the catalogue as the operator does first (`connection tools`)."""
    gw.ok("connection", "tools", name, "--role", role)


def rows(gw: Gateway, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    conn = gw.connect()
    try:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def policy_rows(gw: Gateway) -> tuple[list[tuple[Any, ...]], ...]:
    """Every policy row, for "nothing written" comparisons."""
    return (
        rows(gw, "SELECT * FROM placement_tool ORDER BY 1, 2, 3, 4"),
        rows(gw, "SELECT * FROM argument_rule ORDER BY 1, 2, 3"),
        rows(gw, "SELECT connection_id, role_id, mode, pin FROM placement ORDER BY 1, 2"),
        rows(gw, "SELECT * FROM tool_catalog ORDER BY 1, 2"),
        rows(gw, "SELECT count(*) FROM audit"),
    )


def audits(gw: Gateway, action: str) -> list[dict[str, Any]]:
    out = []
    for a in gw.audit():
        if a["action"] == action:
            entry = dict(a)
            for key in ("before", "after", "detail"):
                if entry[key] is not None:
                    entry[key] = json.loads(entry[key])
            out.append(entry)
    return out


def called(up: Upstream) -> list[str]:
    """Upstream names of the `tools/call` the upstream received, in order."""
    out = []
    for r in up.calls("tools/call"):
        params = (r.body or {}).get("params") or {}
        out.append(str(params.get("name")))
    return out
