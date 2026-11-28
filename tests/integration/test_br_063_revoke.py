"""BR-063 — revocation follows RFC 7009 (N13, R-05)."""

from __future__ import annotations

import secrets

from tests.support import Gateway, grant, mcp, refresh, revoke, standard_tree

ROLE = "mpx/cairn/staging/dev"
CID = "client-test"


def _setup(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    gw.preregister("mpx", "other-client")


def test_br_063_missing_token_is_invalid_request(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        r = revoke(client, ROLE, {"client_id": CID})
    assert (r.status_code, r.json()["error"]) == (400, "invalid_request")


def test_br_063_unknown_client_is_invalid_client_and_revokes_nothing(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        g = grant(gw, client, ROLE)
        r = revoke(client, ROLE, {"token": g["access_token"], "client_id": "unknown"})
        assert (r.status_code, r.json()["error"]) == (401, "invalid_client")
        assert mcp(client, ROLE, g["access_token"]).status_code == 200


def test_br_063_unknown_malformed_or_foreign_token_is_200_without_effect(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        g = grant(gw, client, ROLE)
        for token in (
            secrets.token_urlsafe(48),
            "not.a.jwt",
            g["access_token"],
            g["refresh_token"],
        ):
            r = revoke(client, ROLE, {"token": token, "client_id": "other-client"})
            assert r.status_code == 200
        assert mcp(client, ROLE, g["access_token"]).status_code == 200
        assert refresh(client, ROLE, g["refresh_token"]).status_code == 200


def test_br_063_refresh_revocation_kills_the_lineage(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        g = grant(gw, client, ROLE)
        r = revoke(
            client,
            ROLE,
            {"token": g["refresh_token"], "token_type_hint": "refresh_token", "client_id": CID},
        )
        assert r.status_code == 200
        assert mcp(client, ROLE, g["access_token"]).status_code == 401
        assert refresh(client, ROLE, g["refresh_token"]).json()["error"] == "invalid_grant"
        again = revoke(client, ROLE, {"token": g["refresh_token"], "client_id": CID})
        assert again.status_code == 200  # already revoked


def test_br_063_access_token_revoked_by_its_jti(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        g = grant(gw, client, ROLE)
        r = revoke(
            client,
            ROLE,
            {"token": g["access_token"], "token_type_hint": "refresh_token", "client_id": CID},
        )
        assert r.status_code == 200  # the hint is only a hint
        assert mcp(client, ROLE, g["access_token"]).status_code == 401
        n = refresh(client, ROLE, g["refresh_token"])  # the lineage lives: only this jti falls
        assert n.status_code == 200
        assert mcp(client, ROLE, n.json()["access_token"]).status_code == 200
    assert any(e["action"] == "oauth.access.revoke" for e in gw.audit())


def test_br_063_client_secret_ignored_and_removed_client_unknown(gw: Gateway) -> None:
    """S1, S21: secret ignored; client removed: `invalid_client`, refresh token included."""
    _setup(gw)
    with gw.client() as client:
        g = grant(gw, client, ROLE)
        r = revoke(
            client,
            ROLE,
            {"token": g["access_token"], "client_id": CID, "client_secret": "quelconque"},
        )
        assert r.status_code == 200
        assert mcp(client, ROLE, g["access_token"]).status_code == 401
        gw.ok("client", "remove", CID)
        gone = revoke(client, ROLE, {"token": g["refresh_token"], "client_id": CID})
        assert (gone.status_code, gone.json()["error"]) == (401, "invalid_client")
        renew = refresh(client, ROLE, g["refresh_token"])
        assert (renew.status_code, renew.json()["error"]) == (401, "invalid_client")
