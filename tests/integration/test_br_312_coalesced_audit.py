"""BR-312, BR-066 v6, BR-122 v3 (AC-14) — a limit refusal writes one `rate_limited.refused` entry
per surface and key in 60 s: 1 000 refusals in a minute write one entry, the next period's entry
carries the 999 others in `suppressed`; no limited call has a `relay.tool_call` row."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hullgate.limits import Coalescer, Limits
from tests.relay_bed import A, Bed, call, gateway_error, names
from tests.support import Gateway, org, standard_tree

ROLE = "mpx/cairn/staging/dev"


def rs() -> str:
    """RFC 9728 metadata path of ROLE, under the latest gateway's identifier."""
    return f"/.well-known/oauth-protected-resource/o/{org()}/p/{ROLE}/mcp"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _limited(gw: Gateway) -> list[dict[str, Any]]:
    return [e for e in gw.audit() if e["action"] == "rate_limited.refused"]


def test_br_312_coalescer_counts_in_memory() -> None:
    clock = Clock()
    c = Coalescer(60.0, clock)
    assert c.refused("oauth", "k") == 0  # the first refusal of a period is written
    for _ in range(999):
        assert c.refused("oauth", "k") is None
    assert c.refused("credential", "k") == 0  # another surface: its own period
    clock.now += 60
    assert c.refused("oauth", "k") == 999


def test_br_312_one_thousand_oauth_refusals_write_one_entry(gw: Gateway) -> None:
    standard_tree(gw)
    clock = Clock()
    with gw.client(limits=Limits(clock=clock), peer="192.0.2.5") as client:
        for _ in range(60):
            client.get(rs())
        for _ in range(1000):
            assert client.get(rs()).status_code == 429
        assert len(_limited(gw)) == 1
        clock.now += 60.5  # next period: the budget is back, spent again, then refused
        for _ in range(60):
            assert client.get(rs()).status_code == 200
        assert client.get(rs()).status_code == 429
    entries = _limited(gw)
    assert len(entries) == 2
    first, second = (json.loads(e["detail"]) for e in entries)
    assert first == {"surface": "oauth", "key": "192.0.2.5", "suppressed": 0,
                     "address": "192.0.2.5"}  # fmt: skip
    assert second["suppressed"] == 999
    assert entries[0]["result"] == "refused"
    assert entries[0]["reason"] == "rate_limited"
    assert entries[0]["actor"] == "anonymous"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_312_limited_tool_calls_have_no_tool_call_row(bed: Bed) -> None:
    gw = bed.gw
    bed.connection("ca", A)
    gw.ok("settings", "set", A, "token_rate_limit", "2/min")
    token_id, token = gw.issue(A, "looper")
    relay = gw.relay(limits=Limits(clock=Clock()))
    with bed.serve(relay) as client:
        names(client, A, token)
        for _ in range(2):
            assert gateway_error(call(client, A, token, "ca__read")) is None
        for _ in range(50):
            assert gateway_error(call(client, A, token, "ca__read")) == "rate_limited"
    calls = [e for e in gw.audit() if e["action"] == "relay.tool_call" and e["actor"] == token_id]
    assert len(calls) == 2
    entries = _limited(gw)
    assert len(entries) == 1
    detail = json.loads(entries[0]["detail"])
    assert detail["surface"] == "token"
    assert detail["key"] == f"operator:{token_id}"
    assert entries[0]["actor"] == token_id
    assert entries[0]["path"] == A
    assert "address" in detail  # BR-313: the request that opened the period


def test_br_312_credentials_beyond_budget_coalesce(gw: Gateway) -> None:
    standard_tree(gw)
    from tests.support import mcp

    with gw.client(limits=Limits(clock=Clock())) as client:
        for _ in range(130):
            assert mcp(client, ROLE, "hg_bad").status_code == 401
    entries = _limited(gw)
    assert len(entries) == 1
    assert json.loads(entries[0]["detail"])["surface"] == "credential"
