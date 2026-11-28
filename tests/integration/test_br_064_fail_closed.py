"""BR-064 — vault or key outage: explicit 503, nothing issued (AC-05).

Access point, CLI and every OAuth route (`authorize`, `consent`, `token`, `revoke`, `register`
with DCR on, metadata), database down then connections key absent (`key_absent`).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.support import (
    Gateway,
    authorize_params,
    get_code,
    grant,
    mcp,
    org,
    pkce,
    request_id,
    resource,
    standard_tree,
    start_authorization,
)

ISSUED_TABLES = ("operator_token", "lineage", "auth_code", "refresh", "access_jti")


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # noqa: S608
            for t in (*ISSUED_TABLES, "audit")
        }
    finally:
        conn.close()


@contextmanager
def _lock_held(gw: Gateway) -> Iterator[None]:
    holder = sqlite3.connect(gw.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        yield
    finally:
        holder.execute("ROLLBACK")
        holder.close()


@contextmanager
def _read_only(gw: Gateway) -> Iterator[None]:
    gw.path.chmod(0o444)
    try:
        yield
    finally:
        gw.path.chmod(0o644)


@contextmanager
def _closed(gw: Gateway) -> Iterator[None]:
    gw.db.close()
    try:
        yield
    finally:
        assert gw.db.open()


FAULTS: dict[str, Callable[[Gateway], AbstractContextManager[None]]] = {
    "lock held": _lock_held,
    "read-only file": _read_only,
    "database closed": _closed,
}


# `chmod 0444` does not take the write right away from root (os.access returns true): under root,
# the "read-only" outage would not be injected and the case would prove something else; skipped.
UNDER_ROOT = os.geteuid() == 0
ROOT_SKIP = pytest.mark.skipif(UNDER_ROOT, reason="read-only outage cannot be injected under root")


def _faults(names: list[str]) -> list[object]:
    return [pytest.param(n, marks=ROOT_SKIP) if n == "read-only file" else n for n in names]


@pytest.mark.parametrize("fault", _faults(sorted(FAULTS)))
def test_br_064_endpoint_503_json_without_redirect_nor_issuance(gw: Gateway, fault: str) -> None:
    standard_tree(gw)
    _, token = gw.issue("mpx/cairn/staging/dev")
    before = _counts(gw.path)
    with gw.client() as client:
        with FAULTS[fault](gw):
            responses = [
                mcp(client, "mpx/cairn/staging/dev", token),
                mcp(client, "mpx/cairn/staging/dev"),
                mcp(client, "mpx/cairn/staging/dev", "hg_unknown"),
            ]
        recovered = mcp(client, "mpx/cairn/staging/dev", token)
    for response in responses:
        assert response.status_code == 503, (fault, response.text)
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"error": "temporarily_unavailable"}
        assert "location" not in response.headers
    assert _counts(gw.path) == before
    assert recovered.status_code == 200  # the first request after the outage is served


def test_br_064_database_unreadable_for_step1_is_503_not_404(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client() as client:
        gw.db.close()
        try:
            response = mcp(client, "mpx/cairn/nope/dev")
        finally:
            gw.db.open()
    assert response.status_code == 503


@pytest.mark.parametrize("fault", _faults(["lock held", "read-only file"]))
def test_br_064_cli_writes_nothing_and_prints_no_token(gw: Gateway, fault: str) -> None:
    standard_tree(gw)
    before = _counts(gw.path)
    with FAULTS[fault](gw):
        issued = gw.cli("token", "create", "--path", "mpx", "--label", "x")
        added = gw.cli("tree", "add", "/", "client", "Acme")
    for result in (issued, added):
        assert result.code == 1, result
        assert "database unavailable" in result.err
    assert "hg_" not in issued.out + issued.err
    assert issued.out == ""
    assert _counts(gw.path) == before
    assert "acme" not in gw.ok("tree", "show")


ROLE = "mpx/cairn/staging/dev"


@contextmanager
def _key_removed(gw: Gateway) -> Iterator[None]:
    saved = gw.key_path.read_bytes()
    gw.key_path.chmod(0o600)
    gw.key_path.unlink()
    try:
        yield
    finally:
        gw.key_path.write_bytes(saved)


OAUTH_FAULTS: dict[str, Callable[[Gateway], AbstractContextManager[None]]] = {
    **FAULTS,
    "key_absent": _key_removed,
}


def _oauth_requests(gw: Gateway, client: TestClient) -> dict[str, Callable[[], Any]]:
    """One request per route, prepared before the outage (pending request, code, lineage)."""
    g = grant(gw, client, ROLE)
    code, verifier = get_code(gw, client, ROLE)
    _, challenge = pkce()
    req = request_id(start_authorization(client, ROLE, authorize_params(ROLE, challenge)))
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    base = f"/o/{org()}/oauth/p/{ROLE}"
    totp = gw.totp_code()

    def post(path: str, data: dict[str, str]) -> Any:
        from urllib.parse import urlencode

        cookie = {"Cookie": f"hullgate_consent={data['req']}"} if "req" in data else {}
        return client.post(path, content=urlencode(data), headers={**form, **cookie})

    return {
        "authorize": lambda: start_authorization(client, ROLE, authorize_params(ROLE, challenge)),
        "consent": lambda: post(f"{base}/consent", {"req": req, "decision": "allow", "totp": totp}),
        "token (code)": lambda: post(
            f"{base}/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": "http://localhost:4567/callback",
                "client_id": "client-test",
                "resource": resource(ROLE),
            },
        ),
        "token (renouvellement)": lambda: post(
            f"{base}/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": g["refresh_token"],
                "client_id": "client-test",
                "resource": resource(ROLE),
            },
        ),
        "revoke": lambda: post(
            f"{base}/revoke", {"token": g["refresh_token"], "client_id": "client-test"}
        ),
        "register": lambda: client.post(
            f"{base}/register", json={"redirect_uris": ["http://localhost:1/cb"]}
        ),
        "metadata 9728": lambda: client.get(
            f"/.well-known/oauth-protected-resource/o/{org()}/p/{ROLE}/mcp"
        ),
        "metadata 8414": lambda: client.get(f"/.well-known/oauth-authorization-server{base}"),
        "access point (JWT)": lambda: mcp(client, ROLE, g["access_token"]),
    }


@pytest.mark.parametrize("fault", _faults(sorted(OAUTH_FAULTS)))
def test_br_064_oauth_routes_503_json_without_redirect_nor_issuance(
    gw: Gateway, fault: str
) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    gw.ok("client", "dcr", "on")
    with gw.client() as client:
        requests = _oauth_requests(gw, client)
        before = _counts(gw.path)
        with OAUTH_FAULTS[fault](gw):
            responses = {name: send() for name, send in requests.items()}
            ready = client.get("/readyz")
        after = _counts(gw.path)
        recovered = requests["token (renouvellement)"]()
    assert ready.status_code == 503
    if fault == "key_absent":
        assert ready.json()["reason"] == "key_unavailable"
    for name, response in responses.items():
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", {})
        assert status == 503, (fault, name, getattr(response, "text", ""))
        assert headers["content-type"].startswith("application/json"), name
        assert "location" not in headers, name
        assert response.json() == {"error": "temporarily_unavailable"}, name
    for table in ISSUED_TABLES:
        assert after[table] == before[table], (fault, table)
    assert getattr(recovered, "status_code", None) == 200  # nothing was consumed


def test_br_064_key_file_unreadable_or_invalid_is_key_unavailable(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client() as client:
        assert client.get("/readyz").status_code == 200
        saved = gw.key_path.read_bytes()
        gw.key_path.chmod(0o600)
        gw.key_path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n")
        bad = client.get("/readyz")
        gw.key_path.write_bytes(saved)
        good = client.get("/readyz")
    assert (bad.status_code, bad.json()["reason"]) == (503, "key_unavailable")
    assert good.status_code == 200


def test_br_064_no_key_configured_means_not_ready(gw: Gateway) -> None:
    standard_tree(gw)
    _, token = gw.issue(ROLE)
    with gw.client(key=False) as client:
        assert client.get("/readyz").json()["reason"] == "key_unavailable"
        assert mcp(client, ROLE, token).status_code == 503


def test_br_064_oauth_refusal_served_when_its_audit_fails(gw: Gateway) -> None:
    """S16: the audit write of a refusal is exempt from BR-064 (BR-066)."""
    standard_tree(gw)
    conn = sqlite3.connect(gw.path)
    conn.execute(
        "CREATE TRIGGER t_audit_fails BEFORE INSERT ON audit "
        "BEGIN SELECT RAISE(ABORT, 'simulated outage'); END"
    )
    conn.commit()
    conn.close()
    with gw.client() as client:
        r = client.post(
            f"/o/{org()}/oauth/p/{ROLE}/revoke", data={"client_id": "unknown", "token": "x"}
        )
    assert (r.status_code, r.json()["error"]) == (401, "invalid_client")
