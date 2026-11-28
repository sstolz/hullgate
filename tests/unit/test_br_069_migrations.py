"""BR-069 — the schema migrates forward at startup, or the gateway is not ready."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hullgate import cli, migrations
from hullgate.db import Database
from tests.support import Gateway


def _version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_br_069_fresh_database_migrated_on_open(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    db = Database(str(path))
    assert db.open()
    assert db.readiness().ok
    db.close()
    assert _version(path) == migrations.SCHEMA_VERSION
    assert {
        "node",
        "setting",
        "operator_token",
        "client",
        "lineage",
        "auth_code",
        "refresh",
        "access_jti",
        "audit",
    } <= _tables(path)


def test_br_069_reopen_applies_nothing_twice(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    for _ in range(3):
        db = Database(str(path))
        assert db.open()
        db.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM node WHERE level = 'org'").fetchone() == (1,)
    conn.close()


def test_br_069_migrations_applied_in_order_each_in_own_transaction(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    steps = (
        migrations.Migration(1, ("CREATE TABLE a (x INTEGER)",)),
        migrations.Migration(2, ("INSERT INTO a VALUES (2)",)),
        migrations.Migration(3, ("INSERT INTO a VALUES (3)", "INSERT INTO missing VALUES (1)")),
    )
    with pytest.raises(migrations.SchemaError) as err:
        migrations.migrate(conn, steps)
    assert err.value.reason == "schema_error"
    assert migrations.user_version(conn) == 2
    assert conn.execute("SELECT x FROM a").fetchall() == [(2,)]  # migration 3 rolled back whole
    conn.close()


def test_br_069_failed_migration_leaves_gateway_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    last = migrations.SCHEMA_VERSION
    failing = migrations.Migration(last + 1, ("SELECT * FROM nowhere",))
    broken = (*migrations.MIGRATIONS, failing)
    monkeypatch.setattr(migrations, "MIGRATIONS", broken)
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", last + 1)
    monkeypatch.setattr(migrations.migrate, "__defaults__", (broken,))
    db = Database(str(tmp_path / "hullgate.db"))
    assert db.open()
    assert db.readiness().reason == "schema_error"
    assert not db.readiness().ok
    db.close()
    assert _version(tmp_path / "hullgate.db") == last


def test_br_069_newer_database_untouched_and_not_ready(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE future (x INTEGER)")
    conn.execute(f"PRAGMA user_version = {migrations.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    db = Database(str(path))
    assert db.open()
    state = db.readiness()
    db.close()
    assert (state.ok, state.reason) == (False, "schema_too_new")
    assert _tables(path) == {"future"}
    assert _version(path) == migrations.SCHEMA_VERSION + 1
    assert path.read_bytes()[:100] == before[:100]


def test_br_069_cli_refuses_database_of_other_version(gw: Gateway) -> None:
    conn = sqlite3.connect(gw.path)
    conn.execute(f"PRAGMA user_version = {migrations.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    result = gw.cli("tree", "add", "/", "client", "Acme")
    assert result.code == 1
    assert "version" in result.err


def test_br_069_cli_does_not_create_missing_database(tmp_path: Path) -> None:
    env = {"PUBLIC_URL": "https://x.test", "ADMIN_URL": "http://localhost:1"}
    env["DATA_DIR"] = str(tmp_path)
    assert cli.run(["tree", "show"], env) == 1
    assert not (tmp_path / "hullgate.db").exists()


def test_br_069_version_reread_under_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open that read 0 before another one migrated does not replay migration 1."""
    path = tmp_path / "hullgate.db"
    late = sqlite3.connect(path, isolation_level=None)
    real = migrations.user_version
    calls = {"n": 0}

    def stale_then_real(conn: sqlite3.Connection) -> int:
        calls["n"] += 1
        if calls["n"] == 1:  # read outside the lock, before the concurrent migration
            other = Database(str(path))
            assert other.open()
            other.close()
            return 0
        return real(conn)

    monkeypatch.setattr(migrations, "user_version", stale_then_real)
    assert migrations.migrate(late) == migrations.SCHEMA_VERSION
    late.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM node WHERE level = 'org'").fetchone() == (1,)
    conn.close()


def test_br_069_version_1_database_migrates_forward_to_current(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    conn = sqlite3.connect(path, isolation_level=None)
    migrations.migrate(conn, migrations.MIGRATIONS[:1])
    conn.execute(
        "INSERT INTO node (id, parent_id, level, slug, name, created_at)"
        " VALUES ('c1', 'org', 'client', 'mpx', 'MPX', 0)"
    )
    conn.close()
    db = Database(str(path))
    assert db.open()
    assert db.readiness().ok
    db.close()
    assert _version(path) == migrations.SCHEMA_VERSION == 12
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"consent_request", "consent_failure", "totp_used", "instance_flag"} <= tables
        # F-006 (migration 3): connection library
        assert {"service", "application", "connection", "placement", "pending_auth"} <= tables
        assert "vault_meta" in tables
        assert conn.execute("SELECT slug FROM node WHERE id = 'c1'").fetchone() == ("mpx",)
        assert conn.execute("SELECT value FROM instance_flag WHERE key='dcr'").fetchone() == (
            "off",
        )
    finally:
        conn.close()


def test_br_069_version_2_database_migrates_to_3_keeping_data(tmp_path: Path) -> None:
    """F-006: migration 3 is added without touching F-005's data (key of `client`)."""
    path = tmp_path / "hullgate.db"
    conn = sqlite3.connect(path, isolation_level=None)
    migrations.migrate(conn, migrations.MIGRATIONS[:2])
    conn.execute(
        "INSERT INTO client (client_id, kind, node_id, created_at) VALUES ('c', 'cimd', NULL, 0)"
    )
    conn.close()
    db = Database(str(path))
    assert db.open()
    assert db.readiness().ok
    db.close()
    conn = sqlite3.connect(path)
    try:
        assert _version(path) == migrations.SCHEMA_VERSION
        assert conn.execute("SELECT client_id FROM client").fetchall() == [("c",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_br_069_version_3_database_migrates_to_4_keeping_connections(tmp_path: Path) -> None:
    """F-006 lot B: migration 4 adds the authentication method of an application (an application
    declared with a secret moves to `client_secret_basic`), the reason of the last outcome and the
    `key_alert` table, without touching the connections of lot A."""
    path = tmp_path / "hullgate.db"
    conn = sqlite3.connect(path, isolation_level=None)
    migrations.migrate(conn, migrations.MIGRATIONS[:3])
    conn.executescript(
        "INSERT INTO service (id, model, base_url, created_at)"
        " VALUES ('s', 'gitea', 'https://s.example.test', 0);"
        "INSERT INTO application (id, service_id, mode, client_id, redirect_uri, secret_enc,"
        " created_at) VALUES ('a', 's', 'declaree', 'c', 'http://127.0.0.1/oauth/retour',"
        " x'01', 0);"
        "INSERT INTO application (id, service_id, mode, client_id, redirect_uri, created_at)"
        " VALUES ('b', 's', 'declaree', 'c', 'http://127.0.0.1/oauth/retour', 0);"
    )
    conn.close()
    db = Database(str(path))
    assert db.open()
    assert db.readiness().ok
    db.close()
    conn = sqlite3.connect(path)
    try:
        assert _version(path) == migrations.SCHEMA_VERSION
        methods = conn.execute("SELECT id, auth_method FROM application ORDER BY id").fetchall()
        assert methods == [("a", "client_secret_basic"), ("b", "none")]
        assert conn.execute("SELECT COUNT(*) FROM key_alert").fetchone() == (0,)
        assert "last_reason" in [r[1] for r in conn.execute("PRAGMA table_info(connection)")]
    finally:
        conn.close()


def _f014_and_f020(path: Path) -> tuple[int, str | None, bool, int]:
    """(version, the organisation's identifier, `approved_at` present, F-020 settings rows)."""
    conn = sqlite3.connect(path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        columns = {r[1] for r in conn.execute("PRAGMA table_info(node)")}
        public_id = (
            conn.execute("SELECT public_id FROM node WHERE id = 'org'").fetchone()[0]
            if "public_id" in columns
            else None
        )
        approved = {r[1] for r in conn.execute("PRAGMA table_info(consent_request)")}
        seeded = conn.execute(
            "SELECT count(*) FROM setting WHERE node_id = 'org'"
            " AND key IN ('token_rate_limit', 'connection_rate_limit')"
        ).fetchone()[0]
    finally:
        conn.close()
    return version, public_id, "approved_at" in approved, int(seeded)


def test_br_069_migrations_10_and_11_both_run_in_order() -> None:
    """F-014's 10 and F-020's 11 are both in the sequence, 10 first, with no gap."""
    versions = [m.version for m in migrations.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert versions[9:11] == [10, 11]


@pytest.mark.parametrize("start", [0, 9])
def test_br_069_fresh_and_version_9_databases_get_10_then_11(tmp_path: Path, start: int) -> None:
    """A fresh database and one at 9 (before F-014 and F-020) both end at 11 with F-014's
    identifier drawn and F-020's column and seed rows."""
    path = tmp_path / "hullgate.db"
    if start:
        conn = sqlite3.connect(path, isolation_level=None)
        migrations.migrate(conn, migrations.MIGRATIONS[:start])
        conn.close()
        assert _f014_and_f020(path) == (9, None, False, 0)
    db = Database(str(path))
    assert db.open()
    db.close()
    version, public_id, approved_at, seeded = _f014_and_f020(path)
    assert version == migrations.SCHEMA_VERSION == 12
    assert public_id is not None
    assert len(public_id) == 12
    assert approved_at
    assert seeded == 2


def test_br_069_slice4_preserves_live_challenges_and_seeds_only_once(tmp_path: Path) -> None:
    path = tmp_path / "slice4.db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        migrations.migrate(conn, migrations.MIGRATIONS[:11])
        conn.execute(
            "INSERT INTO webauthn_challenge VALUES"
            " ('live', 'sign_in', NULL, NULL, '192.0.2.1', 2000000000)"
        )
        migrations.migrate(conn)
        assert conn.execute("SELECT challenge, purpose FROM webauthn_challenge").fetchall() == [
            ("live", "sign_in")
        ]
        assert conn.execute(
            "SELECT value FROM setting WHERE key = 'connection_expiry_days'"
        ).fetchall() == [("7",)]
        conn.execute("UPDATE admin_setup SET dismissed = 1")
        migrations.migrate(conn)
        assert conn.execute("SELECT dismissed FROM admin_setup").fetchone() == (1,)
        assert "refresh_expires_at" in {r[1] for r in conn.execute("PRAGMA table_info(connection)")}
        conn.execute(
            "INSERT INTO webauthn_challenge VALUES"
            " ('barrier', 'barrier', NULL, 'pending', '192.0.2.1', 2000000000)"
        )
    finally:
        conn.close()
