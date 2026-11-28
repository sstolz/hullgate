"""BR-181 (F-015 AC-15): a pinned tool whose upstream definition no longer hashes to its baseline
is suspended on that placement: hidden and refused with 0 `tools/call`, the other tools stay,
reverting upstream does not lift it, one `tool.suspended` per (placement, tool, hash); the
suspension is the `pinned_definition` row, written whatever becomes of the audit entry, and a
failure of that write fails the listing closed."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from hullgate import audit, relay
from tests.fake_upstream import READ, FakeClock, Upstream, tool
from tests.policy_support import audits, called, catalogue, rows
from tests.relay_bed import A, B, Bed, call, gateway_error, names, unknown
from tests.support import Gateway

PN = "mpx/pn"


def schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"q": {"type": "string"}}}


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    b = Bed(gw)
    up = b.connection("pn", A, B)
    up.tools = [tool("look", schema(), READ), tool("other", schema(), READ)]
    catalogue(gw, PN, A)
    gw.ok("policy", "pin", PN, "--role", A, "on")
    return b


@pytest.fixture
def served(bed: Bed) -> Iterator[tuple[TestClient, FakeClock]]:
    clock = FakeClock()
    with bed.serve(bed.gw.relay(upclock=clock)) as client:
        yield client, clock


def refill(clock: FakeClock) -> None:
    clock.advance(relay.CACHE_TTL_S + 1)


def look(bed: Bed) -> dict[str, Any]:
    t: dict[str, Any] = bed.upstream("pn").tools[0]
    return t


def suspended_at(gw: Gateway, name: str, role: str = A) -> list[tuple[Any, ...]]:
    return rows(
        gw,
        "SELECT sha256 IS NOT NULL, suspended_at IS NOT NULL FROM pinned_definition"
        " WHERE connection_id = ? AND role_id = ? AND upstream_name = ?",
        gw.connection_id(PN), gw.node_id(role), name,
    )  # fmt: skip


CHANGES = {
    "title": lambda t: t.update(title="Look"),
    "description": lambda t: t.update(description="tool look, rewritten"),
    "inputSchema": lambda t: t["inputSchema"]["properties"].update(r={"type": "string"}),
    "outputSchema": lambda t: t.update(outputSchema={"type": "object"}),
    "annotations": lambda t: t["annotations"].update(openWorldHint=True),
}


@pytest.mark.parametrize("field", sorted(CHANGES))
def test_br_181_a_changed_field_suspends_the_tool_alone(
    bed: Bed, served: tuple[TestClient, FakeClock], field: str
) -> None:
    gw, (client, clock) = bed.gw, served
    up = bed.upstream("pn")
    assert names(client, A, bed.token(A)) == ["pn__look", "pn__other"]  # first listing: baselines
    assert suspended_at(gw, "look") == [(1, 0)]
    CHANGES[field](look(bed))
    refill(clock)
    assert names(client, A, bed.token(A)) == ["pn__other"]
    response = call(client, A, bed.token(A), "pn__look", {"q": "x"})
    assert unknown(response)
    assert "look" not in called(up)
    assert suspended_at(gw, "look") == [(1, 1)]
    assert suspended_at(gw, "other") == [(1, 0)]
    [entry] = [e for e in gw.audit() if e["action"] == "relay.tool_call"]
    assert (entry["result"], entry["reason"]) == ("refused", "suspended")
    # the role B of the same connection is not pinned (`auto`, not sensitive): it keeps the tool
    assert names(client, B, bed.token(B)) == ["pn__look", "pn__other"]


def test_br_181_reverting_upstream_does_not_lift_it(
    bed: Bed, served: tuple[TestClient, FakeClock]
) -> None:
    gw, (client, clock) = bed.gw, served
    assert names(client, A, bed.token(A)) == ["pn__look", "pn__other"]
    original = dict(look(bed))
    look(bed)["description"] = "changed"
    refill(clock)
    assert names(client, A, bed.token(A)) == ["pn__other"]
    bed.upstream("pn").tools[0] = original
    for _ in range(2):
        refill(clock)
        assert names(client, A, bed.token(A)) == ["pn__other"]
    assert suspended_at(gw, "look") == [(1, 1)]


def test_br_181_one_audit_entry_per_hash(bed: Bed, served: tuple[TestClient, FakeClock]) -> None:
    gw, (client, clock) = bed.gw, served
    names(client, A, bed.token(A))
    look(bed)["description"] = "v2"
    for _ in range(3):  # the same change read three times
        refill(clock)
        names(client, A, bed.token(A))
    [first] = audits(gw, "tool.suspended")
    assert first["actor"] == "gateway"
    assert first["node_id"] == gw.node_id(A)
    assert first["path"] == PN
    assert first["detail"]["tool"] == "look"
    assert first["detail"]["new"] is False
    assert first["before"]["sha256"] != first["after"]["sha256"]
    assert first["after"]["suspended"] is True
    look(bed)["description"] = "v3"  # another hash: one more entry
    refill(clock)
    names(client, A, bed.token(A))
    assert len(audits(gw, "tool.suspended")) == 2


def test_br_181_a_failed_audit_write_leaves_the_suspension(
    bed: Bed, served: tuple[TestClient, FakeClock], monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, (client, clock) = bed.gw, served
    names(client, A, bed.token(A))
    original = audit.write

    def failing(conn: sqlite3.Connection, **fields: Any) -> None:
        if fields.get("action") == "tool.suspended":
            raise sqlite3.OperationalError("disk I/O error")
        original(conn, **fields)

    monkeypatch.setattr(audit, "write", failing)
    look(bed)["description"] = "v2"
    refill(clock)
    assert names(client, A, bed.token(A)) == ["pn__other"]
    assert suspended_at(gw, "look") == [(1, 1)]
    assert audits(gw, "tool.suspended") == []


def test_br_181_a_failed_state_write_fails_the_listing_closed(
    bed: Bed, served: tuple[TestClient, FakeClock], monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, (client, clock) = bed.gw, served
    up: Upstream = bed.upstream("pn")
    names(client, A, bed.token(A))

    def failing(*_: Any, **__: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(relay, "check_pins", failing)
    look(bed)["description"] = "v2"
    refill(clock)
    assert names(client, A, bed.token(A)) == []  # nothing exposed from that connection
    omitted = [e for e in gw.audit() if e["action"] == "relay.list_omitted"]
    assert [e["path"] for e in omitted] == [PN]
    # a database that refuses the write is an outage of the gateway, never an exposure
    assert [e["reason"] for e in omitted] == ["outage"]
    response = call(client, A, bed.token(A), "pn__other", {"q": "x"})
    assert gateway_error(response) is not None
    assert "other" not in called(up)
    monkeypatch.undo()
    assert names(client, A, bed.token(A)) == ["pn__other"]  # the next fill suspends `look`


def test_br_181_a_baseline_stays_while_the_pin_is_lifted(
    bed: Bed, served: tuple[TestClient, FakeClock]
) -> None:
    gw, (client, clock) = bed.gw, served
    names(client, A, bed.token(A))
    gw.ok("policy", "pin", PN, "--role", A, "off")
    look(bed)["description"] = "v2"
    refill(clock)
    assert names(client, A, bed.token(A)) == ["pn__look", "pn__other"]  # not pinned: served
    assert suspended_at(gw, "look") == [(1, 0)]
    gw.ok("policy", "pin", PN, "--role", A, "inherit")  # `auto`, not sensitive: still off
    gw.ok("policy", "pin", PN, "--role", A, "on")  # the pin returns: compared again
    refill(clock)
    assert names(client, A, bed.token(A)) == ["pn__other"]
    assert suspended_at(gw, "look") == [(1, 1)]


def test_br_181_the_upstream_receives_the_upstream_definition(
    bed: Bed, served: tuple[TestClient, FakeClock]
) -> None:
    """AC-13: an argument rule narrows the announced enum only; the baseline hashes the upstream
    definition, so the narrowing alone suspends nothing."""
    gw, (client, clock) = bed.gw, served
    look(bed)["inputSchema"]["properties"]["q"]["enum"] = ["a", "b"]
    gw.ok("policy", "arg", PN, "--role", A, "look", "q", "a")
    shown = names(client, A, bed.token(A))
    assert shown == ["pn__look", "pn__other"]
    refill(clock)
    assert names(client, A, bed.token(A)) == shown
    assert suspended_at(gw, "look") == [(1, 0)]
    stored = rows(gw, "SELECT definition FROM pinned_definition WHERE upstream_name = 'look'"
                      " AND role_id = ?", gw.node_id(A))  # fmt: skip
    assert '"enum":["a","b"]' in stored[0][0]


def test_br_181_policy_arg_records_the_baseline_at_entry(gw: Gateway) -> None:
    """BR-180 clarification 1 (lot B review, N1): the rule pins the tool from its entry: the
    definition `policy arg` validated on is the baseline, in the transaction that writes the
    rule, so a parameter renamed before the next listing suspends the tool."""
    bed = Bed(gw)
    up = bed.connection("pa", A)
    up.tools = [tool("look", {"type": "object", "properties": {
        "method": {"type": "string", "enum": ["get", "drop"]}}}, READ)]  # fmt: skip
    catalogue(gw, "mpx/pa", A)
    gw.ok("policy", "pin", "mpx/pa", "--role", A, "off")  # only the rule pins
    assert rows(gw, "SELECT count(*) FROM pinned_definition") == [(0,)]
    gw.ok("policy", "arg", "mpx/pa", "--role", A, "look", "method", "get")
    assert rows(
        gw, "SELECT sha256 IS NOT NULL, suspended_at IS NOT NULL FROM pinned_definition"
    ) == [(1, 0)]
    [entry] = audits(gw, "policy.argument_set")
    assert len(entry["detail"]["baseline"]) == 64
    props = up.tools[0]["inputSchema"]["properties"]
    props["action"] = props.pop("method")  # renamed in the window
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == []
        response = call(client, A, bed.token(A), "pa__look", {"method": "get", "action": "drop"})
    assert unknown(response)
    assert called(up) == []
    assert rows(gw, "SELECT suspended_at IS NOT NULL FROM pinned_definition") == [(1,)]


def test_br_181_a_second_policy_arg_keeps_the_first_baseline(gw: Gateway) -> None:
    bed = Bed(gw)
    up = bed.connection("pa", A)
    up.tools = [tool("look", {"type": "object", "properties": {
        "method": {"type": "string"}}}, READ)]  # fmt: skip
    catalogue(gw, "mpx/pa", A)
    gw.ok("policy", "arg", "mpx/pa", "--role", A, "look", "method", "get")
    first = rows(gw, "SELECT sha256 FROM pinned_definition")
    gw.ok("policy", "arg", "mpx/pa", "--role", A, "look", "method", "get", "list")
    assert rows(gw, "SELECT sha256 FROM pinned_definition") == first
    assert "baseline" not in audits(gw, "policy.argument_set")[-1]["detail"]
