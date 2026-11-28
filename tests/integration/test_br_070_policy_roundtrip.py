"""BR-070 v4 (F-015 lot C, AC-20): the export carries the action policy. For a same state it is
byte-identical; each placement is a mapping (`path`, `mode`, `pin`, `open`, `deny`, `allowlist`,
`pinned_tools`, `arguments`, lists sorted); each service carries `tools`, its catalogue names with
their manual class; no baseline, no queue state, no secret; and every list entry of the document
names a tool the same document carries, so an import on a fresh instance can write the names
before the lists that cite them (BR-176)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests.fake_upstream import DESTRUCTIVE, READ, tool
from tests.policy_support import catalogue, method_schema, rows
from tests.relay_bed import A, B, Bed
from tests.support import REGISTRY, Gateway, export_of

PE = "mpx/pe"
PF = "mpx/pf"


def parse(text: str) -> Any:
    """The YAML subset `hullgate export` writes: block mappings and `- ` sequences by indentation,
    scalars as JSON (strings quoted, `true`, `false`, `null`, integers, `[]`, `{}`, flow
    lists) or plain (`level: organisation`)."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    pos = 0

    def indent(ln: str) -> int:
        return len(ln) - len(ln.lstrip(" "))

    def block(level: int) -> Any:
        nonlocal pos
        if lines[pos].lstrip().startswith("- "):
            seq = []
            while pos < len(lines) and indent(lines[pos]) == level and \
                    lines[pos].lstrip().startswith("- "):  # fmt: skip
                lines[pos] = " " * (level + 2) + lines[pos].lstrip()[2:]
                seq.append(block(level + 2))
            return seq
        out: dict[str, Any] = {}
        while pos < len(lines) and indent(lines[pos]) == level and \
                not lines[pos].lstrip().startswith("- "):  # fmt: skip
            key, _, rest = lines[pos].strip().partition(":")
            pos += 1
            rest = rest.strip()
            if rest:
                try:
                    out[key] = json.loads(rest)
                except ValueError:
                    out[key] = rest  # plain scalar (`level: organisation`)
            else:
                out[key] = block(indent(lines[pos]))
        return out

    return block(0)


def build(gw: Gateway, order: int) -> None:
    """The same policy state, written in two orders."""
    bed = Bed(gw)
    tools = [tool("list_issues", annotations=READ), tool("delete_repo", annotations=DESTRUCTIVE),
             tool("pull_request_write", method_schema("create", "merge"), {"readOnlyHint": False}),
             tool("get_me", annotations=READ)]  # fmt: skip
    up_e = bed.connection("pe", A, B)
    up_e.tools = [dict(t) for t in tools]
    up_f = bed.connection("pf", A)
    up_f.tools = [dict(t) for t in tools[:2]]
    catalogue(gw, PE, A)
    catalogue(gw, PF, A)
    steps = [
        ("tool", "classify", "spe", "pull_request_write", "write"),
        ("tool", "classify", "spe", "get_me", "destructive"),
        ("policy", "mode", PE, "--role", A, "read"),
        ("policy", "pin", PE, "--role", B, "on"),
        ("policy", "add", PE, "--role", A, "open", "pull_request_write", "delete_repo"),
        ("policy", "add", PE, "--role", A, "deny", "get_me"),
        ("policy", "add", PE, "--role", B, "allowlist", "list_issues", "get_me"),
        ("policy", "arg", PE, "--role", A, "pull_request_write", "method", "merge", "create"),
        ("policy", "pin-tool", PE, "--role", A, "list_issues"),
        ("policy", "add", PF, "--role", A, "deny", "delete_repo"),
    ]
    for step in steps if order == 0 else reversed(steps):
        gw.ok(*step)
    catalogue(gw, PE, B)  # baselines under the pin of B


def test_br_070_v4_same_state_gives_byte_identical_export(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = Gateway(tmp_path / "a"), Gateway(tmp_path / "b")
    try:
        build(first, 0)
        build(second, 1)
        one = export_of(first)
        assert first.ok("export") == first.ok("export")
        assert one == export_of(second)  # their drawn identifiers only differ (F-014)
        baselines = [r[0] for r in rows(first, "SELECT sha256 FROM pinned_definition"
                                               " WHERE sha256 IS NOT NULL")]  # fmt: skip
    finally:
        first.close()
        second.close()
    assert baselines  # the state holds baselines; the export does not
    for digest in baselines:
        assert digest not in one
    assert not re.search(r"\b[0-9a-f]{64}\b", one)
    for word in ("queued", "queue", "sha256", "suspended", "baseline", "annotation"):
        assert word not in one, word
    for needle in REGISTRY.secrets:
        assert needle not in one


def test_br_070_v4_carries_policy_catalogue_and_manual_classes(gw: Gateway) -> None:
    build(gw, 0)
    doc = parse(gw.ok("export"))
    assert doc["format"] == 5
    services = {s["id"]: s for s in doc["services"]}
    assert services["spe"]["tools"] == [
        {"name": "delete_repo"},
        {"name": "get_me", "class": "destructive"},
        {"name": "list_issues"},
        {"name": "pull_request_write", "class": "write"},
    ]
    assert services["spf"]["tools"] == [{"name": "delete_repo"}, {"name": "list_issues"}]
    conns = {c["name"]: c for c in doc["connections"]}
    a, b = conns[PE]["placements"]
    assert a == {
        "path": A, "mode": "read", "pin": "inherit",
        "open": ["delete_repo", "pull_request_write"], "deny": ["get_me"], "allowlist": [],
        "pinned_tools": ["list_issues"],
        "arguments": [{"tool": "pull_request_write", "argument": "method",
                       "values": ["create", "merge"]}],
    }  # fmt: skip
    assert b == {
        "path": B, "mode": "inherit", "pin": "on", "open": [], "deny": [],
        "allowlist": ["get_me", "list_issues"], "pinned_tools": [], "arguments": [],
    }  # fmt: skip
    assert conns[PF]["placements"][0]["deny"] == ["delete_repo"]


def test_br_070_v4_every_list_entry_names_a_tool_the_document_carries(gw: Gateway) -> None:
    build(gw, 0)
    doc = parse(gw.ok("export"))
    names = {s["id"]: {t["name"] for t in s["tools"]} for s in doc["services"]}
    cited = 0
    for c in doc["connections"]:
        known = names[c["service"]]
        for p in c["placements"]:
            entries = p["open"] + p["deny"] + p["allowlist"] + p["pinned_tools"]
            entries += [r["tool"] for r in p["arguments"]]
            assert set(entries) <= known, (c["name"], p["path"], set(entries) - known)
            cited += len(entries)
    assert cited == 8


def test_br_070_v4_blank_placements_and_empty_catalogue(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("pz", A)
    doc = parse(gw.ok("export"))
    [svc] = [s for s in doc["services"] if s["id"] == "spz"]
    assert svc["tools"] == []
    [conn] = [c for c in doc["connections"] if c["name"] == "mpx/pz"]
    assert conn["placements"] == [{
        "path": A, "mode": "inherit", "pin": "inherit", "open": [], "deny": [], "allowlist": [],
        "pinned_tools": [], "arguments": [],
    }]  # fmt: skip
