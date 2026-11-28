"""BR-174 (F-015 AC-7): `tools/list` and `tools/call` agree on exposure on every row of steps 2 to
5b of §3.2 (step 1, suspension, is lot B; step 6 is call-only, BR-178). For each tool of three
placements covering the rows: announced if and only if a call reaches the upstream; a hidden
tool answers "unknown tool" with 0 `tools/call` and the audit keeps its first reason."""

from __future__ import annotations

import pytest

from tests.fake_upstream import DESTRUCTIVE, READ, tool
from tests.policy_support import audits, called, catalogue
from tests.relay_bed import A, Bed, S, call, names, unknown
from tests.support import Gateway

TOOLS = [
    tool("r", annotations=READ),
    tool("w"),
    tool("d", annotations=DESTRUCTIVE),
    tool("r_denied", annotations=READ),
    tool("d_opened", annotations=DESTRUCTIVE),
    tool("d_opened_denied", annotations=DESTRUCTIVE),
    tool("r_listed", annotations=READ),
    tool("w_listed"),
    tool("d_listed", annotations=DESTRUCTIVE),
]

# (prefix, role, lists) -> expected reason per tool (None: exposed)
SETUPS = {
    "pa": (A, {"deny": ["r_denied", "d_opened_denied"], "open": ["d_opened", "d_opened_denied"]}),
    "pl": (A, {"allowlist": ["r_listed", "w_listed", "d_listed"], "open": ["d_opened"]}),
    "ps": (S, {"allowlist": ["r_listed", "w_listed", "d_listed"], "open": ["d_opened"],
               "deny": ["r_denied"]}),
}  # fmt: skip
EXPECTED = {
    "pa": {"r": None, "w": None, "d": "above_mode", "r_denied": "denied", "d_opened": None,
           "d_opened_denied": "denied", "r_listed": None, "w_listed": None,
           "d_listed": "above_mode"},
    "pl": {"r": "not_allowlisted", "w": "not_allowlisted", "d": "not_allowlisted",
           "r_denied": "not_allowlisted", "d_opened": None, "d_opened_denied": "not_allowlisted",
           "r_listed": None, "w_listed": None, "d_listed": "above_mode"},
    "ps": {"r": "not_allowlisted", "w": "not_allowlisted", "d": "not_allowlisted",
           "r_denied": "denied", "d_opened": None, "d_opened_denied": "not_allowlisted",
           "r_listed": None, "w_listed": "write_closed", "d_listed": "write_closed"},
}  # fmt: skip


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


@pytest.mark.parametrize("prefix", sorted(SETUPS))
def test_br_174_list_and_call_agree(bed: Bed, prefix: str) -> None:
    gw = bed.gw
    role, lists = SETUPS[prefix]
    up = bed.connection(prefix, role)
    up.tools = [dict(t) for t in TOOLS]
    name = f"mpx/{prefix}"
    catalogue(gw, name, role)
    for which, members in lists.items():
        gw.ok("policy", "add", name, "--role", role, which, *members)
    with bed.serve() as client:
        announced = set(names(client, role, bed.token(role)))
        outcomes = {
            t["name"]: call(client, role, bed.token(role), f"{prefix}__{t['name']}") for t in TOOLS
        }
    expected = EXPECTED[prefix]
    assert announced == {f"{prefix}__{n}" for n, r in expected.items() if r is None}
    reached = set(called(up))
    for upstream, response in outcomes.items():
        exposed = expected[upstream] is None
        assert (f"{prefix}__{upstream}" in announced) is exposed
        assert (upstream in reached) is exposed, upstream
        assert unknown(response) is not exposed
    refused = {a["detail"]["upstream_name"]: a["reason"] for a in audits(gw, "relay.tool_call")
               if a["result"] == "refused"}  # fmt: skip
    assert refused == {n: r for n, r in expected.items() if r is not None}
