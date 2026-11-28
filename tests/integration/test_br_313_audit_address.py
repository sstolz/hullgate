"""BR-313 (AC-15) — every refusal entry of the public listener carries the full client address in
`detail.address`: `access.refused`, OAuth route refusals, barrier failures and closure,
`rate_limited.refused`; `relay.tool_call` and an issuance do not."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.integration.test_br_060_consent import _pending
from tests.integration.test_br_060_passkey import enrolled, post, public
from tests.policy_support import rows
from tests.relay_bed import A, Bed, call, names
from tests.support import (
    Gateway,
    authorize_params,
    consent,
    grant,
    mcp,
    pkce,
    request_id,
    start_authorization,
)

PEER = "2001:db8:5:6::7"


def _address(entry: dict[str, Any]) -> str | None:
    detail = json.loads(entry["detail"]) if entry["detail"] else {}
    value = detail.get("address")
    return value if isinstance(value, str) else None


@pytest.mark.parametrize("case", ["failure", "limited-options", "limited-assertion", "ceiling"])
def test_br_313_passkey_failures_and_closures_keep_full_ipv6(gw: Gateway, case: str) -> None:
    console, _ = enrolled(gw)
    other_peer = "2001:db8:5:9::8"  # same /48: shared limits, distinct audit attribution
    with public(gw, console.origin, peer=PEER) as client:
        req = _pending(gw, client)
        if case == "failure":
            assert post(client, req, "passkey", {}).status_code == 200
        else:
            decision = "passkey-options" if case == "ceiling" else "passkey"
            for _ in range(5):
                assert post(client, req, decision, {}).status_code == 200
            with public(gw, console.origin, peer=other_peer) as other:
                decision = "passkey" if case == "limited-assertion" else "passkey-options"
                assert post(other, req, decision, {}).status_code == 429
    events = [e for e in gw.audit() if e["action"] == "oauth.consent"]
    assert events
    assert all(e["result"] == "refused" for e in events)
    assert events[-1]["reason"] == ("unknown_challenge" if case == "failure" else "passkey_limited")
    assert _address(events[-1]) == (PEER if case == "failure" else other_peer)
    assert all(_address(e) == PEER for e in events[:-1])
    query = (
        "SELECT DISTINCT address FROM webauthn_challenge"
        if case == "ceiling"
        else "SELECT DISTINCT address FROM admin_attempt"
    )
    assert rows(gw, query) == [("2001:db8:5::/48",)]
    assert rows(gw, "SELECT count(*) FROM auth_code") == [(0,)]


def test_br_313_refusals_carry_the_address_other_entries_do_not(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ca", A)
    gw.preregister("mpx")
    with gw.client(peer=PEER) as client:
        mcp(client, A, "hg_bad")  # access.refused
        _, challenge = pkce()
        start_authorization(client, A, authorize_params(A, challenge, "nobody"))  # oauth refusal
        params = authorize_params(A, pkce()[1])
        req = request_id(start_authorization(client, A, params))
        for _ in range(5):
            consent(gw, client, A, req, code="999999")  # barrier failures
        consent(gw, client, A, req)  # barrier closed
    for entry in gw.audit():
        refused = entry["result"] == "refused" and (
            entry["action"].startswith(("access.", "oauth."))
            or entry["action"] == "rate_limited.refused"
        )
        if refused:
            assert _address(entry) == PEER, entry
        else:
            assert _address(entry) is None, entry
    reasons = {e["reason"] for e in gw.audit() if _address(e) == PEER}
    assert {"invalid_token", "unknown_client", "consent_failed", "consent_locked"} <= reasons


def test_br_313_tool_calls_and_issuances_carry_no_address(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ca", A)
    gw.preregister("mpx")
    with gw.client(peer=PEER) as client:
        names(client, A, bed.token(A))
        call(client, A, bed.token(A), "ca__read")
        grant(gw, client, A)
    issued = [e for e in gw.audit() if e["action"] in ("relay.tool_call", "oauth.code.issue",
                                                       "oauth.token.issue")]  # fmt: skip
    assert issued
    assert all(_address(e) is None for e in issued)
