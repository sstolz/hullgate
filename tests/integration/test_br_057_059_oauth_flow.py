"""BR-057 (PKCE S256 required) and BR-059 (single-use code, rotation, reuse = revocation)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from tests.support import (
    Gateway,
    authorize_params,
    claims,
    code_from,
    consent,
    exchange,
    get_code,
    grant,
    mcp,
    pkce,
    refresh,
    request_id,
    standard_tree,
    start_authorization,
)

ROLE = "mpx/cairn/staging/dev"


def _setup(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")


def _error(response_location: str) -> str | None:
    return parse_qs(urlsplit(response_location).query).get("error", [None])[0]


def _counts(gw: Gateway) -> tuple[int, int, int]:
    conn = gw.connect()
    try:
        a, r, j = (
            int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # noqa: S608
            for t in ("auth_code", "refresh", "access_jti")
        )
        return a, r, j
    finally:
        conn.close()


def test_br_057_pkce_plain_or_absent_refused_without_code(gw: Gateway) -> None:
    _setup(gw)
    verifier, challenge = pkce()
    with gw.client() as client:
        for extra in (
            {"code_challenge_method": "plain", "code_challenge": verifier},
            {"code_challenge": "", "code_challenge_method": ""},
            {"code_challenge_method": ""},
        ):
            r = start_authorization(client, ROLE, authorize_params(ROLE, challenge, **extra))
            assert r.status_code == 302
            location = r.headers["location"]
            assert "code=" not in location
            assert "/consent" not in location
            assert _error(location) == "invalid_request"
    assert _counts(gw) == (0, 0, 0)


def test_br_057_wrong_verifier_is_invalid_grant(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        code, verifier = get_code(gw, client, ROLE)
        bad = exchange(client, ROLE, code, "x" * 64)
        assert (bad.status_code, bad.json()["error"]) == (400, "invalid_grant")
        assert "access_token" not in bad.json()
        good = exchange(client, ROLE, code, verifier)  # not consumed by the failure
        assert good.status_code == 200


def test_br_059_code_single_use_and_reuse_revokes_lineage(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        code, verifier = get_code(gw, client, ROLE)
        first = exchange(client, ROLE, code, verifier)
        assert first.status_code == 200
        access, rt = first.json()["access_token"], first.json()["refresh_token"]
        assert mcp(client, ROLE, access).status_code == 200
        again = exchange(client, ROLE, code, verifier)
        assert (again.status_code, again.json()["error"]) == (400, "invalid_grant")
        assert mcp(client, ROLE, access).status_code == 401
        assert refresh(client, ROLE, rt).json()["error"] == "invalid_grant"
    reasons = [e["reason"] for e in gw.audit() if e["action"] == "lineage.revoke"]
    assert "reuse_detected" in reasons


def test_br_059_rotation_invalidates_previous_tokens(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        g = grant(gw, client, ROLE)
        r1 = refresh(client, ROLE, g["refresh_token"])
        assert r1.status_code == 200
        n = r1.json()
        assert n["refresh_token"] != g["refresh_token"]
        c0, c1 = claims(g["access_token"]), claims(n["access_token"])
        assert c1["jti"] != c0["jti"]
        assert c1["aud"] == c0["aud"]
        assert mcp(client, ROLE, g["access_token"]).status_code == 401
        assert mcp(client, ROLE, n["access_token"]).status_code == 200
        # reuse of the old refresh token: the whole lineage falls
        old = refresh(client, ROLE, g["refresh_token"])
        assert (old.status_code, old.json()["error"]) == (400, "invalid_grant")
        assert mcp(client, ROLE, n["access_token"]).status_code == 401
        assert refresh(client, ROLE, n["refresh_token"]).status_code == 400


def test_br_059_refresh_cannot_widen_scope_or_change_client(gw: Gateway) -> None:
    _setup(gw)
    gw.preregister("mpx", "other-client")
    with gw.client() as client:
        verifier, challenge = pkce()
        params = authorize_params(ROLE, challenge, scope="mcp")
        first = start_authorization(client, ROLE, params)
        code = code_from(consent(gw, client, ROLE, request_id(first)))
        g = exchange(client, ROLE, code, verifier).json()
        wide = refresh(client, ROLE, g["refresh_token"], scope="mcp offline_access")
        assert (wide.status_code, wide.json()["error"]) == (400, "invalid_scope")
        other = refresh(client, ROLE, g["refresh_token"], client_id="other-client")
        assert (other.status_code, other.json()["error"]) == (400, "invalid_grant")
        ok = refresh(client, ROLE, g["refresh_token"])  # neither of them consumed it
        assert ok.status_code == 200


def test_br_059_code_bound_to_client_and_redirect(gw: Gateway) -> None:
    _setup(gw)
    gw.preregister("mpx", "other-client")
    with gw.client() as client:
        code, verifier = get_code(gw, client, ROLE)
        other = exchange(client, ROLE, code, verifier, client_id="other-client")
        assert other.json()["error"] == "invalid_grant"
        moved = exchange(client, ROLE, code, verifier, redirect="http://localhost:4567/other")
        assert moved.json()["error"] == "invalid_grant"
        assert exchange(client, ROLE, code, verifier).status_code == 200


def test_br_059_code_expires_after_120_s(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        code, verifier = get_code(gw, client, ROLE)
        gw.clock.advance(121)
        late = exchange(client, ROLE, code, verifier)
        assert (late.status_code, late.json()["error"]) == (400, "invalid_grant")


def test_br_059_refresh_issued_without_offline_access(gw: Gateway) -> None:
    """S22: a refresh token is issued at every authorization, `offline_access` or not."""
    _setup(gw)
    with gw.client() as client:
        verifier, challenge = pkce()
        first = start_authorization(client, ROLE, authorize_params(ROLE, challenge, scope="mcp"))
        code = code_from(consent(gw, client, ROLE, request_id(first)))
        body = exchange(client, ROLE, code, verifier).json()
        assert body["scope"] == "mcp"
        assert body["refresh_token"]
        assert refresh(client, ROLE, body["refresh_token"]).status_code == 200


def test_br_059_concurrent_exchange_is_reuse_never_503(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m2 (review 411): a second concurrent exchange of the same code cannot slip in between.

    Another process tries to consume the code between the read and the write of the exchange: it
    finds the write lock taken; the exchange succeeds; a second exchange is a reuse.
    """
    import sqlite3

    from hullgate import oauth

    _setup(gw)
    seen: list[str] = []
    real = oauth.s256

    def concurrent(verifier: str) -> str:
        other = sqlite3.connect(gw.path, timeout=0.2, isolation_level=None)
        try:
            other.execute("UPDATE auth_code SET used = 1")
            seen.append("written")
        except sqlite3.OperationalError:
            seen.append("locked")
        finally:
            other.close()
        return real(verifier)

    with gw.client() as client:
        code, verifier = get_code(gw, client, ROLE)
        monkeypatch.setattr(oauth, "s256", concurrent)
        first = exchange(client, ROLE, code, verifier)
        monkeypatch.setattr(oauth, "s256", real)
        second = exchange(client, ROLE, code, verifier)
    assert seen == ["locked"]
    assert first.status_code == 200
    assert (second.status_code, second.json()["error"]) == (400, "invalid_grant")
    reasons = [e["reason"] for e in gw.audit() if e["action"] == "lineage.revoke"]
    assert reasons == ["reuse_detected"]
