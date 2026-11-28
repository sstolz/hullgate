"""BR-187 (F-015 AC-15): on a placement that already holds baselines and is pinned whole, a tool
without a baseline is suspended as new and holds a row with `suspended_at`; the first listing of
a placement suspends nothing; a placement with tool pins only leaves a new tool alone."""

from __future__ import annotations

from typing import Any

import pytest

from hullgate import relay
from tests.fake_upstream import READ, FakeClock, tool
from tests.policy_support import audits, called, catalogue, rows
from tests.relay_bed import A, Bed, S, call, names, unknown
from tests.support import Gateway

PN = "mpx/pn"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    b = Bed(gw)
    up = b.connection("pn", A)
    up.tools = [tool("look", annotations=READ), tool("seek", annotations=READ)]
    catalogue(gw, PN, A)
    return b


def state(gw: Gateway, role: str) -> dict[str, tuple[Any, ...]]:
    return {
        r[0]: r[1:]
        for r in rows(
            gw,
            "SELECT upstream_name, sha256 IS NOT NULL, suspended_at IS NOT NULL"
            " FROM pinned_definition WHERE connection_id = ? AND role_id = ?",
            gw.connection_id(PN),
            gw.node_id(role),
        )
    }


def test_br_187_a_new_tool_on_a_placement_pinned_whole_is_suspended(bed: Bed) -> None:
    gw = bed.gw
    # S is sensitive: `pinning` `auto` pins it whole; its allowlist shows the listed reads
    gw.ok("connection", "place", PN, S, "--confirm-share")
    gw.ok("policy", "add", PN, "--role", S, "allowlist", "look", "seek")
    clock = FakeClock()
    with bed.serve(gw.relay(upclock=clock)) as client:
        assert names(client, S, bed.token(S)) == ["pn__look", "pn__seek"]
        assert state(gw, S) == {"look": (1, 0), "seek": (1, 0)}  # first listing: nothing
        bed.upstream("pn").tools.append(tool("peek", annotations=READ))
        clock.advance(relay.CACHE_TTL_S + 1)
        assert names(client, S, bed.token(S)) == ["pn__look", "pn__seek"]
        assert state(gw, S)["peek"] == (0, 1)
        gw.ok("policy", "add", PN, "--role", S, "allowlist", "peek")  # listed: still suspended
        assert names(client, S, bed.token(S)) == ["pn__look", "pn__seek"]
        response = call(client, S, bed.token(S), "pn__peek")
        assert unknown(response)
        assert "peek" not in called(bed.upstream("pn"))
    [entry] = audits(gw, "tool.suspended")
    assert entry["detail"]["new"] is True
    assert entry["before"] == {"sha256": None}
    [refusal] = [e for e in gw.audit() if e["action"] == "relay.tool_call"]
    assert refusal["reason"] == "suspended"


def test_br_187_tool_pins_only_leave_a_new_tool_alone(bed: Bed) -> None:
    gw = bed.gw
    gw.ok("policy", "pin-tool", PN, "--role", A, "look")
    clock = FakeClock()
    with bed.serve(gw.relay(upclock=clock)) as client:
        assert names(client, A, bed.token(A)) == ["pn__look", "pn__seek"]
        assert state(gw, A) == {"look": (1, 0)}
        bed.upstream("pn").tools.append(tool("peek", annotations=READ))
        clock.advance(relay.CACHE_TTL_S + 1)
        assert names(client, A, bed.token(A)) == ["pn__look", "pn__seek", "pn__peek"]
    assert state(gw, A) == {"look": (1, 0)}
    assert audits(gw, "tool.suspended") == []


def test_br_187_the_first_listing_of_a_placement_suspends_nothing(bed: Bed) -> None:
    gw = bed.gw
    gw.ok("policy", "pin", PN, "--role", A, "on")
    bed.upstream("pn").tools.append(tool("peek", annotations=READ))
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["pn__look", "pn__seek", "pn__peek"]
    assert state(gw, A) == {"look": (1, 0), "seek": (1, 0), "peek": (1, 0)}
    assert audits(gw, "tool.suspended") == []
