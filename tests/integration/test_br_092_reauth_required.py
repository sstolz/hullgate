"""BR-092 — a definitive refusal from the provider puts the connection in "reauthentication
required" (AC-21), closed table of replies; the body and `error_description` are never repeated."""

from __future__ import annotations

import json

import pytest

from hullgate import connections
from tests import fake_provider
from tests.fake_provider import Reply
from tests.support import Gateway, auth_url, standard_tree

ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"
TRAP = "PIEGE-error-description-0123456789"


@pytest.mark.parametrize(
    "reply",
    [
        *(
            Reply(status=400, body={"error": code, "error_description": TRAP})
            for code in (
                "invalid_grant",
                "invalid_scope",
                "consent_required",
                "interaction_required",
                "login_required",
                "invalid_client",
                "unauthorized_client",
            )
        ),
        Reply(normal=True, body={"expires_in": 30, "error_description": TRAP}),
    ],
    ids=lambda r: (r.body or {}).get("error", "expires_in_30"),
)
def test_br_092_definitive_outcome(gw: Gateway, reply: Reply) -> None:
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    gw.expire(NAME, 30)
    provider.script.append(reply)
    with pytest.raises(connections.ReauthRequired):
        gw.access(NAME, ROLE)
    calls = len(provider.journal)
    with pytest.raises(connections.ReauthRequired):
        gw.access(NAME, ROLE)
    assert len(provider.journal) == calls  # no more calls to the provider
    row = gw.row(NAME)
    assert (row["state"], row["tokens_enc"]) == ("reauth_required", None)
    code = (reply.body or {}).get("error", "expiry_too_short")
    entry = [e for e in gw.audit() if e["action"] == "connection.renew"][-1]
    assert (entry["result"], entry["reason"]) == ("permanent", code)
    [view] = json.loads(gw.ok("connection", "status", NAME, "--json"))
    text = gw.ok("connection", "status", NAME)
    if code in ("invalid_client", "unauthorized_client"):
        assert view["application_to_redeclare"] == "app"
        assert "application app to redeclare or reregister" in text
    else:
        assert view["application_to_redeclare"] is None
    assert TRAP not in json.dumps(gw.audit())
    assert TRAP not in text
    # reauthorize then authorize: active
    url = auth_url(gw.ok("connection", "reauthorize", NAME))
    assert gw.row(NAME)["state"] == "pending"
    gw.ok("connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url))
    assert gw.row(NAME)["state"] == "active"
    assert gw.access(NAME, ROLE).authorization.startswith("Bearer at-")


def test_br_092_definitive_at_exchange_writes_nothing(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.provider()
    gw.service()
    gw.application()
    url = gw.open_oauth()
    provider.script.append(
        Reply(status=400, body={"error": "invalid_grant", "error_description": TRAP})
    )
    result = gw.cli("connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url))
    assert result.code == 2
    assert "invalid_grant" in result.err
    assert TRAP not in result.err
    assert gw.row(NAME)["state"] == "pending"
    assert gw.row(NAME)["tokens_enc"] is None
    provider.script.append(Reply(status=503))
    url = auth_url(gw.ok("connection", "reauthorize", NAME))
    transient = gw.cli(
        "connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url)
    )
    assert transient.code == 1
    assert "transient" in transient.err
    assert fake_provider.TOKEN in {c.path for c in provider.journal}
