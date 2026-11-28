"""BR-052 — durations fixed at issuance, capped when sensitive (operator tokens, lot A)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.support import REGISTRY, Gateway, mcp, standard_tree

DAY = 86400


def _ttl(gw: Gateway, token_id: str) -> int:
    conn = sqlite3.connect(gw.path)
    try:
        row = conn.execute(
            "SELECT expires_at - issued_at FROM operator_token WHERE id = ?", (token_id,)
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def test_br_052_default_duration_is_thirty_days(gw: Gateway) -> None:
    standard_tree(gw)
    token_id, _ = gw.issue("mpx/cairn/staging/dev")
    assert _ttl(gw, token_id) == 30 * DAY


def test_br_052_inherited_setting_and_explicit_duration(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("settings", "set", "mpx/cairn", "token_duration", "12h")
    inherited, _ = gw.issue("mpx/cairn/staging/dev")
    explicit, _ = gw.issue("mpx/cairn/staging/dev", "x", "--duration", "3d")
    assert _ttl(gw, inherited) == 12 * 3600
    assert _ttl(gw, explicit) == 3 * DAY


def test_br_052_sensitive_cap_of_seven_days(gw: Gateway) -> None:
    standard_tree(gw)
    default, _ = gw.issue("mpx/cairn/prod/dev")
    asked, _ = gw.issue("mpx/cairn/prod/dev", "x", "--duration", "60d")
    short, _ = gw.issue("mpx/cairn/prod/dev", "x", "--duration", "2d")
    assert (_ttl(gw, default), _ttl(gw, asked), _ttl(gw, short)) == (7 * DAY, 7 * DAY, 2 * DAY)
    out = gw.ok("token", "create", "--path", "mpx/cairn/prod/dev", "--label", "y")
    assert "capped at 7 days" in out
    REGISTRY.secrets.append(out.strip().splitlines()[-1])


def test_br_052_expiry_fixed_at_issuance(gw: Gateway) -> None:
    standard_tree(gw)
    token_id, _ = gw.issue("mpx/cairn/staging/dev")
    gw.ok("settings", "set", "/", "token_duration", "1h")
    assert _ttl(gw, token_id) == 30 * DAY
    listed = json.loads(gw.ok("token", "list", "--json"))
    assert listed[0]["state"] == "active"


@pytest.mark.parametrize("duration", ["0d", "366d", "1w", "abc", "-1d"])
def test_br_052_invalid_duration_refused(gw: Gateway, duration: str) -> None:
    standard_tree(gw)
    result = gw.cli("token", "create", "--path", "mpx", "--label", "x", f"--duration={duration}")
    assert result.code == 2
    assert "hg_" not in result.out


def test_br_052_expired_token_refused_at_endpoint(gw: Gateway) -> None:
    standard_tree(gw)
    token_id, value = gw.issue("mpx/cairn/staging/dev", "court", "--duration", "1h")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev", value).status_code == 200
        gw.clock.advance(3600)
        response = mcp(client, "mpx/cairn/staging/dev", value)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_token"}
    listed = {t["id"]: t["state"] for t in json.loads(gw.ok("token", "list", "--json"))}
    assert listed[token_id] == "expired"


def _lineage_expiry(gw: Gateway) -> list[int]:
    conn = sqlite3.connect(gw.path)
    try:
        return [
            int(r[0] - r[1]) for r in conn.execute("SELECT expires_at, created_at FROM lineage")
        ]
    finally:
        conn.close()


def test_br_052_oauth_access_one_hour_lineage_from_setting(gw: Gateway) -> None:
    from tests.support import claims, grant

    standard_tree(gw)
    gw.preregister("mpx")
    gw.ok("settings", "set", "mpx/cairn", "token_duration", "3d")
    with gw.client() as client:
        g = grant(gw, client, "mpx/cairn/staging/dev")
        sensitive = grant(gw, client, "mpx/cairn/prod/dev")
    c = claims(g["access_token"])
    assert c["exp"] - c["iat"] == 3600
    assert g["expires_in"] == 3600
    gw.ok("settings", "set", "mpx/cairn", "token_duration", "60d")
    gw.ok("settings", "set", "mpx/cairn/prod", "token_duration", "90d")
    assert sorted(_lineage_expiry(gw)) == [3 * 86400, 3 * 86400]  # fixed at issuance
    assert (
        claims(sensitive["access_token"])["exp"] - claims(sensitive["access_token"])["iat"] == 3600
    )


def test_br_052_oauth_lineage_capped_at_seven_days_when_sensitive(gw: Gateway) -> None:
    from tests.support import grant

    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        grant(gw, client, "mpx/cairn/prod/dev")
        grant(gw, client, "mpx/cairn/staging/dev")
    assert sorted(_lineage_expiry(gw)) == [7 * 86400, 30 * 86400]


def test_br_052_rotation_does_not_extend_lineage(gw: Gateway) -> None:
    from tests.support import grant, refresh

    standard_tree(gw)
    gw.preregister("mpx")
    gw.ok("settings", "set", "mpx", "token_duration", "1d")
    path = "mpx/cairn/staging/dev"
    with gw.client() as client:
        g = grant(gw, client, path)
        gw.clock.advance(20 * 3600)
        n = refresh(client, path, g["refresh_token"])
        assert n.status_code == 200
        gw.clock.advance(5 * 3600)
        late = refresh(client, path, n.json()["refresh_token"])
        assert (late.status_code, late.json()["error"]) == (400, "invalid_grant")
        assert mcp(client, path, n.json()["access_token"]).status_code == 401  # > 1 h


def test_br_052_marking_sensitive_does_not_shorten_existing_tokens(gw: Gateway) -> None:
    """S3: the 7-day cap is evaluated at issuance."""
    gw.build(
        ("/", "client", "MPX"), ("mpx", "project", "Cairn"), ("mpx/cairn", "environment", "Prod")
    )
    gw.issue("mpx/cairn/prod/principal", "long", "--duration", "30d")
    before = [t["expires_at"] for t in json.loads(gw.ok("token", "list", "--json"))]
    gw.ok("tree", "sensitive", "mpx/cairn/prod", "on")
    after = [t["expires_at"] for t in json.loads(gw.ok("token", "list", "--json"))]
    assert before == after
    capped = json.loads(
        gw.ok(
            "token",
            "create",
            "--path",
            "mpx/cairn/prod/principal",
            "--label",
            "neuf",
            "--duration",
            "30d",
            "--json",
        )
    )
    REGISTRY.secrets.append(capped["value"])
    assert capped["expires_at"] != after[0]
