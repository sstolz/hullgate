"""BR-073 — the barrier closes after five failures in fifteen minutes (injected clock)."""

from __future__ import annotations

from starlette.testclient import TestClient

from hullgate.db import Database
from tests.support import (
    Gateway,
    authorize_params,
    code_from,
    consent,
    pkce,
    request_id,
    standard_tree,
    start_authorization,
)

ROLE = "mpx/cairn/staging/dev"


def _req(client: TestClient) -> str:
    _, challenge = pkce()
    return request_id(start_authorization(client, ROLE, authorize_params(ROLE, challenge)))


def _fail(gw: Gateway, client: TestClient, req: str, n: int) -> None:
    for _ in range(n):
        r = consent(gw, client, ROLE, req, code="999999")
        assert r.status_code in (200, 302)  # 302: right code by coincidence, unlikely
        assert r.status_code == 200


def test_br_073_fifth_failure_closes_the_barrier(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        req = _req(client)
        _fail(gw, client, req, 5)
        locked = consent(gw, client, ROLE, req)  # right code, not evaluated
        assert locked.status_code == 429
        assert "location" not in locked.headers
    reasons = [e["reason"] for e in gw.audit() if e["action"] == "oauth.consent"]
    assert reasons.count("consent_failed") == 5
    assert "consent_locked" in reasons
    conn = gw.connect()
    try:
        assert conn.execute("SELECT count(*) FROM auth_code").fetchone() == (0,)
    finally:
        conn.close()


def test_br_073_window_slides(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        req = _req(client)
        _fail(gw, client, req, 3)
        gw.clock.advance(9 * 60)
        _fail(gw, client, req, 2)
        assert consent(gw, client, ROLE, req).status_code == 429
        gw.clock.advance(6 * 60 + 1)  # the first three leave the window
        req2 = _req(client)  # the previous request expired (10 min)
        code_from(consent(gw, client, ROLE, req2))


def test_br_073_counter_survives_restart(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        req = _req(client)
        _fail(gw, client, req, 5)
    gw.db.close()
    gw.db = Database(str(gw.path))
    assert gw.db.open()
    with gw.client() as client:
        assert consent(gw, client, ROLE, req).status_code == 429


def test_br_073_concurrent_wrong_codes_evaluate_at_most_five(gw: Gateway) -> None:
    """F-020 (AC-11): the window's check, the evaluation and the count run in one write
    transaction: of forty concurrent wrong POSTs, at most five are evaluated."""
    import threading

    standard_tree(gw)
    gw.preregister("mpx")
    statuses: list[int] = []
    with gw.client() as client:
        req = _req(client)
        start = threading.Barrier(40)

        def post() -> None:
            start.wait()
            statuses.append(consent(gw, client, ROLE, req, code="999999").status_code)

        threads = [threading.Thread(target=post) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    reasons = [e["reason"] for e in gw.audit() if e["action"] == "oauth.consent"]
    assert reasons.count("consent_failed") == 5
    assert reasons.count("consent_locked") == 35
    assert sorted(statuses) == [200] * 5 + [429] * 35


def test_br_073_transaction_purges_rows_past_the_window(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        req = _req(client)
        _fail(gw, client, req, 3)
        gw.clock.advance(15 * 60 + 1)
        req = _req(client)
        _fail(gw, client, req, 1)
    conn = gw.connect()
    try:
        assert conn.execute("SELECT count(*) FROM consent_failure").fetchone() == (1,)
    finally:
        conn.close()
