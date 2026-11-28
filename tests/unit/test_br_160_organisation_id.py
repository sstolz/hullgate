"""BR-160 — the organisation has one permanent, random public identifier (F-014 AC-01, AC-02).

Format on a fresh database and on one at the previous schema; two fresh databases differ; stable
across reopen, restart, backup and restore; the store refuses any update of a value once set,
while the organisation's other columns stay editable.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from hullgate import backup, migrations
from hullgate.db import Database
from tests.support import Gateway

ID_RE = re.compile(r"^[a-z2-7]{12}$")


def _public_id(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("SELECT public_id FROM node WHERE id = 'org'").fetchone()[0])
    finally:
        conn.close()


def _root_line(gw: Gateway) -> str:
    return gw.ok("tree", "show", "/").splitlines()[0]


def test_br_160_fresh_database_has_a_well_formed_identifier(gw: Gateway) -> None:
    assert ID_RE.match(gw.org_id), gw.org_id
    assert _public_id(gw.path) == gw.org_id


def test_br_160_only_the_organisation_carries_one(gw: Gateway) -> None:
    gw.ok("tree", "add", "/", "client", "Acme")
    conn = sqlite3.connect(gw.path)
    try:
        rows = conn.execute("SELECT level FROM node WHERE public_id IS NOT NULL").fetchall()
    finally:
        conn.close()
    assert rows == [("org",)]


def test_br_160_previous_schema_gets_one_by_migration_10(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        migrations.migrate(conn, migrations.MIGRATIONS[:9])
        conn.execute(
            "INSERT INTO node (id, parent_id, level, slug, name, created_at)"
            " VALUES ('c1', 'org', 'client', 'acme', 'Acme', 0)"
        )
        assert migrations.user_version(conn) == 9
        assert migrations.migrate(conn) == migrations.SCHEMA_VERSION
        rows = conn.execute("SELECT id, public_id FROM node ORDER BY id").fetchall()
    finally:
        conn.close()
    assert dict(rows)["c1"] is None
    assert ID_RE.match(dict(rows)["org"]), rows


def test_br_160_two_fresh_databases_differ(tmp_path: Path) -> None:
    drawn = set()
    for n in range(2):
        conn = sqlite3.connect(tmp_path / f"db{n}.db", isolation_level=None)
        try:
            migrations.migrate(conn)
            drawn.add(conn.execute("SELECT public_id FROM node WHERE id = 'org'").fetchone()[0])
        finally:
            conn.close()
    assert len(drawn) == 2, drawn


def test_br_160_stable_across_reopen_and_restart(gw: Gateway) -> None:
    before = _root_line(gw)
    assert before == f"/  « Organisation »  organisation  {gw.org_id}"
    gw.db.close()
    again = Database(str(gw.path))  # a restart: the startup runner plays again (BR-069)
    assert again.open()
    again.close()
    assert gw.db.open()
    assert _public_id(gw.path) == gw.org_id
    assert _root_line(gw) == before


def test_br_160_backup_and_restore_keep_it(gw: Gateway, tmp_path: Path) -> None:
    snap = tmp_path / "out" / "snap.db"
    backup.snapshot(str(gw.path), str(snap))
    restored = tmp_path / "restored.db"
    backup.restore(str(snap), str(restored))
    assert _public_id(snap) == gw.org_id
    assert _public_id(restored) == gw.org_id


def test_br_160_store_refuses_an_update_of_the_value(gw: Gateway) -> None:
    conn = sqlite3.connect(gw.path)
    try:
        for value in ("aaaaaaaaaaaa", None, gw.org_id.upper()):
            with pytest.raises(sqlite3.IntegrityError, match="public_id is permanent"):
                conn.execute("UPDATE node SET public_id = ? WHERE id = 'org'", (value,))
        # an update of another column of the row, the display name, succeeds
        conn.execute("UPDATE node SET name = 'Renamed' WHERE id = 'org'")
        conn.commit()
    finally:
        conn.close()
    assert _public_id(gw.path) == gw.org_id
    assert _root_line(gw) == f"/  « Renamed »  organisation  {gw.org_id}"


@pytest.mark.parametrize(
    ("level", "value"),
    [
        ("client", "bbbbbbbbbbbb"),
        ("org", "short"),
        ("org", "BBBBBBBBBBBB"),
        ("org", "bbbbbbbbbbb1"),
    ],
)
def test_br_160_store_refuses_a_malformed_or_misplaced_value(
    tmp_path: Path, level: str, value: str
) -> None:
    conn = sqlite3.connect(tmp_path / "db.db", isolation_level=None)
    try:
        migrations.migrate(conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO node (id, parent_id, level, slug, name, created_at, public_id)"
                " VALUES ('x', 'org', ?, 'x', 'X', 0, ?)",
                (level, value),
            )
    finally:
        conn.close()


def test_br_160_draw_uses_the_whole_alphabet() -> None:
    """The draw is `random() & 31` over the 32 letters: every character is reachable."""
    conn = sqlite3.connect(":memory:")
    try:
        draw = migrations._DRAW
        seen = set("".join(conn.execute(f"SELECT {draw}").fetchone()[0] for _ in range(200)))
    finally:
        conn.close()
    assert seen == set(migrations.PUBLIC_ID_ALPHABET)
