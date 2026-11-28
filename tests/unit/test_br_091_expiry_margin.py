"""BR-091 — a token expiring in less than 60 s is never returned (AC-20), injected clock."""

from __future__ import annotations

import json

import pytest

from hullgate import connections
from tests import fake_provider
from tests.fake_provider import Reply
from tests.support import Gateway, standard_tree

ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"


def _ready(gw: Gateway) -> fake_provider.Provider:
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    return provider


def test_br_091_renewed_before_return_at_59_s(gw: Gateway) -> None:
    provider = _ready(gw)
    gw.expire(NAME, 59)
    access = gw.access(NAME, ROLE)
    assert len(provider.calls(fake_provider.TOKEN, "refresh_token")) == 1
    assert access.expires_at is not None
    assert access.expires_at - gw.clock() >= 60
    assert access.generation == 2


def test_br_091_failed_renewal_returns_nothing(gw: Gateway) -> None:
    provider = _ready(gw)
    gw.expire(NAME, 59)
    provider.script.append(Reply(status=503))
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)


def test_br_091_no_renewal_at_61_s(gw: Gateway) -> None:
    provider = _ready(gw)
    gw.expire(NAME, 61)
    assert gw.access(NAME, ROLE).generation == 1
    assert provider.calls(fake_provider.TOKEN, "refresh_token") == []


def test_br_091_missing_expires_in_is_estimated(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.provider()
    provider.expires_in = None
    gw.oauth()
    [view] = json.loads(gw.ok("connection", "status", NAME, "--json"))
    assert view["expiry_estimated"] is True
    assert gw.row(NAME)["expires_at"] == int(gw.clock()) + 3600
    assert "(estimated)" in gw.ok("connection", "status", NAME)


def test_br_091_short_expiry_at_exchange_refused(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.provider()
    provider.expires_in = 30
    gw.service()
    gw.application()
    url = gw.open_oauth()
    result = gw.cli("connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url))
    assert result.code == 2
    assert "expiry_too_short" in result.err
    assert gw.row(NAME)["state"] == "pending"


def test_br_091_without_refresh_token_reauth_without_call(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.provider()
    provider.give_refresh = False
    gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    gw.expire(NAME, 59)
    with pytest.raises(connections.ReauthRequired):
        gw.access(NAME, ROLE)
    assert provider.calls(fake_provider.TOKEN, "refresh_token") == []
    assert gw.row(NAME)["state"] == "reauth_required"


def test_br_091_renewal_without_new_refresh_token_keeps_old(gw: Gateway) -> None:
    provider = _ready(gw)
    provider.rotate = False  # the provider returns no new refresh_token
    first = gw.tokens(NAME)
    assert first is not None
    for _ in range(2):
        gw.expire(NAME, 30)
        gw.access(NAME, ROLE)
    calls = provider.calls(fake_provider.TOKEN, "refresh_token")
    assert [c.form["refresh_token"] for c in calls] == [first["refresh_token"]] * 2


def test_br_091_reduced_scopes_recorded_and_audited(gw: Gateway) -> None:
    provider = _ready(gw)
    provider.scope = "read:issue"
    gw.expire(NAME, 30)
    gw.access(NAME, ROLE)
    assert json.loads(gw.ok("connection", "status", NAME, "--json"))[0]["scopes"] == ["read:issue"]
    entry = [e for e in gw.audit() if e["action"] == "connection.renew"][-1]
    assert json.loads(entry["before"]) == {"scopes": "write:issue openid"}
    assert json.loads(entry["after"]) == {"scopes": "read:issue"}
