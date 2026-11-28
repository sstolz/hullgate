"""BR-127 (F-008 lot B, AC-17): an upstream 401 before any content calls `signal_refusal` with
the generation of the `Access` used; an OAuth connection is retried once with the token that
`get_access` then returns; a second 401, or any 401 of a provided-token connection, returns
`connection_reauth_required` without a new attempt; the provided token moves to reauthentication
required, `last_reason` `mcp_refusal_401`."""

from __future__ import annotations

from typing import Any

import pytest

from hullgate import lifecycle
from tests.fake_upstream import Fault
from tests.relay_bed import A, Bed, call, gateway_error, names
from tests.support import Gateway


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, str]]:
    seen: list[tuple[str, int, str]] = []
    original = lifecycle.signal

    def spy(run: Any, key: Any, connection_id: str, generation: int, cause: str, **kw: Any) -> str:
        seen.append((connection_id, generation, cause))
        return original(run, key, connection_id, generation, cause, **kw)

    monkeypatch.setattr(lifecycle, "signal", spy)
    return seen


def test_br_127_oauth_401_is_signalled_then_retried_once(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = bed.gw
    up = bed.connection("oa", A, oauth=True)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        generation = int(gw.row("mpx/oa")["generation"])
        signals = _spy(monkeypatch)
        up.faults.append(Fault("status", status=401, on="tools/call"))
        response = call(client, A, bed.token(A), "oa__read")
    assert response.json()["result"]["isError"] is False
    assert len(up.calls("tools/call")) == 2
    assert signals == [(gw.connection_id("mpx/oa"), generation, lifecycle.MCP_401)]
    assert up.executions == {"read": 1}


def test_br_127_second_401_stops_without_a_third_request(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    up = bed.connection("oa", A, oauth=True)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        signals = _spy(monkeypatch)
        up.faults += [Fault("status", status=401, on="tools/call") for _ in range(3)]
        response = call(client, A, bed.token(A), "oa__read")
    assert gateway_error(response) == "connection_reauth_required"
    assert len(up.calls("tools/call")) == 2
    assert len(signals) == 1


def test_br_127_static_token_401_disables_without_retry(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = bed.gw
    up = bed.connection("st", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        signals = _spy(monkeypatch)
        up.faults.append(Fault("status", status=401, on="tools/call"))
        response = call(client, A, bed.token(A), "st__read")
    assert gateway_error(response) == "connection_reauth_required"
    assert len(up.calls("tools/call")) == 1
    assert len(signals) == 1
    row = gw.row("mpx/st")
    assert (row["state"], row["last_reason"]) == (
        "reauth_required",
        "mcp_refusal_401",
    )
    assert "mcp_refusal_401" in gw.ok("connection", "status", "mpx/st")
