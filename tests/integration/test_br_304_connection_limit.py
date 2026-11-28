"""BR-304 (AC-05) — a connection's tool calls are limited per minute, counted per connection
across roles and tokens, after the policy and before the upstream `tools/call`; a name hidden by
policy stays "unknown tool" while the connection is over its limit (BR-113)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from hullgate.limits import Limits
from hullgate.policy import ALLOW, HIDE, Decision
from tests.fake_upstream import Fault
from tests.relay_bed import A, B, Bed, call, gateway_error, names, unknown
from tests.support import Gateway


class Clock:
    def __init__(self) -> None:
        self.now = 9000.0

    def __call__(self) -> float:
        return self.now


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


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_304_two_tokens_on_one_connection_share_the_count(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("ca", A, B, confirm=True)
    gw.ok("settings", "set", "mpx", "connection_rate_limit", "3/min")
    relay = gw.relay(limits=Limits(clock=Clock()))
    ta, tb = bed.token(A), bed.token(B)
    with bed.serve(relay) as client:
        names(client, A, ta)
        names(client, B, tb)
        for role, token in ((A, ta), (B, tb), (A, ta)):
            assert gateway_error(call(client, role, token, "ca__read")) is None
        sent = len(up.calls("tools/call"))
        refused = call(client, B, tb, "ca__read")
        assert gateway_error(refused) == "rate_limited"
        assert "retry in" in refused.json()["result"]["content"][0]["text"]
        assert len(up.calls("tools/call")) == sent  # no upstream `tools/call`


def test_br_304_hidden_name_stays_unknown_over_the_limit(bed: Bed) -> None:
    gw = bed.gw
    bed.connection("ca", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))  # `ca__write` recorded in the table
    gw.ok("settings", "set", A, "connection_rate_limit", "1/min")
    relay = gw.relay(policy=HideWrite(), limits=Limits(clock=Clock()))
    token = bed.token(A)
    with bed.serve(relay) as client:
        assert gateway_error(call(client, A, token, "ca__read")) is None
        assert gateway_error(call(client, A, token, "ca__read")) == "rate_limited"
        assert unknown(call(client, A, token, "ca__write"))  # as an unknown name answers
        assert unknown(call(client, A, token, "ca__absent"))


def test_br_304_a_401_retry_counts_once(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("oa", A, oauth=True)
    gw.ok("settings", "set", A, "connection_rate_limit", "2/min")
    relay = gw.relay(limits=Limits(clock=Clock()))
    token = bed.token(A)
    with bed.serve(relay) as client:
        names(client, A, token)
        up.faults.append(Fault("status", status=401, on="tools/call"))
        assert gateway_error(call(client, A, token, "oa__read")) is None
        assert len(up.calls("tools/call")) == 2  # retried after the 401
        assert gateway_error(call(client, A, token, "oa__read")) is None  # second of two
        assert gateway_error(call(client, A, token, "oa__read")) == "rate_limited"


def test_br_304_independent_of_the_token_limit(bed: Bed) -> None:
    """A token under its limit is still refused when the connection is over its own."""
    gw = bed.gw
    bed.connection("ca", A)
    gw.ok("settings", "set", A, "connection_rate_limit", "2/min")
    gw.ok("settings", "set", A, "token_rate_limit", "100/min")
    relay = gw.relay(limits=Limits(clock=Clock()))
    first, second = gw.issue(A)[1], gw.issue(A, "other")[1]
    with bed.serve(relay) as client:
        names(client, A, first)
        assert gateway_error(call(client, A, first, "ca__read")) is None
        assert gateway_error(call(client, A, first, "ca__read")) is None
        assert gateway_error(call(client, A, second, "ca__read")) == "rate_limited"
