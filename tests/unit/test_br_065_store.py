"""BR-065 — nothing is issued without being recorded (issuance and audit, one transaction)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.support import Gateway, standard_tree


def _break_audit(gw: Gateway) -> None:
    """Every audit write fails: the act's transaction must be rolled back whole."""
    conn = sqlite3.connect(gw.path)
    conn.execute(
        "CREATE TRIGGER test_audit_fails BEFORE INSERT ON audit "
        "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
    )
    conn.commit()
    conn.close()


def _rows(gw: Gateway, table: str) -> int:
    conn = sqlite3.connect(gw.path)
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608
    finally:
        conn.close()


def test_br_065_token_not_stored_nor_printed_when_audit_fails(gw: Gateway) -> None:
    standard_tree(gw)
    _break_audit(gw)
    result = gw.cli("token", "create", "--path", "mpx/cairn/staging/dev", "--label", "x")
    assert result.code == 1
    assert "hg_" not in result.out + result.err
    assert result.out == ""
    assert _rows(gw, "operator_token") == 0


@pytest.mark.parametrize(
    "command",
    [
        ("tree", "add", "mpx", "project", "Autre"),
        ("tree", "rename", "mpx", "Nouveau", "--slug", "nouveau"),
        ("tree", "delete", "mpx/cairn/staging", "--confirm", "mpx/cairn/staging"),
        ("tree", "sensitive", "mpx/cairn/staging", "on"),
        ("settings", "set", "mpx", "token_duration", "3d"),
    ],
)
def test_br_065_tree_change_rolled_back_when_audit_fails(
    gw: Gateway, command: tuple[str, ...]
) -> None:
    standard_tree(gw)
    conn = sqlite3.connect(gw.path)
    before = conn.execute("SELECT * FROM node ORDER BY id").fetchall()
    settings = conn.execute("SELECT * FROM setting ORDER BY node_id, key").fetchall()
    conn.close()
    _break_audit(gw)
    assert gw.cli(*command).code == 1
    conn = sqlite3.connect(gw.path)
    assert conn.execute("SELECT * FROM node ORDER BY id").fetchall() == before
    assert conn.execute("SELECT * FROM setting ORDER BY node_id, key").fetchall() == settings
    conn.close()


def test_br_065_revocation_rolled_back_when_audit_fails(gw: Gateway) -> None:
    standard_tree(gw)
    token_id, _ = gw.issue("mpx")
    _break_audit(gw)
    assert gw.cli("token", "revoke", token_id).code == 1
    conn = sqlite3.connect(gw.path)
    revoked = conn.execute("SELECT revoked FROM operator_token WHERE id = ?", (token_id,))
    assert revoked.fetchone() == (0,)
    conn.close()


def test_br_065_issuance_writes_token_and_audit_together(gw: Gateway) -> None:
    standard_tree(gw)
    before = _rows(gw, "audit")
    token_id, _ = gw.issue("mpx")
    assert _rows(gw, "operator_token") == 1
    entries = gw.audit()[before:]
    assert [(e["action"], e["result"]) for e in entries] == [("token.issue", "ok")]
    assert token_id in entries[0]["after"]


def test_br_065_oauth_issuance_rolled_back_when_audit_fails(gw: Gateway) -> None:
    from tests.support import exchange, get_code

    standard_tree(gw)
    gw.preregister("mpx")
    path = "mpx/cairn/staging/dev"
    with gw.client() as client:
        code, verifier = get_code(gw, client, path)
        _break_audit(gw)
        r = exchange(client, path, code, verifier)
        assert r.status_code == 503
        assert "access_token" not in r.text
        assert (_rows(gw, "refresh"), _rows(gw, "access_jti")) == (0, 0)
        conn = sqlite3.connect(gw.path)
        try:
            assert conn.execute("SELECT used FROM auth_code").fetchone() == (0,)
        finally:
            conn.close()


def test_br_065_code_not_issued_when_audit_fails(gw: Gateway) -> None:
    from tests.support import authorize_params, consent, pkce, request_id, start_authorization

    standard_tree(gw)
    gw.preregister("mpx")
    path = "mpx/cairn/staging/dev"
    with gw.client() as client:
        _, challenge = pkce()
        req = request_id(start_authorization(client, path, authorize_params(path, challenge)))
        _break_audit(gw)
        r = consent(gw, client, path, req)
    assert r.status_code == 503
    assert "location" not in r.headers
    assert (_rows(gw, "auth_code"), _rows(gw, "lineage")) == (0, 0)
