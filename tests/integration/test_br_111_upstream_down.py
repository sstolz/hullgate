"""BR-111 (F-008 lot B, AC-12): an upstream outage does not erase the tools of the role's other
connections; the omission is audited with its reason; a stale cache is never served as a fallback;
a role's connections are listed in parallel under a global deadline."""

from __future__ import annotations

import time

import pytest

from hullgate import relay as relay_mod
from tests.fake_upstream import FakeClock, Fault
from tests.relay_bed import A, Bed, call, gateway_error, names
from tests.support import Gateway


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def _omitted(bed: Bed) -> list[str]:
    return [a["reason"] for a in bed.gw.audit() if a["action"] == "relay.list_omitted"]


def test_br_111_one_upstream_down_keeps_the_others(bed: Bed) -> None:
    clock = FakeClock()
    down = bed.connection("dn", A)
    bed.connection("ok", A)
    with bed.serve(bed.gw.relay(upclock=clock)) as client:
        token = bed.token(A)
        assert names(client, A, token) == ["dn__read", "dn__write", "ok__read", "ok__write"]
        clock.advance(301)
        down.faults.append(Fault("status", status=500, on="tools/list"))
        assert names(client, A, token) == ["ok__read", "ok__write"]
    entry = [a for a in bed.gw.audit() if a["action"] == "relay.list_omitted"]
    assert [(a["reason"], a["path"]) for a in entry] == [("outage", "mpx/dn")]


def test_br_111_stale_cache_is_never_a_fallback(bed: Bed) -> None:
    clock = FakeClock()
    down = bed.connection("dn", A)
    relay = bed.gw.relay(upclock=clock)
    with bed.serve(relay) as client:
        token = bed.token(A)
        assert names(client, A, token) == ["dn__read", "dn__write"]
        lists = len(down.calls("tools/list"))
        clock.advance(299)
        assert names(client, A, token) == ["dn__read", "dn__write"]  # < 300 s: without the upstream
        assert len(down.calls("tools/list")) == lists
        clock.advance(2)
        down.faults.append(Fault("status", status=503, on="tools/list"))
        assert names(client, A, token) == []  # > 300 s and upstream down: omitted
        response = call(client, A, token, "dn__read")
    assert _omitted(bed) == ["outage"]
    # the list is cold and the upstream answers again: the call goes through a fresh list
    assert gateway_error(response) is None


def test_br_111_known_tool_of_a_down_upstream_is_unavailable(bed: Bed) -> None:
    clock = FakeClock()
    down = bed.connection("dn", A)
    relay = bed.gw.relay(upclock=clock)
    with bed.serve(relay) as client:
        token = bed.token(A)
        names(client, A, token)
        clock.advance(301)
        down.faults.append(Fault("status", status=500, on="tools/list"))
        response = call(client, A, token, "dn__read")
    assert gateway_error(response) == "upstream_unavailable"
    assert down.calls("tools/call") == []


def test_br_111_connections_are_listed_in_parallel(bed: Bed) -> None:
    ups = [bed.connection(p, A) for p in ("sa", "sb", "sc")]
    for up in ups:
        up.faults.append(Fault("delay", on="tools/list", seconds=1.5))
    with bed.serve() as client:
        started = time.monotonic()
        listed = names(client, A, bed.token(A))
        elapsed = time.monotonic() - started
    assert len(listed) == 6
    assert elapsed < 3.0, elapsed  # in sequence: ≥ 4.5 s


def test_br_111_global_deadline_omits_the_late_connection(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global deadline of the list (30 s, H21) played at 1 s: an upstream that exceeds it is
    omitted, reason "deadline", the others are returned."""
    monkeypatch.setattr(relay_mod, "LIST_DEADLINE_S", 1.0)
    bed.connection("ok", A)
    late = bed.connection("lt", A)
    late.faults.append(Fault("delay", on="tools/list", seconds=3.0))
    with bed.serve() as client:
        started = time.monotonic()
        assert names(client, A, bed.token(A)) == ["ok__read", "ok__write"]
        assert time.monotonic() - started < 2.5
    assert _omitted(bed) == ["deadline"]
    assert relay_mod.LIST_DEADLINE_S == 1.0  # the real value is read below
    monkeypatch.undo()
    assert relay_mod.LIST_DEADLINE_S == 30.0
