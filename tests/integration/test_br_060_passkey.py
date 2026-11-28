"""AC-32: a barrier assertion is public-origin, single-use and consent-bound; TOTP is separate."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from starlette.testclient import TestClient

from hullgate import config
from hullgate.admin import barrier
from hullgate.app import build_public_app
from tests.admin_support import Console, SoftAuthenticator
from tests.integration.test_br_060_consent import ROLE, _codes, _pending, _setup
from tests.policy_support import rows
from tests.support import PUBLIC_URL, Gateway, code_from, consent, consent_page, org


@contextmanager
def public(
    gw: Gateway, admin: config.AdminOrigin, peer: str = "203.0.113.8"
) -> Iterator[TestClient]:
    app = build_public_app(
        gw.db,
        PUBLIC_URL,
        key=gw.key,
        totp=gw.totp,
        clock=gw.clock,
        admin_origin=admin,
        relay=gw.relay(),
    )
    with TestClient(app, follow_redirects=False, client=(peer, 1)) as client:
        yield client


def post(
    client: TestClient,
    req: str,
    decision: str,
    credential: dict[str, Any] | None = None,
    origin: str = PUBLIC_URL,
) -> Any:
    data = {"req": req, "decision": decision}
    if credential is not None:
        data["credential"] = json.dumps(credential)
    return client.post(
        f"/o/{org()}/oauth/p/{ROLE}/consent",
        data=data,
        headers={"Cookie": f"hullgate_consent={req}", "Origin": origin},
    )


def enrolled(gw: Gateway) -> tuple[Console, SoftAuthenticator]:
    _setup(gw)
    console = Console(gw, admin_url="https://admin.example.test", rp_id="example.test")
    with console.client() as client:
        key = console.admin(client)
    return console, key


def test_br_060_passkey_issues_code_without_totp_and_no_admin_session(gw: Gateway) -> None:
    console, key = enrolled(gw)
    gw.totp_path.chmod(0o600)
    gw.totp_path.unlink()
    sessions = rows(gw, "SELECT * FROM admin_session")
    with public(gw, console.origin) as client:
        req = _pending(gw, client)
        page = consent_page(client, ROLE, req)
        assert "Authorize with a passkey" in page.text
        assert "script-src 'sha256-" in page.headers["content-security-policy"]
        options = post(client, req, "passkey-options")
        assert options.status_code == 200, options.text
        credential = key.get(options.json()["publicKey"], PUBLIC_URL)
        done = post(client, req, "passkey", credential)
        code_from(done)
        assert "set-cookie" not in done.headers
        assert post(client, req, "passkey", credential).status_code == 400
    assert _codes(gw) == 1
    assert rows(gw, "SELECT * FROM admin_session") == sessions
    event = next(a for a in gw.audit() if a["action"] == "oauth.code.issue")
    assert json.loads(event["detail"])["factor"] == "passkey"


def test_br_073_totp_lock_does_not_lock_passkey(gw: Gateway) -> None:
    console, key = enrolled(gw)
    with public(gw, console.origin) as client:
        req = _pending(gw, client)
        for _ in range(5):
            assert consent(gw, client, ROLE, req, code="wrong").status_code == 200
        locked = consent(gw, client, ROLE, req, code="wrong")
        assert locked.status_code == 429
        assert "Authorize with a passkey" in locked.text
        options = post(client, req, "passkey-options").json()["publicKey"]
        code_from(post(client, req, "passkey", key.get(options, PUBLIC_URL)))
    assert rows(gw, "SELECT count(*) FROM consent_failure") == [(5,)]


@pytest.mark.parametrize(
    "bad", ["origin", "uv", "rp", "unknown", "expired", "request", "admin-challenge", "address"]
)
def test_br_060_passkey_refuses_invalid_ceremonies(gw: Gateway, bad: str) -> None:
    console, key = enrolled(gw)
    with public(gw, console.origin) as client:
        req = _pending(gw, client)
        options = post(client, req, "passkey-options").json()["publicKey"]
        if bad == "admin-challenge":
            with console.client() as admin:
                options = console.call(admin, "POST", "auth/options", {}, csrf="").json()[
                    "publicKey"
                ]
        kwargs: dict[str, Any] = {}
        if bad == "uv":
            kwargs["uv"] = False
        if bad == "rp":
            kwargs["rp_id"] = "evil.test"
        if bad == "unknown":
            key = SoftAuthenticator()
        credential = key.get(
            options, console.origin.origin if bad == "origin" else PUBLIC_URL, **kwargs
        )
        if bad == "expired":
            gw.clock.advance(301)
        if bad == "request":
            req = _pending(gw, client)
        if bad == "address":
            with public(gw, console.origin, peer="203.0.113.9") as other:
                answer = post(other, req, "passkey", credential)
        else:
            answer = post(client, req, "passkey", credential)
        assert answer.status_code == 200
        assert "Passkey refused" in answer.text
        assert _codes(gw) == 0
        assert rows(gw, "SELECT count(*) FROM consent_failure") == [(0,)]


def test_br_073_passkey_window_is_by_address_and_totp_stays_available(gw: Gateway) -> None:
    console, _ = enrolled(gw)
    with public(gw, console.origin) as client:
        req = _pending(gw, client)
        for _ in range(5):
            assert post(client, req, "passkey", {}).status_code == 200
        assert post(client, req, "passkey-options").status_code == 429
        assert post(client, req, "passkey", {}).status_code == 429
        with public(gw, console.origin, peer="203.0.113.9") as other:
            assert post(other, req, "passkey-options").status_code == 200
        code_from(consent(gw, client, ROLE, req))
    assert rows(gw, "SELECT count(*) FROM consent_failure") == [(0,)]


def test_br_060_localhost_and_sibling_rp_never_offer_passkey(gw: Gateway) -> None:
    console, _ = enrolled(gw)
    # Browser Origins canonicalize host case and omit the default HTTPS port.
    canonical = barrier.origin("https://MCP.example.test:443/", console.origin)
    assert canonical is not None
    assert canonical.origin == "https://mcp.example.test"
    alternate = barrier.origin("https://mcp.example.test:8443", console.origin)
    assert alternate is not None
    assert alternate.origin == "https://mcp.example.test:8443"
    for origin in (
        config.admin_origin("http://localhost:14791"),
        config.admin_origin("https://admin.example.test"),
    ):
        with public(gw, origin) as client:
            req = _pending(gw, client)
            assert "Authorize with a passkey" not in consent_page(client, ROLE, req).text
            assert post(client, req, "passkey-options").status_code == 403
    assert console.origin.rp_id == "example.test"


def test_br_060_options_require_origin_and_double_submit(gw: Gateway) -> None:
    console, _ = enrolled(gw)
    with public(gw, console.origin) as client:
        req = _pending(gw, client)
        assert post(client, req, "passkey-options", origin="https://evil.test").status_code == 403
        assert (
            client.post(
                f"/o/{org()}/oauth/p/{ROLE}/consent",
                data={"req": req, "decision": "passkey-options"},
                headers={"Origin": PUBLIC_URL},
            ).status_code
            == 400
        )
        for _ in range(5):
            assert post(client, req, "passkey-options").status_code == 200
        assert post(client, req, "passkey-options").status_code == 429
