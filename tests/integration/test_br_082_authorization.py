"""BR-082 — outbound authorization requires PKCE S256 and a single-use `state` (AC-11).

Against the fake provider (`tests/fake_provider.py`): the upstream journal (`provider.journal`)
says what reached the provider; the "operator's browser" is `provider.approve`.
"""

from __future__ import annotations

import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from hullgate import connections
from tests import fake_provider
from tests.support import Gateway, auth_url, standard_tree

ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _setup(gw: Gateway) -> fake_provider.Provider:
    standard_tree(gw)
    gw.service()
    gw.application()
    return gw.provider()


def _secret_free(gw: Gateway, *values: str, outputs: tuple[str, ...] = ()) -> None:
    dump = json.dumps(gw.audit(), ensure_ascii=False)
    for value in values:
        assert value not in dump
        for text in outputs:
            assert value not in text


def test_br_082_url_pkce_state_redirect_then_active(gw: Gateway) -> None:
    provider = _setup(gw)
    url = gw.open_oauth()
    q = _query(url)
    assert url.startswith(provider.base + fake_provider.AUTHORIZE + "?")
    assert q["code_challenge_method"] == "S256"
    assert len(q["code_challenge"]) == 43
    assert len(q["state"]) >= 32
    assert q["redirect_uri"] == "http://127.0.0.1/oauth/callback"
    assert q["client_id"] == "client-app"
    assert q["scope"] == "write:issue openid"
    assert "resource" not in q  # gitea model (§3.1)
    state = json.loads(gw.ok("connection", "status", NAME, "--json"))[0]
    assert state["state"] == "pending"
    callback = provider.approve(url)
    out = gw.ok("connection", "authorize", NAME, "--callback", "-", stdin=callback + "\n")
    assert "authorized: mpx/gitea-dev, account cairn-ai" in out
    [exchange] = provider.calls(fake_provider.TOKEN, "authorization_code")
    assert exchange.form["redirect_uri"] == "http://127.0.0.1/oauth/callback"
    assert exchange.form["client_id"] == "client-app"
    assert "code_verifier" in exchange.form
    [view] = json.loads(gw.ok("connection", "status", NAME, "--json"))
    assert (view["state"], view["account"], view["scopes"]) == (
        "active",
        "cairn-ai",
        ["write:issue", "openid"],
    )
    assert view["expiry_estimated"] is False
    code = _query(callback)["code"]
    _secret_free(gw, code, callback, outputs=(out,))


def test_br_082_resource_for_generique_with_rfc9728_only(gw: Gateway) -> None:
    standard_tree(gw)
    with_prm = gw.provider("svc", "generic")
    with_prm.prm = True
    with_prm.resource = "https://svc.example.test/mcp"
    gw.ok(
        "service",
        "declare",
        "svc",
        "--server",
        "https://svc.example.test",
        "--model",
        "generic",
    )
    gw.application("app", "svc")
    url = gw.open_oauth("mpx/svc", "sv", "svc", "app")
    assert _query(url)["resource"] == "https://svc.example.test/mcp"
    gw.ok(
        "connection", "authorize", "mpx/svc", "--callback", "-", stdin=with_prm.approve(url) + "\n"
    )
    [exchange] = with_prm.calls(fake_provider.TOKEN, "authorization_code")
    assert exchange.form["resource"] == "https://svc.example.test/mcp"
    # without an RFC 9728 document: no `resource`
    gw.provider("plain", "generic")
    gw.ok(
        "service",
        "declare",
        "plain",
        "--server",
        "https://plain.example.test",
        "--model",
        "generic",
    )
    gw.application("app2", "plain")
    assert "resource" not in _query(gw.open_oauth("mpx/plain", "pl", "plain", "app2"))


def test_br_082_unknown_replayed_expired_state_refused_without_exchange(gw: Gateway) -> None:
    provider = _setup(gw)
    url = gw.open_oauth()
    callback = provider.approve(url)
    forged = callback.replace(_query(callback)["state"], "x" * 43)
    result = gw.cli("connection", "authorize", NAME, "--callback", "-", stdin=forged)
    assert result.code == 2
    assert "unknown state" in result.err
    assert provider.calls(fake_provider.TOKEN) == []
    gw.ok("connection", "authorize", NAME, "--callback", "-", stdin=callback)
    replay = gw.cli("connection", "authorize", NAME, "--callback", "-", stdin=callback)
    assert replay.code == 2
    assert "already used" in replay.err
    assert len(provider.calls(fake_provider.TOKEN)) == 1
    # expired: new authorization, clock at +10 min
    url2 = auth_url(gw.ok("connection", "reauthorize", NAME))
    callback2 = provider.approve(url2)
    gw.clock.advance(600)
    late = gw.cli("connection", "authorize", NAME, "--callback", "-", stdin=callback2)
    assert late.code == 2
    assert "state expired" in late.err
    assert len(provider.calls(fake_provider.TOKEN)) == 1
    for refusal in (result, replay, late):
        _secret_free(gw, callback, callback2, outputs=(refusal.out, refusal.err))
    reasons = [e["reason"] for e in gw.audit() if e["action"] == "connection.authorize_refused"]
    assert reasons == ["unknown state", "state already used", "state expired"]


def test_br_082_concurrent_authorize_one_exchange(gw: Gateway) -> None:
    provider = _setup(gw)
    callback = provider.approve(gw.open_oauth())
    codes: list[int] = []

    def go() -> None:
        codes.append(
            gw.cli("connection", "authorize", NAME, "--callback", "-", stdin=callback).code
        )

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [0, 2]
    assert len(provider.calls(fake_provider.TOKEN, "authorization_code")) == 1


def test_br_082_provider_error_consumes_state(gw: Gateway) -> None:
    provider = _setup(gw)
    url = gw.open_oauth()
    state = _query(url)["state"]
    denied = (
        f"http://127.0.0.1/oauth/callback?error=access_denied&error_description=PIEGE&state={state}"
    )
    result = gw.cli("connection", "authorize", NAME, "--callback", "-", stdin=denied)
    assert result.code == 2
    assert "access_denied" in result.err
    assert "PIEGE" not in result.err
    assert json.loads(gw.ok("connection", "status", NAME, "--json"))[0]["state"] == "pending"
    assert provider.calls(fake_provider.TOKEN) == []
    again = gw.cli("connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url))
    assert again.code == 2
    assert "already used" in again.err
    entry = next(e for e in gw.audit() if e["action"] == "connection.authorize_refused")
    assert entry["reason"] == "provider: access_denied"
    _secret_free(gw, "PIEGE", state)


def test_br_082_callback_as_argument_refused(gw: Gateway) -> None:
    provider = _setup(gw)
    callback = provider.approve(gw.open_oauth())
    code = _query(callback)["code"]
    result = gw.cli("connection", "authorize", NAME, "--callback", callback)
    assert result.code == 2
    _secret_free(gw, code, callback, outputs=(result.out, result.err))
    assert provider.calls(fake_provider.TOKEN) == []


def test_br_082_pending_is_not_authorized_and_reopen_replaces_state(gw: Gateway) -> None:
    provider = _setup(gw)
    url1 = gw.open_oauth()
    gw.ok("connection", "place", NAME, ROLE)
    with pytest.raises(connections.NotAuthorized) as err:
        gw.access(NAME, ROLE)
    assert err.value.code == "access_not_authorized"
    url2 = auth_url(gw.ok("connection", "reauthorize", NAME))
    old = gw.cli("connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url1))
    assert old.code == 2
    assert "unknown state" in old.err
    gw.ok("connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url2))
    assert gw.access(NAME, ROLE).authorization.startswith("Bearer at-")


def test_br_082_reauthorize_active_keeps_tokens_until_exchange(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    before = gw.access(NAME, ROLE).authorization
    old_tokens = gw.tokens(NAME)
    assert old_tokens is not None
    url = auth_url(gw.ok("connection", "reauthorize", NAME))
    assert json.loads(gw.ok("connection", "status", NAME, "--json"))[0]["state"] == "active"
    assert gw.access(NAME, ROLE).authorization == before  # served until the exchange
    out = gw.ok("connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url))
    assert "former tokens: revocation revoked" in out
    assert gw.access(NAME, ROLE).authorization != before
    revoked = {token for _hint, _client, token in provider.revoked}
    assert {old_tokens["access_token"], old_tokens["refresh_token"]} <= revoked
