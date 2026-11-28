"""BR-183 (F-015 AC-17): `policy preview` shows, for a role, each tool of its placed connections
with its public name, class and source, whether it is exposed, every reason that hides it, and
its opening, grouped actions, argument rule and `sensitive_via`. On a role covering the rows of
§3.2, each row's `exposed` equals what `tools/list` returns; 0 `tools/call`; an unavailable
connection gets its line and the others are listed; a role without connection gives one line;
no secret in the output."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fake_upstream import DESTRUCTIVE, NONE, READ, Fault, tool
from tests.integration.test_br_174_list_and_call import EXPECTED, SETUPS, TOOLS
from tests.policy_support import catalogue, gitea
from tests.relay_bed import A, B, Bed, names
from tests.support import REGISTRY, Gateway


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def _preview(gw: Gateway, role: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(gw.ok("policy", "preview", role, "--json"))
    return data


@pytest.mark.parametrize("prefix", sorted(SETUPS))
def test_br_183_preview_agrees_with_tools_list(bed: Bed, prefix: str) -> None:
    gw = bed.gw
    role, lists = SETUPS[prefix]
    up = bed.connection(prefix, role)
    up.tools = [dict(t) for t in TOOLS]
    catalogue(gw, f"mpx/{prefix}", role)
    for which, members in lists.items():
        gw.ok("policy", "add", f"mpx/{prefix}", "--role", role, which, *members)
    seen = _preview(gw, role)
    with bed.serve() as client:
        listed = set(names(client, role, bed.token(role)))
    rows = {t["upstream_name"]: t for t in seen["tools"]}
    assert set(rows) == {t["name"] for t in TOOLS}
    for upstream, row in rows.items():
        assert row["exposed"] is (row["public_name"] in listed)
        first = EXPECTED[prefix][upstream]
        assert (row["reasons"][:1] or [None]) == [first]
    exposed_first = [t["exposed"] for t in seen["tools"]]
    assert exposed_first == sorted(exposed_first, reverse=True)
    assert up.calls("tools/call") == []
    assert rows["d_opened"]["opened"] is True
    # every applicable reason, not only the first (ps: `w` is not listed and closed)
    if prefix == "ps":
        assert rows["w"]["reasons"] == ["not_allowlisted", "write_closed"]


def test_br_183_grouped_rule_source_and_class(bed: Bed) -> None:
    gw = bed.gw
    gitea(bed, "gt", A)
    catalogue(gw, "mpx/gt", A)
    gw.ok("policy", "add", "mpx/gt", "--role", A, "open", "pull_request_write")
    gw.ok("policy", "arg", "mpx/gt", "--role", A, "pull_request_write", "method", "create")
    gw.ok("tool", "classify", "sgt", "issue_write", "write")
    rows = {t["upstream_name"]: t for t in _preview(gw, A)["tools"]}
    pr = rows["pull_request_write"]
    assert pr["grouped"] == {"method": ["create", "update", "merge"]}
    assert pr["argument_rule"] == {"argument": "method", "values": ["create"]}
    assert (pr["class"], pr["source"], pr["opened"], pr["pinned"]) == (
        "destructive", "default", True, True,  # the rule implies the pin (BR-180)
    )  # fmt: skip
    assert (rows["issue_write"]["class"], rows["issue_write"]["source"]) == ("write", "manual")
    listed = rows["list_issues"]
    assert (listed["class"], listed["source"]) == ("read", "annotations")
    assert rows["delete_file"]["reasons"] == ["above_mode"]


def test_br_183_unavailable_connection_and_empty_role(bed: Bed) -> None:
    gw = bed.gw
    ok = bed.connection("ok", A)
    ok.tools = [tool("a", annotations=READ), tool("b", annotations=NONE)]
    down = bed.connection("dn", A)
    for _ in range(2):  # one per preview below
        down.faults.append(Fault("status", on="tools/list", status=503))
    seen = _preview(gw, A)
    assert [u["connection"] for u in seen["unavailable"]] == ["mpx/dn"]
    assert seen["unavailable"][0]["code"] == "upstream_unavailable"
    assert {t["public_name"] for t in seen["tools"]} == {"ok__a", "ok__b"}
    text = gw.ok("policy", "preview", A)
    assert "unavailable  mpx/dn  upstream_unavailable" in text
    empty = gw.cli("policy", "preview", B)
    assert empty.code == 0
    assert empty.out.strip() == f"rights of {B}: no connection"
    assert gw.cli("policy", "preview", "mpx/cairn").code == 2  # not a role


def test_br_183_no_secret_in_the_output(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("se", A)
    up.tools = [tool("a", annotations=DESTRUCTIVE)]
    out = gw.ok("policy", "preview", A, "--json") + gw.ok("policy", "preview", A)
    for secret in REGISTRY.secrets:
        if len(secret) >= 12:
            assert secret not in out
    assert "Bearer" not in out
