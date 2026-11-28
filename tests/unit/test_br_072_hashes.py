"""BR-072 — the database keeps no operator token in clear, only its digest (lot A)."""

from __future__ import annotations

import hashlib
import sqlite3

from tests.support import Gateway, mcp, org, standard_tree


def _dump(path: str) -> dict[str, str]:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            t: "\n".join(
                "\x1f".join("" if v is None else str(v) for v in row)
                for row in conn.execute(f"SELECT * FROM {t}")  # noqa: S608
            )
            for t in tables
        }
    finally:
        conn.close()


def test_br_072_no_operator_token_value_in_any_table(gw: Gateway) -> None:
    standard_tree(gw)
    issued = [gw.issue("mpx/cairn"), gw.issue("mpx/cairn/prod/dev"), gw.issue("mpx")]
    with gw.client() as client:
        for _, value in issued:
            mcp(client, "mpx/cairn/prod/dev", value)
    gw.ok("token", "revoke", issued[2][0])
    gw.ok("tree", "delete", "mpx/cairn/prod/dev", "--confirm", "mpx/cairn/prod/dev")
    gw.close()
    dump = _dump(str(gw.path))
    for _, value in issued:
        for table, text in dump.items():
            assert value not in text, table
            assert value.removeprefix("hg_") not in text, table
        digest = hashlib.sha256(value.encode()).hexdigest()
        assert digest in dump["operator_token"]
        assert all(digest not in text for t, text in dump.items() if t != "operator_token")
    raw = gw.path.read_bytes()
    for suffix in ("-wal",):
        wal = gw.path.with_name(gw.path.name + suffix)
        if wal.exists():
            raw += wal.read_bytes()
    for _, value in issued:
        assert value.encode() not in raw


def test_br_072_no_oauth_code_refresh_or_access_token_in_any_table(gw: Gateway) -> None:
    from tests.support import exchange, get_code, refresh

    standard_tree(gw)
    gw.preregister("mpx")
    path = "mpx/cairn/staging/dev"
    values: list[str] = []
    with gw.client() as client:
        code, verifier = get_code(gw, client, path)
        first = exchange(client, path, code, verifier).json()
        second = refresh(client, path, first["refresh_token"]).json()
        values += [
            code,
            verifier,
            first["access_token"],
            first["refresh_token"],
            second["access_token"],
            second["refresh_token"],
        ]
    gw.close()
    dump = _dump(str(gw.path))
    for value in values:
        for table, text in dump.items():
            assert value not in text, table
    for value in (code, first["refresh_token"], second["refresh_token"]):
        assert hashlib.sha256(value.encode()).hexdigest() in dump["auth_code"] + dump["refresh"]
    from tests.support import claims

    assert claims(first["access_token"])["jti"] in dump["access_jti"]
    raw = gw.path.read_bytes()
    wal = gw.path.with_name(gw.path.name + "-wal")
    if wal.exists():
        raw += wal.read_bytes()
    for value in values:
        assert value.encode() not in raw


def test_br_072_no_client_secret_column_nor_registration_secret(gw: Gateway) -> None:
    """S1: no column and no dynamic registration response carries a client secret."""
    standard_tree(gw)
    gw.ok("client", "dcr", "on")
    with gw.client() as client:
        r = client.post(
            f"/o/{org()}/oauth/p/mpx/cairn/staging/dev/register",
            json={
                "redirect_uris": ["http://localhost:1/cb"],
                "token_endpoint_auth_method": "client_secret_basic",
            },
        )
    assert r.status_code == 201
    assert not [k for k in r.json() if "secret" in k]
    conn = sqlite3.connect(gw.path)
    try:
        tables = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        columns = [f"{t}.{c[1]}" for t in tables for c in conn.execute(f"PRAGMA table_info({t})")]
    finally:
        conn.close()
    # BR-072 clarified (F-006): no *incoming* client secret; the upstream secrets are
    # encrypted envelopes (BR-087), the only secret column admitted.
    assert [c for c in columns if "secret" in c] == [
        "application.secret_enc",
        "application.secret_expires_at",
    ]
