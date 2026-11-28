"""BR-207, BR-208 (AC-07) — sign-in attempts are limited by client address; a forwarded address
is believed only from a declared proxy."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from hullgate import __main__ as main
from hullgate import config
from hullgate.admin import accounts, auth
from hullgate.db import Database
from tests.admin_support import Console, SoftAuthenticator
from tests.support import Gateway


def _fail(console: Console, client: TestClient, headers: dict[str, str] | None = None) -> int:
    extra = headers or {}
    options = client.post("/admin/api/v1/auth/options", json={},
                          headers={**console.headers(csrf=""), **extra})  # fmt: skip
    if options.status_code != 200:
        return int(options.status_code)
    credential = SoftAuthenticator().get(options.json()["publicKey"])  # unknown credential
    response = client.post("/admin/api/v1/auth/assert", json={"credential": credential},
                           headers={**console.headers(csrf=""), **extra})  # fmt: skip
    return int(response.status_code)


def test_br_207_sixth_attempt_is_refused_without_evaluation(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
    with console.client() as client:
        assert [_fail(console, client) for _ in range(5)] == [401] * 5
        before = [e for e in gw.audit() if e["action"] == "admin.sign_in"]
        response = console.sign_in(client, key)  # a valid passkey: not evaluated
        assert response.status_code == 429
        assert response.json()["code"] == "rate_limited"
        refused = [e for e in gw.audit() if e["action"] == "admin.sign_in"][len(before) :]
        assert [e["reason"] for e in refused] == ["rate_limited"]
    with console.client(address="198.51.100.20") as other:
        assert console.sign_in(other, key).status_code == 200  # another address is not limited
    failures = [
        e for e in gw.audit() if e["action"] == "admin.sign_in" and e["result"] == "refused"
    ]
    assert len(failures) == 6


def test_br_207_window_slides(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        for _ in range(5):
            _fail(console, client)
            gw.clock.advance(60)
        assert console.sign_in(client, key).status_code == 429
        gw.clock.advance(accounts.ATTEMPT_WINDOW_S - 5 * 60 + 61)
        assert console.sign_in(client, key).status_code == 200


def test_br_207_enrolment_failures_count_too(gw: Gateway) -> None:
    console = Console(gw)
    token = console.bootstrap()
    with console.client() as client:
        for _ in range(5):
            r = console.call(client, "POST", "enrol/options", {"link": "nope"}, csrf="")
            assert r.status_code == 404
        r = console.call(client, "POST", "enrol/options", {"link": token}, csrf="")
        assert r.status_code == 429


def test_br_207_sixth_live_challenge_is_refused(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        codes = [console.call(client, "POST", "auth/options", {}, csrf="").status_code
                 for _ in range(auth.LIVE_CHALLENGES + 1)]  # fmt: skip
        assert codes == [200] * auth.LIVE_CHALLENGES + [429]
        gw.clock.advance(auth.CHALLENGE_TTL_S + 1)
        assert console.call(client, "POST", "auth/options", {}, csrf="").status_code == 200


def test_br_207_attempts_clear_reopens_the_window(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        for _ in range(5):
            _fail(console, client)
        assert console.sign_in(client, key).status_code == 429
        out = gw.ok("admin", "attempts", "clear")
        assert "sign-in attempts cleared: 5" in out
        assert console.sign_in(client, key).status_code == 200
    cleared = [e for e in gw.audit() if e["action"] == "admin.attempts.clear"]
    assert cleared[0]["actor"] == "operator-cli"


def test_br_208_forwarded_addresses_count_apart_behind_a_declared_proxy(gw: Gateway) -> None:
    console = Console(gw, proxies="172.18.0.0/16")
    with console.client() as client:
        key = console.admin(client)
    with console.client(address="172.18.0.2") as proxy:
        a = {"X-Forwarded-For": "198.51.100.1"}
        b = {"X-Forwarded-For": "198.51.100.2"}
        assert [_fail(console, proxy, a) for _ in range(5)] == [401] * 5
        assert _fail(console, proxy, a) == 429
        assert _fail(console, proxy, b) == 401
    with console.client(address="192.0.2.9") as undeclared:
        # an undeclared peer's header is ignored: it counts under its own address
        assert [_fail(console, undeclared, a) for _ in range(5)] == [401] * 5
        assert _fail(console, undeclared, b) == 429
    assert key


def test_br_208_both_listeners_read_no_proxy_headers(tmp_path: Path) -> None:
    cfg = config.load(
        {
            "PUBLIC_URL": "https://mcp.example.test",
            "ADMIN_URL": "http://localhost:14791",
            "DATA_DIR": str(tmp_path),
        }
    )
    db = Database(cfg.db_path)
    assert db.open()
    try:
        listeners = main.configs(cfg, db)
    finally:
        db.close()
    assert len(listeners) == 2
    assert [c.proxy_headers for c in listeners] == [False, False]
    assert listeners[1].access_log is False  # enrolment links travel in the admin paths
