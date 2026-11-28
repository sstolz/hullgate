"""BR-176, BR-049 (F-015 AC-9, AC-10): `open`, `deny`, `allowlist` are sets of upstream names on
one placement. An unknown tool, an unplaced connection and a locked node are refused with nothing
written; `allowlist --class read` lists the current reads once, one audit entry each, and a later
read stays out; `connection unplace`, `connection remove` and `tree delete` leave no policy row;
a re-placement starts blank; a tool that disappears upstream keeps its entry, inert."""

from __future__ import annotations

import json

import pytest

from tests.fake_upstream import READ, tool
from tests.policy_support import audits, catalogue, policy_rows, rows
from tests.relay_bed import A, B, Bed, names
from tests.support import Gateway

LI = "mpx/li"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    b = Bed(gw)
    up = b.connection("li", A)
    up.tools = [tool("look", annotations=READ), tool("find", annotations=READ), tool("edit")]
    catalogue(gw, LI, A)
    return b


def _policy_count(gw: Gateway) -> tuple[int, int]:
    return (
        rows(gw, "SELECT count(*) FROM placement_tool")[0][0],
        rows(gw, "SELECT count(*) FROM argument_rule")[0][0],
    )


@pytest.mark.parametrize(
    "args",
    [
        ["policy", "add", LI, "--role", A, "allowlist", "nothing"],
        ["policy", "add", LI, "--role", A, "deny", "look", "nothing"],
        ["policy", "add", LI, "--role", B, "open", "look"],  # not placed on B
        ["policy", "add", LI, "--role", A, "allowlist"],  # no tool
        ["policy", "add", LI, "--role", A, "open", "--class", "read"],
        ["policy", "add", LI, "--role", A, "allowlist", "look", "--class", "read"],
        ["policy", "remove", LI, "--role", A, "deny", "look"],  # not in the list
        ["policy", "mode", LI, "--role", B, "read"],
        ["policy", "add", "mpx/nope", "--role", A, "open", "look"],
    ],
)
def test_br_176_entry_refusals_write_nothing(bed: Bed, args: list[str]) -> None:
    before = policy_rows(bed.gw)
    result = bed.gw.cli(*args)
    assert result.code == 2, result
    assert policy_rows(bed.gw) == before


@pytest.mark.parametrize(
    "args",
    [
        ["policy", "add", LI, "--role", A, "open", "look"],
        ["policy", "add", LI, "--role", A, "allowlist", "--class", "read"],
        ["policy", "remove", LI, "--role", A, "deny", "edit"],
        ["policy", "mode", LI, "--role", A, "read"],
        ["policy", "arg", LI, "--role", A, "edit", "x", "y"],
        ["policy", "arg-remove", LI, "--role", A, "edit"],
    ],
)
def test_br_049_every_policy_write_refused_on_a_locked_node(bed: Bed, args: list[str]) -> None:
    gw = bed.gw
    gw.ok("policy", "add", LI, "--role", A, "deny", "edit")
    gw.ok("tree", "lock", "mpx/cairn")
    before = policy_rows(gw)
    result = gw.cli(*args)
    assert result.code == 2, result
    assert "locked by mpx/cairn" in result.err
    assert policy_rows(gw) == before


def test_br_176_class_read_lists_current_reads_once(bed: Bed) -> None:
    gw = bed.gw
    out = gw.ok("policy", "add", LI, "--role", A, "allowlist", "--class", "read")
    assert "added to allowlist: 2" in out
    entries = audits(gw, "policy.list_add")
    assert sorted(e["detail"]["tool"] for e in entries) == ["find", "look"]
    assert all(e["detail"]["by_class"] == "read" for e in entries)
    up = bed.upstream("li")
    up.tools.append(tool("later", annotations=READ))
    gw.clock.advance(301)
    with bed.serve() as client:
        assert sorted(names(client, A, bed.token(A))) == ["li__find", "li__look"]  # later: out
    shown = json.loads(gw.ok("policy", "show", LI, "--role", A, "--json"))
    assert shown["allowlist"] == ["find", "look"]


def test_br_176_policy_goes_with_its_placement(bed: Bed) -> None:
    gw = bed.gw
    gw.ok("connection", "place", LI, B)
    for role in (A, B):
        gw.ok("policy", "add", LI, "--role", role, "deny", "edit")
        gw.ok("policy", "mode", LI, "--role", role, "read")
    assert _policy_count(gw) == (2, 0)
    gw.ok("connection", "unplace", LI, B)
    assert rows(gw, "SELECT role_id FROM placement_tool") == [(gw.node_id(A),)]
    gw.ok("connection", "place", LI, B)
    shown = json.loads(gw.ok("policy", "show", LI, "--role", B, "--json"))
    assert (shown["deny"], shown["mode"], shown["mode_origin"]) == ([], "write", "/")
    gw.ok("tree", "delete", "mpx/cairn/staging/rev", "--confirm", "mpx/cairn/staging/rev")
    gw.ok("connection", "remove", LI, "--confirm", LI)
    assert _policy_count(gw) == (0, 0)


def test_br_176_tree_delete_erases_the_policy(bed: Bed) -> None:
    gw = bed.gw
    gw.ok("policy", "add", LI, "--role", A, "allowlist", "look")
    gw.ok("tree", "delete", A, "--confirm", A)
    assert _policy_count(gw) == (0, 0)


def test_br_176_disappeared_tool_keeps_its_entry_inert(bed: Bed) -> None:
    gw = bed.gw
    gw.ok("policy", "add", LI, "--role", A, "allowlist", "look", "edit")
    up = bed.upstream("li")
    up.tools = [t for t in up.tools if t["name"] != "look"]
    gw.clock.advance(301)
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["li__edit"]
    shown = json.loads(gw.ok("policy", "show", LI, "--role", A, "--json"))
    assert shown["allowlist"] == ["edit", "look"]
