"""BR-116 (F-008 lot B, AC-19): the gateway's failures are returned as an `isError` result from
a closed list of nine codes (F-020, BR-116 v3: `rate_limited` with its delay), with no token,
URL or upstream body; a policy refusal is never one of these codes (it is "unknown tool"); 403,
404 outside a session, 500 and 302 from the upstream return `upstream_unavailable`."""

from __future__ import annotations

from typing import Any

import pytest

from hullgate import limits, upstream_mcp
from hullgate.relay import Relay
from tests.fake_upstream import FakeClock, Fault, Upstream
from tests.relay_bed import A, Bed, call, gateway_error, names
from tests.support import Gateway

TOKEN = "tok-TOKEN-MARKER-" + "x" * 20
MARKERS = ("TOKEN-MARKER", "mk.mcp.test", "UPSTREAM-SECRET-MESSAGE", "boom", "SECRET-DESCRIPTION")
CODES = {
    "upstream_unavailable", "upstream_busy", "deadline_exceeded", "uncertain_outcome",
    "response_too_large", "request_too_large", "connection_reauth_required", "internal_error",
    "rate_limited",
}  # fmt: skip


def test_br_116_closed_list_is_the_pinned_set() -> None:
    """AC-20: the eight codes of `upstream_mcp.py` and F-020's `rate_limited` are pinned English
    literals (the instruction `(retry in N s)` is checked by `test_br_108`, `test_br_114` and
    `test_br_303`)."""
    code = upstream_mcp
    assert {
        code.UNAVAILABLE, code.BUSY, code.DEADLINE, code.UNCERTAIN, code.TOO_LARGE,
        code.REQUEST_TOO_LARGE, code.REAUTHORIZE, code.INTERNAL, limits.RATE_LIMITED,
    } == CODES  # fmt: skip
    assert len(CODES) == 9


def _provoke(up: Upstream, relay: Relay, clock: FakeClock, code: str) -> dict[str, Any] | None:
    """Prepares the fault that gives `code`; returns particular arguments, otherwise None."""
    if code == "upstream_unavailable":
        up.faults.append(Fault("rpc_error", on="tools/call"))
    elif code == "upstream_busy":
        up.faults.append(Fault("status", status=429, on="tools/call",
                               headers={"Retry-After": "5"}))  # fmt: skip
    elif code == "deadline_exceeded":
        up.faults.append(Fault("hang", on="tools/call"))
        clock.expect_hang(lambda: bool(up.calls("tools/call")))
    elif code == "uncertain_outcome":
        up.faults.append(Fault("cut", on="tools/call"))
    elif code == "response_too_large":
        up.faults.append(Fault("big", on="tools/call"))
    elif code == "request_too_large":
        return {"x": "a" * (1024 * 1024)}
    elif code == "rate_limited":  # F-020: the token's window refuses (BR-303)
        relay.limits.tokens.admit = lambda *_a, **_k: 30.0  # type: ignore[method-assign]
    elif code == "connection_reauth_required":
        up.faults.append(Fault("status", status=401, on="tools/call",
                               headers={"WWW-Authenticate": 'Bearer error_description="'
                                        'SECRET-DESCRIPTION"'}))  # fmt: skip
    else:

        def boom(*_: Any, **__: Any) -> Any:
            raise RuntimeError("UPSTREAM-SECRET-MESSAGE mk.mcp.test")

        relay.client.call_tool = boom  # type: ignore[method-assign]
    return None


@pytest.mark.parametrize("code", sorted(CODES))
def test_br_116_gateway_failures_are_closed_and_clean(gw: Gateway, code: str) -> None:
    bed = Bed(gw)
    up = bed.connection("mk", A, value=TOKEN)
    clock = FakeClock()
    relay = gw.relay(upclock=clock)
    with bed.serve(relay) as client:
        names(client, A, bed.token(A))
        args = _provoke(up, relay, clock, code)
        response = call(client, A, bed.token(A), "mk__read", args)
    assert gateway_error(response) == code, response.text[:300]
    text = response.text
    for marker in MARKERS:
        assert marker not in text, (code, marker)
    assert "politique" not in text
    assert {k.lower() for k in response.headers} & {"set-cookie", "www-authenticate"} == set()


@pytest.mark.parametrize("status", [403, 404, 500, 302])
def test_br_116_upstream_statuses_are_unavailable(gw: Gateway, status: int) -> None:
    bed = Bed(gw)
    up = bed.connection("mk", A, value=TOKEN)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        kind = "redirect" if status == 302 else "status"
        up.faults.append(Fault(kind, status=status, on="tools/call"))
        response = call(client, A, bed.token(A), "mk__read")
    assert gateway_error(response) == "upstream_unavailable"
    assert not any(m in response.text for m in MARKERS)
