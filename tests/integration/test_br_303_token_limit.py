"""BR-303 (AC-04) — a token's tool calls are limited per minute, counted per token across its
roles, before any other check of the relay: 0 `get_access`, 0 upstream exchange (BR-002)."""

from __future__ import annotations

import re

import pytest

from hullgate.limits import Limits
from tests.relay_bed import A, B, Bed, call, gateway_error, names, tool_result
from tests.support import Gateway, grant, refresh


class Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def _delay(text: str) -> int:
    match = re.fullmatch(r"rate_limited \(retry in (\d+) s\)", text)
    assert match is not None, text
    return int(match.group(1))


def test_br_303_sixth_call_is_refused_before_anything(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = bed.gw
    up = bed.connection("ca", A)
    gw.ok("settings", "set", A, "token_rate_limit", "5/min")
    clock = Clock()
    limits = Limits(clock=clock)
    relay = gw.relay(limits=limits)
    token = bed.token(A)
    with bed.serve(relay) as client:
        names(client, A, token)
        for _ in range(5):
            assert gateway_error(call(client, A, token, "ca__read")) is None
            clock.now += 1
        accesses = bed.spy_access(monkeypatch)
        before = len(up.calls())
        refused = call(client, A, token, "ca__read")
        assert refused.status_code == 200
        result = tool_result(refused)
        assert result["isError"] is True
        delay = _delay(result["content"][0]["text"])
        assert delay == 55  # the oldest of the five, 5 s ago, leaves in 55 s
        assert accesses == []
        assert len(up.calls()) == before
        # an unknown name gets the same answer: the check reveals nothing (§3.3)
        assert gateway_error(call(client, A, token, "zz__unknown")) == "rate_limited"
        clock.now += delay
        assert gateway_error(call(client, A, token, "ca__read")) is None


def test_br_303_count_above_a_lowered_value(bed: Bed) -> None:
    gw = bed.gw
    bed.connection("ca", A)
    clock = Clock()
    relay = gw.relay(limits=Limits(clock=clock))
    token = bed.token(A)
    with bed.serve(relay) as client:
        names(client, A, token)
        for _ in range(6):
            assert gateway_error(call(client, A, token, "ca__read")) is None
            clock.now += 5
        gw.ok("settings", "set", A, "token_rate_limit", "3/min")  # applies at the next call
        text = tool_result(call(client, A, token, "ca__read"))["content"][0]["text"]
        delay = _delay(text)
        assert delay == 45  # the 4th oldest (t+15) leaves at t+75; now t+30
        clock.now += delay
        assert gateway_error(call(client, A, token, "ca__read")) is None


def test_br_303_token_on_two_roles_has_one_count(bed: Bed) -> None:
    gw = bed.gw
    bed.connection("ca", A, B, confirm=True)
    gw.ok("settings", "set", "mpx", "token_rate_limit", "4/min")
    clock = Clock()
    relay = gw.relay(limits=Limits(clock=clock))
    _, token = gw.issue("mpx/cairn/staging")  # covers A and B
    with bed.serve(relay) as client:
        names(client, A, token)
        names(client, B, token)
        for role in (A, B, A, B):
            assert gateway_error(call(client, role, token, "ca__read")) is None
        assert gateway_error(call(client, A, token, "ca__read")) == "rate_limited"
        assert gateway_error(call(client, B, token, "ca__read")) == "rate_limited"
        # another token keeps its own count
        assert gateway_error(call(client, A, bed.token(A), "ca__read")) is None


def test_br_303_value_is_read_at_the_role_called(bed: Bed) -> None:
    gw = bed.gw
    bed.connection("ca", A, B, confirm=True)
    gw.ok("settings", "set", A, "token_rate_limit", "2/min")
    gw.ok("settings", "set", B, "token_rate_limit", "10/min")
    relay = gw.relay(limits=Limits(clock=Clock()))
    _, token = gw.issue("mpx/cairn/staging")
    with bed.serve(relay) as client:
        names(client, A, token)
        names(client, B, token)
        for _ in range(3):
            assert gateway_error(call(client, B, token, "ca__read")) is None
        assert gateway_error(call(client, A, token, "ca__read")) == "rate_limited"
        assert gateway_error(call(client, B, token, "ca__read")) is None


def test_br_303_oauth_refresh_does_not_reset_the_count(bed: Bed) -> None:
    gw = bed.gw
    bed.connection("ca", A)
    gw.preregister("mpx")
    gw.ok("settings", "set", A, "token_rate_limit", "3/min")
    relay = gw.relay(limits=Limits(clock=Clock()))
    with bed.serve(relay) as client:
        body = grant(gw, client, A)
        access = body["access_token"]
        names(client, A, access)
        for _ in range(3):
            assert gateway_error(call(client, A, access, "ca__read")) is None
        renewed = refresh(client, A, body["refresh_token"])
        assert renewed.status_code == 200, renewed.text
        fresh = renewed.json()["access_token"]
        assert gateway_error(call(client, A, fresh, "ca__read")) == "rate_limited"
        # a new authorisation is a new lineage: its own count
        other = grant(gw, client, A)["access_token"]
        assert gateway_error(call(client, A, other, "ca__read")) is None


def test_br_303_calls_refused_later_in_the_chain_count(bed: Bed) -> None:
    gw = bed.gw
    bed.connection("ca", A)
    gw.ok("settings", "set", A, "token_rate_limit", "2/min")
    relay = gw.relay(limits=Limits(clock=Clock()))
    token = bed.token(A)
    with bed.serve(relay) as client:
        names(client, A, token)
        call(client, A, token, "zz__unknown")  # admitted here, refused at stage 1
        call(client, A, token, "zz__unknown")
        assert gateway_error(call(client, A, token, "ca__read")) == "rate_limited"
