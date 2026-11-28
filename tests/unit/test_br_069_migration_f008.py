"""BR-069, BR-038/BR-039 (F-008 lot A, AC-02): migration 5 (announced "4" by the spec, a number
taken by F-006 lot B) applies to an F-006 database that carries services, connections and
placements; they stay intact, the services have `upstream_type` NULL; a backup that contains
`tool_name` restores identically."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hullgate import backup, migrations

F006 = """
INSERT INTO node (id, parent_id, level, slug, name, created_at) VALUES
    ('c1', 'org', 'client', 'mpx', 'MPX', 0),
    ('p1', 'c1', 'project', 'cairn', 'Cairn', 0),
    ('e1', 'p1', 'env', 'staging', 'Staging', 0),
    ('r1', 'e1', 'role', 'dev', 'Dev', 0);
INSERT INTO service (id, model, base_url, created_at)
    VALUES ('gitea', 'gitea', 'https://git.example.test', 0);
INSERT INTO connection (id, client_node_id, name, prefix, service_id, kind, state, tokens_enc,
    created_at) VALUES ('k1', 'c1', 'gitea-dev', 'gd', 'gitea', 'fourni', 'active', x'01', 0);
INSERT INTO placement (connection_id, role_id, created_at) VALUES ('k1', 'r1', 0);
"""


# F-016 lot A, migration 8: the console's tables, empty after a restore of an older backup.
ADMIN_TABLES = {
    "administrator": 0, "passkey": 0, "admin_session": 0, "enrolment": 0,
    "webauthn_challenge": 0, "admin_attempt": 0, "admin_allowlist": 0,
}  # fmt: skip


def _f006(path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path, isolation_level=None)
    migrations.migrate(conn, migrations.MIGRATIONS[:4])
    assert migrations.user_version(conn) == 4
    conn.executescript(F006)
    rows = conn.execute("SELECT * FROM connection").fetchall()
    rows += conn.execute("SELECT * FROM placement").fetchall()
    conn.close()
    return rows


def test_br_069_migration_5_keeps_f006_data(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    before = _f006(path)
    # Migration 5 alone (migration 6, F-010, rewrites the stored values: test_br_132_*).
    conn = sqlite3.connect(path, isolation_level=None)
    migrations.migrate(conn, migrations.MIGRATIONS[:5])
    conn.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        after = conn.execute("SELECT * FROM connection").fetchall()
        after += conn.execute("SELECT * FROM placement").fetchall()
        assert after == before
        assert conn.execute(
            "SELECT id, base_url, upstream_type, mcp_url, mcp_internal FROM service"
        ).fetchall() == [("gitea", "https://git.example.test", None, None, 0)]
        assert conn.execute("SELECT COUNT(*) FROM tool_name").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_br_069_tool_name_constraints(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    _f006(path)
    conn = sqlite3.connect(path, isolation_level=None)
    migrations.migrate(conn)
    conn.execute("INSERT INTO tool_name VALUES ('k1', 'read', 'gd__read', 0)")
    with pytest.raises(sqlite3.IntegrityError):  # (connection, public name) unique
        conn.execute("INSERT INTO tool_name VALUES ('k1', 'read2', 'gd__read', 0)")
    with pytest.raises(sqlite3.IntegrityError):  # (connection, upstream name) unique
        conn.execute("INSERT INTO tool_name VALUES ('k1', 'read', 'gd__other', 0)")
    with pytest.raises(sqlite3.IntegrityError):  # closed upstream type
        conn.execute("UPDATE service SET upstream_type = 'api-rest'")
    conn.close()


def test_br_039_backup_with_tool_name_restores(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    _f006(path)
    conn = sqlite3.connect(path, isolation_level=None)
    migrations.migrate(conn, migrations.MIGRATIONS[:5])
    conn.execute("INSERT INTO tool_name VALUES ('k1', 'read', 'gd__read', 0)")
    conn.execute(
        "UPDATE service SET upstream_type = 'mcp-distant', mcp_url = 'https://m.example.test/mcp'"
    )
    conn.close()
    snap = tmp_path / "snap.db"
    counts = backup.snapshot(str(path), str(snap))
    restored = backup.restore(str(snap), str(tmp_path / "restored.db"))
    # the restore migrates forward: F-015's migration 7 adds three settings and four tables
    added = {"tool_catalog": 0, "placement_tool": 0, "argument_rule": 0, "pinned_definition": 0}
    added |= ADMIN_TABLES  # F-016 lot A, migration 8
    # F-020's migration 11 adds the two tool-call limits
    assert restored == {
        **counts,
        **added,
        "setting": counts["setting"] + 3 + 2 + 1,
        "admin_setup": 1,
    }
    assert restored["tool_name"] == 1
