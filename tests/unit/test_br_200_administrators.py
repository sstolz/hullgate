"""BR-200 — an administrator is a named account with every right: e-mail, display name, and a
state, `pending` or `active`."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from hullgate.admin import accounts
from hullgate.db import Database


def _base(tmp_path: Path) -> sqlite3.Connection:
    db = Database(str(tmp_path / "x.db"))
    assert db.open()
    db.close()
    return sqlite3.connect(tmp_path / "x.db")


def test_br_200_fields_of_an_administrator() -> None:
    names = [f.name for f in dataclasses.fields(accounts.Administrator)]
    assert names == ["id", "email", "name", "state", "created_at"]
    assert accounts.STATES == ("pending", "active")


def test_br_200_state_set_is_closed_in_the_schema(tmp_path: Path) -> None:
    conn = _base(tmp_path)
    try:
        conn.execute("INSERT INTO administrator VALUES ('a', 'a@x.test', 'A', 'pending', 0)")
        conn.execute("UPDATE administrator SET state = 'active'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE administrator SET state = 'invited'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO administrator VALUES ('b', 'b@x.test', 'B', 'admin', 0)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO administrator VALUES ('c', 'a@x.test', 'C', 'pending', 0)")
    finally:
        conn.close()


def test_br_200_an_administrator_carries_no_role(tmp_path: Path) -> None:
    """Every right: no role, scope or permission is stored beside the account (v2: PDF p. 2)."""
    conn = _base(tmp_path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(administrator)")}
    finally:
        conn.close()
    assert columns == {"id", "email", "name", "state", "created_at"}
