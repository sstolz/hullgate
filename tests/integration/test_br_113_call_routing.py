"""BR-113 (F-008 lot B, AC-14, AC-15): `tools/call` finds the connection through the role's
table and checks again before any upstream call, in two stages. Stage 1 (database only):
0 `get_access`, 0 exchange. Stage 2: 0 `tools/call`, at most one `get_access` and, with a cold
cache only, one `tools/list`. Played with a cold cache (new relay, name table in the database) and
with a warm cache; proof by absence (BR-002)."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import pytest

from hullgate.policy import ALLOW, HIDE, Decision
from hullgate.relay import Relay
from tests.fake_upstream import Fault, Upstream, tool
from tests.relay_bed import A, B, Bed, call, names, unknown
from tests.support import Gateway


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


class HideWrite:
    def decide(
        self,
        role_id: str,
        connection_id: str,
        upstream_name: str,
        definition: Mapping[str, Any],
        arguments: Mapping[str, Any] | None,
    ) -> Decision:
        return HIDE if upstream_name == "write" else ALLOW


def _counts(up: Upstream) -> tuple[int, int]:
    return len(up.calls("tools/list")), len(up.calls("tools/call"))


def _warm_or_cold(bed: Bed, warm: bool, policy: Any = None) -> Relay:
    """The test's relay: when warm, the list of A has already been served by it."""
    relay = bed.gw.relay(policy=policy)
    if warm:
        with bed.serve(relay) as client:
            names(client, A, bed.token(A))
    return relay


def _set_state(gw: Gateway, prefix: str, state: str) -> None:
    conn = gw.connect()
    conn.execute("UPDATE connection SET state = ? WHERE prefix = ?", (state, prefix))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_br_113_stage_one_refusals_cost_nothing(
    bed: Bed, monkeypatch: pytest.MonkeyPatch, warm: bool
) -> None:
    """Five stage 1 refusals, in order: unknown name, well-formed name of a connection not
    placed, tool of B called on A, raw upstream name, connection not active; when warm, the list
    of A has been served by the same relay before the refusals. (A sensitive connection reaches
    stage 2 since F-015: test_br_175_*.)"""
    gw = bed.gw
    ca = bed.connection("ca", A)
    cb = bed.connection("cb", B)
    bed.connection("cn")
    # name table filled for ca (A) and cb (B), by a previous relay
    with bed.serve() as client:
        names(client, A, bed.token(A))
        names(client, B, bed.token(B))
    relay = _warm_or_cold(bed, warm)
    accesses = bed.spy_access(monkeypatch)
    before = {p: _counts(u) for p, u in (("ca", ca), ("cb", cb))}
    with bed.serve(relay) as client:
        for case, name in (
            ("unknown", "zz__unknown"),
            ("not-placed", "cn__read"),
            ("other-role", "cb__read"),
            ("raw-name", "read"),
            ("not-active", "ca__read"),
        ):
            if case == "not-active":
                _set_state(gw, "ca", "reauth_required")
            response = call(client, A, bed.token(A), name)
            assert unknown(response), (case, response.text)
            assert accesses == [], case
            assert {p: _counts(u) for p, u in (("ca", ca), ("cb", cb))} == before, case
    assert bed.upstream("cn").calls() == []


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize("case", ["absent-from-list", "masked"])
def test_br_113_stage_two_refusals_never_call(
    bed: Bed, monkeypatch: pytest.MonkeyPatch, warm: bool, case: str
) -> None:
    up = bed.connection("ca", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))  # `ca__write` recorded in the table
    if case == "absent-from-list":
        up.tools = [tool("read")]
    relay = _warm_or_cold(bed, warm, HideWrite() if case == "masked" else None)
    lists, calls = _counts(up)
    accesses = bed.spy_access(monkeypatch)
    with bed.serve(relay) as client:
        response = call(client, A, bed.token(A), "ca__write")
    assert unknown(response), response.text
    after_lists, after_calls = _counts(up)
    assert after_calls == calls == 0
    assert len(accesses) <= 1
    assert after_lists - lists == (0 if warm else 1)


def test_br_113_valid_call_reaches_the_upstream_once(bed: Bed) -> None:
    up = bed.connection("ca", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))  # the public name is born at announcement (BR-110)
        response = call(client, A, bed.token(A), "ca__read")
    assert response.json()["result"]["isError"] is False
    assert len(up.calls("tools/call")) == 1


@pytest.mark.parametrize("action", ["unplace", "revoke"])
def test_br_113_change_during_a_call(bed: Bed, action: str) -> None:
    """AC-15: the change written during a call in flight does not interrupt it (it completes);
    the next call is refused with no exchange."""
    gw = bed.gw
    up = bed.connection("ca", A)
    with bed.serve() as client:
        token = bed.token(A)
        names(client, A, token)
        up.faults.append(Fault("delay", on="tools/call", seconds=1.0))
        got: list[Any] = []
        flight = threading.Thread(target=lambda: got.append(call(client, A, token, "ca__read")))
        flight.start()
        while not up.calls("tools/call"):
            threading.Event().wait(0.02)
        if action == "unplace":
            gw.ok("connection", "unplace", "mpx/ca", A)
        else:
            gw.ok("connection", "revoke", "mpx/ca")
        flight.join(10)
        assert got
        assert got[0].json()["result"]["isError"] is False
        calls = len(up.calls("tools/call"))
        after = call(client, A, token, "ca__read")
    assert unknown(after)
    assert len(up.calls("tools/call")) == calls == 1
