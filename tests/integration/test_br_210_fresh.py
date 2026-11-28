"""BR-210 (AC-09) — a security act asks for the passkey again."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from hullgate.admin import sessions
from tests.admin_support import Console, SoftAuthenticator
from tests.support import Gateway

# The rule's population: adding or deleting a passkey, changing the allow-list.
ACTS = ["passkeys/options", "passkeys", "passkeys/{credential_id}", "allowlist"]


def test_br_210_population_is_the_routes_declared_fresh(gw: Gateway) -> None:
    console = Console(gw)
    assert sorted(s.path for s in console.app.api_routes if s.fresh) == sorted(ACTS)


def _act(console: Console, client: TestClient, path: str, key: SoftAuthenticator) -> Any:
    if path == "passkeys/options":
        return console.call(client, "POST", path, {})
    if path == "passkeys":
        return console.add_passkey(client, SoftAuthenticator())
    if path == "allowlist":
        return console.call(client, "PUT", path, {"cidrs": []})
    return console.call(client, "DELETE", f"passkeys/{key.id}")


@pytest.mark.parametrize("path", ACTS)
def test_br_210_act_needs_a_fresh_assertion(gw: Gateway, path: str) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        gw.clock.advance(sessions.FRESH_S + 1)  # the enrolment itself is not fresh
        if path == "passkeys":
            refused = console.call(client, "POST", "passkeys/options", {})
        else:
            refused = _act(console, client, path, key)
        assert refused.status_code == 403
        assert refused.json()["code"] == "fresh_auth_required"
        value, token = client.cookies.get(sessions.COOKIE), console.csrf
        fresh = console.fresh(client, key)
        assert fresh.status_code == 200
        assert (client.cookies.get(sessions.COOKIE), console.csrf) == (value, token)
        if path == "passkeys/{credential_id}":
            assert console.add_passkey(client, SoftAuthenticator()).status_code == 201
        done = _act(console, client, path, key)
        assert done.status_code in (200, 201), done.text
        gw.clock.advance(sessions.FRESH_S + 1)
        assert console.call(client, "POST", "passkeys/options", {}).status_code == 403


def test_br_210_fresh_takes_a_passkey_of_the_account(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        other = SoftAuthenticator()
        response = console.fresh(client, other)
        assert response.status_code == 403
        assert response.json()["code"] == "fresh_auth_failed"
        assert console.call(client, "POST", "passkeys/options", {}).status_code == 403


def test_br_210_last_passkey_cannot_be_deleted(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        response = console.call(client, "DELETE", f"passkeys/{key.id}")
        assert response.status_code == 409
        assert response.json()["code"] == "last_passkey"
        listed = console.call(client, "GET", "passkeys").json()["passkeys"]
    assert [p["id"] for p in listed] == [key.id]


def test_br_210_passkey_add_and_delete_are_audited(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        spare = SoftAuthenticator()
        assert console.add_passkey(client, spare, label="backup").status_code == 201
        assert console.sign_in(client, spare).status_code == 200
        assert console.fresh(client, spare).status_code == 200
        assert console.call(client, "DELETE", f"passkeys/{key.id}").status_code == 200
        assert console.call(client, "DELETE", "passkeys/unknown").status_code == 404
    actions = [e["action"] for e in gw.audit() if e["action"].startswith("admin.passkey.")]
    assert actions == ["admin.passkey.add", "admin.passkey.add", "admin.passkey.delete"]
