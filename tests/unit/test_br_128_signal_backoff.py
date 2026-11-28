"""BR-128 (F-008 lot B, AC-32): a second 401 on a fresh token places for the connection a
signalling "not before" date (60 s, doubled at each episode up to 15 min, reset by a success);
before it, a 401 returns `connection_reauth_required` without `signal_refusal`, without renewal
or retry; the date holds for every role and never disables the connection. Injected clock."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hullgate import lifecycle
from tests.fake_upstream import FakeClock
from tests.relay_bed import A, B, Bed, call, gateway_error, names
from tests.support import Gateway


def _pauses(bed: Bed) -> list[int]:
    return [
        json.loads(a["detail"])["pause_s"]
        for a in bed.gw.audit()
        if a["action"] == "relay.signal_pause"
    ]


def test_br_128_signal_pause_cycle(gw: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    bed = Bed(gw)
    up = bed.connection("bo", A, B, oauth=True)
    clock = FakeClock()
    signals: list[int] = []
    original = lifecycle.signal

    def spy(*args: Any, **kwargs: Any) -> str:
        signals.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "signal", spy)
    with bed.serve(gw.relay(upclock=clock)) as client:
        ta, tb = bed.token(A), bed.token(B)
        names(client, A, ta)
        names(client, B, tb)
        up.reject = True
        # episode 1: signal, retry, second 401 → date of 60 s
        assert gateway_error(call(client, A, ta, "bo__read")) == "connection_reauth_required"
        assert (len(signals), len(up.calls("tools/call"))) == (1, 2)
        assert _pauses(bed) == [60]
        token = gw.tokens("mpx/bo")
        # before the deadline, on both roles: 0 signal, 0 renewal, 0 retry
        for role, tok in ((A, ta), (B, tb), (A, ta)):
            calls = len(up.calls("tools/call"))
            assert (
                gateway_error(call(client, role, tok, "bo__read")) == "connection_reauth_required"
            )
            assert len(up.calls("tools/call")) == calls + 1
        assert len(signals) == 1
        assert gw.tokens("mpx/bo") == token
        # at the deadline, a new cycle: the duration doubles
        clock.advance(61)
        call(client, A, ta, "bo__read")
        assert len(signals) == 2
        assert _pauses(bed) == [60, 120]
        clock.advance(121)
        call(client, A, ta, "bo__read")
        assert _pauses(bed) == [60, 120, 240]
        # a success resets to 60 s
        clock.advance(241)
        up.reject = False
        assert gateway_error(call(client, A, ta, "bo__read")) is None
        up.reject = True
        call(client, A, ta, "bo__read")
        assert _pauses(bed)[-1] == 60
    assert gw.row("mpx/bo")["state"] == "active"


def test_br_128_pause_is_capped_at_fifteen_minutes(gw: Gateway) -> None:
    bed = Bed(gw)
    up = bed.connection("bo", A, oauth=True)
    clock = FakeClock()
    with bed.serve(gw.relay(upclock=clock)) as client:
        ta = bed.token(A)
        names(client, A, ta)
        up.reject = True
        for _ in range(7):
            call(client, A, ta, "bo__read")
            clock.advance(901)
    assert _pauses(bed) == [60, 120, 240, 480, 900, 900, 900]
    entry = next(a for a in gw.audit() if a["action"] == "relay.signal_pause")
    assert entry["reason"] == "second 401 on a fresh token"
    for value in (gw.tokens("mpx/bo") or {}).values():
        assert str(value) not in json.dumps(gw.audit())
