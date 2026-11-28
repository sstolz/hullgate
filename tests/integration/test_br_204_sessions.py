"""BR-204 (AC-04) — an admin session is short, server-side, and kept as a digest."""

from __future__ import annotations

import hashlib

import pytest
from starlette.testclient import TestClient

from hullgate import config
from hullgate.admin import sessions
from tests.admin_support import Console, SoftAuthenticator
from tests.support import REGISTRY, Gateway


def _session_status(console: Console, client: TestClient, *, activity: bool = True) -> int:
    response = console.call(client, "GET", "session", activity=activity)
    return int(response.status_code)


def test_br_204_cookie_attributes_on_loopback(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        response = console.enrol(client, console.bootstrap(), SoftAuthenticator())
    cookie = response.headers["set-cookie"]
    parts = {p.strip().split("=", 1)[0].lower() for p in cookie.split(";")}
    assert cookie.startswith(f"{sessions.COOKIE}=")
    assert {"httponly", "path", "samesite"} <= parts
    assert "path=/admin" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert "secure" not in parts  # loopback ADMIN_URL: the one exemption


def test_br_204_cookie_is_secure_under_https(gw: Gateway) -> None:
    console = Console(gw, admin_url="https://admin.example.test")
    with console.client() as client:
        response = console.enrol(client, console.bootstrap(), SoftAuthenticator())
        assert response.status_code == 200
        assert "secure" in {p.strip().lower() for p in response.headers["set-cookie"].split(";")}
        assert _session_status(console, client) == 200


def test_br_204_startup_refuses_http_off_loopback() -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load({"PUBLIC_URL": "https://m.example.test", "ADMIN_URL": "http://admin.test"})
    assert err.value.variable == "ADMIN_URL"


def test_br_204_idle_expiry_and_activity(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        gw.clock.advance(sessions.IDLE_S - 60)
        assert _session_status(console, client) == 200  # operator activity: extends
        gw.clock.advance(sessions.IDLE_S - 60)
        assert _session_status(console, client, activity=False) == 200  # a poll: does not
        gw.clock.advance(61)
        assert _session_status(console, client) == 401


def test_br_204_absolute_expiry_holds_under_activity(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        step, elapsed = sessions.IDLE_S - 60, 0
        while elapsed + step < sessions.ABSOLUTE_S:
            gw.clock.advance(step)
            elapsed += step
            assert _session_status(console, client) == 200, elapsed
        gw.clock.advance(sessions.ABSOLUTE_S - elapsed)
        assert _session_status(console, client) == 401


def test_br_204_sign_out_ends_the_session(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        value = client.cookies.get(sessions.COOKIE)
        assert value
        assert console.call(client, "POST", "session/signout", {}).status_code == 200
        client.cookies.set(sessions.COOKIE, value, path="/admin")
        assert _session_status(console, client) == 401
    conn = gw.connect()
    try:
        assert conn.execute("SELECT count(*) FROM admin_session").fetchone()[0] == 0
    finally:
        conn.close()
    assert [e["action"] for e in gw.audit()][-1] == "admin.sign_out"


def test_br_204_stored_as_digest_and_survives_a_restart(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        value = client.cookies.get(sessions.COOKIE)
    assert value is not None
    REGISTRY.secrets.append(value)
    conn = gw.connect()
    try:
        stored = [r[0] for r in conn.execute("SELECT digest FROM admin_session")]
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert stored == [hashlib.sha256(value.encode()).hexdigest()]
    assert value not in dump
    restarted = Console(gw)
    with restarted.client() as client:
        client.cookies.set(sessions.COOKIE, value, path="/admin")
        assert _session_status(restarted, client) == 200


def test_br_204_deleting_a_passkey_ends_the_other_sessions(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as first, console.client() as second:
        key = console.admin(first)
        first_csrf = console.csrf
        assert console.sign_in(second, key).status_code == 200
        second_csrf = console.csrf
        console.csrf = first_csrf
        assert console.fresh(first, key).status_code == 200
        spare = SoftAuthenticator()
        assert console.add_passkey(first, spare).status_code == 201
        response = console.call(first, "DELETE", f"passkeys/{spare.id}")
        assert response.status_code == 200
        assert response.json()["sessions_ended"] == 1
        assert _session_status(console, first) == 200  # the acting session stays
        console.csrf = second_csrf
        assert _session_status(console, second) == 401
