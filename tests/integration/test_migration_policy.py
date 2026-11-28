"""F-015 AC-22 (§12): upgrade of a database at the previous schema (version 6). Placements keep
their rows; every sensitive placement lists nothing; a non-sensitive one lists its tools of class
at most `write`: 36 of the 61 tools of a fake upstream carrying the annotations measured on
`gitea-mcp-server:1.11.0` (36 `readOnlyHint: true`, 15 `readOnlyHint: false` without
`destructiveHint`, 10 `destructiveHint: true`; none is `write`).

The fixture is built by the CLI, then brought back to the exact version-6 schema (new tables
dropped, new columns dropped, new settings deleted, `user_version` 6), and checked against a
fresh version-6 database table by table before the gateway reopens it and migrates."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from hullgate import migrations
from hullgate.db import Database
from tests.fake_upstream import DESTRUCTIVE, READ, tool
from tests.policy_support import GITEA_WRITE
from tests.relay_bed import A, Bed, S, names
from tests.support import Gateway


def gitea_measured() -> list[dict[str, Any]]:
    return (
        [tool(f"read_{i:02d}", annotations=READ) for i in range(36)]
        + [tool(f"write_{i:02d}", annotations=GITEA_WRITE) for i in range(15)]
        + [tool(f"destroy_{i:02d}", annotations=DESTRUCTIVE) for i in range(10)]
    )


def _to_version_6(path: Path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN")
        # F-016 slice 4, migration 12: expiry metadata and guide state go first.
        conn.execute("ALTER TABLE connection DROP COLUMN refresh_expires_at")
        conn.execute("DELETE FROM setting WHERE key = 'connection_expiry_days'")
        conn.execute("DROP TABLE admin_setup")
        # F-020, migration 11: its column and its two settings go first
        conn.execute("ALTER TABLE consent_request DROP COLUMN approved_at")
        conn.execute(
            "DELETE FROM setting WHERE key IN ('token_rate_limit', 'connection_rate_limit')"
        )
        # F-014, migration 10: the organisation's identifier, its trigger and index next
        conn.execute("DROP TRIGGER node_public_id_permanent")
        conn.execute("DROP INDEX node_public_id")
        conn.execute("ALTER TABLE node DROP COLUMN public_id")
        # F-016 slice 2, migration 9: its columns go next
        conn.execute("ALTER TABLE application DROP COLUMN web_return")
        conn.execute("ALTER TABLE pending_auth DROP COLUMN redirect_uri")
        conn.execute("ALTER TABLE placement DROP COLUMN share_confirmed_by")
        conn.execute("ALTER TABLE placement DROP COLUMN share_confirmed_at")
        # F-016 lot A, migration 8: the console's tables go too
        for table in ("passkey", "admin_session", "enrolment", "webauthn_challenge"):
            conn.execute(f"DROP TABLE {table}")
        for table in ("administrator", "admin_attempt", "admin_allowlist"):
            conn.execute(f"DROP TABLE {table}")
        for table in ("placement_tool", "argument_rule", "pinned_definition", "tool_catalog"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("ALTER TABLE placement DROP COLUMN pin")
        conn.execute("ALTER TABLE placement DROP COLUMN mode")
        conn.execute("DELETE FROM setting WHERE key IN ('mode', 'pinning', 'response_cap')")
        conn.execute("PRAGMA user_version = 6")
        conn.execute("COMMIT")
    finally:
        conn.close()


def _schema(path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
            " WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        return [(t, n, tb, " ".join((s or "").split())) for t, n, tb, s in rows]
    finally:
        conn.close()


def test_migration_policy_upgrade_from_version_6(gw: Gateway, tmp_path: Path) -> None:
    bed = Bed(gw)
    for prefix, role in (("gm", A), ("gs", S)):
        bed.connection(prefix, role).tools = gitea_measured()
    gw.ok("connection", "place", "mpx/gm", "mpx/cairn/staging/rev")
    with bed.serve() as client:  # names inscribed under the old schema's tables too
        names(client, A, bed.token(A))
    gw.close()
    _to_version_6(gw.path)
    fresh = tmp_path / "fresh-v6.db"
    conn = sqlite3.connect(fresh, isolation_level=None)
    migrations.migrate(conn, migrations.MIGRATIONS[:6])
    conn.close()
    assert _schema(gw.path) == _schema(fresh)  # the fixture is the version-6 schema
    old = sqlite3.connect(gw.path)
    placements = old.execute("SELECT connection_id, role_id, created_at FROM placement").fetchall()
    old.close()
    assert len(placements) == 3

    gw.db = Database(str(gw.path))
    assert gw.db.open()  # the gateway's start migrates (BR-069)
    gw.reread_org()  # migration 10 drew the identifier of the rebuilt version-6 base (F-014)
    conn = sqlite3.connect(gw.path)
    try:
        assert migrations.user_version(conn) == migrations.SCHEMA_VERSION == 12
        after = conn.execute(
            "SELECT connection_id, role_id, created_at, mode, pin FROM placement"
        ).fetchall()
        assert sorted(after) == sorted((*p, None, None) for p in placements)
        assert conn.execute("SELECT count(*) FROM placement_tool").fetchone() == (0,)
    finally:
        conn.close()
    with bed.serve() as client:
        plain = names(client, A, bed.token(A))
        sensitive = names(client, S, bed.token(S))
    assert len(plain) == 36
    assert all(n.startswith("gm__read_") for n in plain)
    assert sensitive == []
