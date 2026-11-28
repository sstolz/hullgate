"""BR-097 — a provider's endpoints are fixed at the service's declaration (AC-29): a metadata
document changed afterwards sends nothing elsewhere."""

from __future__ import annotations

import json

from tests import fake_provider
from tests.support import Gateway, auth_url, standard_tree

ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"


def test_br_097_declared_endpoints_listed_and_audited(gw: Gateway) -> None:
    provider = gw.provider()
    out = gw.ok(
        "service",
        "declare",
        "gitea-dev",
        "--server",
        "https://gitea-dev.example.test",
        "--model",
        "gitea",
    )
    assert f"token_endpoint: {provider.base}{fake_provider.TOKEN}" in out
    [listed] = json.loads(gw.ok("service", "list", "--json"))
    assert listed["endpoints"] == {
        "authorization_endpoint": provider.base + fake_provider.AUTHORIZE,
        "registration_endpoint": provider.base + fake_provider.REGISTER,
        "revocation_endpoint": provider.base + fake_provider.REVOKE,
        "token_endpoint": provider.base + fake_provider.TOKEN,
        "userinfo_endpoint": provider.base + fake_provider.USERINFO,
    }
    assert f"revocation_endpoint: {provider.base}" in gw.ok("service", "list")
    entry = [e for e in gw.audit() if e["action"] == "service.declare"][-1]
    assert json.loads(entry["after"])["endpoints"] == listed["endpoints"]


def test_br_097_changed_metadata_never_followed_until_rediscover(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    other = gw.provider("evil")
    evil = {
        name: f"{other.base}{path}"
        for name, path in (
            ("token_endpoint", fake_provider.TOKEN),
            ("revocation_endpoint", fake_provider.REVOKE),
            ("userinfo_endpoint", fake_provider.USERINFO),
            ("authorization_endpoint", fake_provider.AUTHORIZE),
        )
    }
    provider.metadata_patch = evil
    # reauthorization (code exchange), renewal, revocation: endpoints kept
    url = auth_url(gw.ok("connection", "reauthorize", NAME))
    assert url.startswith(provider.base)
    gw.ok("connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url))
    gw.expire(NAME, 30)
    gw.access(NAME, ROLE)
    gw.ok("connection", "revoke", NAME)
    assert provider.calls(fake_provider.TOKEN, "refresh_token")
    assert provider.revoked
    assert other.journal == []
    # explicit rediscovery: new values, audit before and after
    out = gw.ok("service", "rediscover", "gitea-dev", "--confirm", "gitea-dev")
    assert f"token_endpoint: {other.base}" in out
    assert "(before: https://gitea-dev.example.test" in out
    entry = [e for e in gw.audit() if e["action"] == "service.rediscover"][-1]
    before, after = json.loads(entry["before"]), json.loads(entry["after"])
    assert before["endpoints"]["token_endpoint"] == provider.base + fake_provider.TOKEN
    assert after["endpoints"]["token_endpoint"] == other.base + fake_provider.TOKEN
    # without --confirm: refusal, nothing written
    assert gw.cli("service", "rediscover", "gitea-dev").code == 2


def test_br_097_http_endpoint_refused_at_declaration(gw: Gateway) -> None:
    provider = gw.provider("plain")
    provider.metadata_patch = {"token_endpoint": "http://plain.example.test/token"}
    result = gw.cli(
        "service",
        "declare",
        "plain",
        "--server",
        "https://plain.example.test",
        "--model",
        "gitea",
    )
    assert result.code == 2
    assert "token_endpoint not https" in result.err
    assert json.loads(gw.ok("service", "list", "--json")) == []
