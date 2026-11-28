"""BR-038, BR-039 — the backup module (`hullgate.backup`) outside the container.

The full runs (`make backup`, `make restore-check`) are in `tests/container/`.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest

from hullgate import backup


def _db(path: Path, rows: int = 3) -> Path:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.execute('CREATE TABLE "o""dd" (v TEXT)')
    conn.close()
    return path


def test_br_038_snapshot_is_verified_copy(tmp_path: Path) -> None:
    src = _db(tmp_path / "src.db", rows=5)
    dest = tmp_path / "out" / "copy.db"
    assert backup.snapshot(str(src), str(dest)) == {'o"dd': 0, "t": 5}
    assert backup.integrity(str(dest)) == "ok"


def test_br_038_snapshot_of_absent_base_fails_without_file(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupError) as err:
        backup.snapshot(str(tmp_path / "absent.db"), str(tmp_path / "copy.db"))
    assert err.value.step == "source"
    assert not (tmp_path / "copy.db").exists()


def test_br_038_snapshot_never_overwrites(tmp_path: Path) -> None:
    src = _db(tmp_path / "src.db")
    dest = tmp_path / "copy.db"
    dest.write_bytes(b"existing")
    with pytest.raises(backup.BackupError):
        backup.snapshot(str(src), str(dest))
    assert dest.read_bytes() == b"existing"


def test_br_038_snapshot_failure_leaves_no_partial(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    src.write_bytes(b"not a database" * 100)
    dest = tmp_path / "copy.db"
    with pytest.raises(backup.BackupError) as err:
        backup.snapshot(str(src), str(dest))
    assert err.value.step == "vacuum_into"
    assert list(tmp_path.iterdir()) == [src]


def test_br_039_restore_from_stream(tmp_path: Path) -> None:
    src = _db(tmp_path / "src.db", rows=7)
    snap = tmp_path / "snap.db"
    backup.snapshot(str(src), str(snap))
    dest = tmp_path / "restored.db"
    # F-010 (BR-039, BR-132): the restored copy is migrated like the gateway would migrate it,
    # and counted after the migrations; the tables of the backup keep their rows.
    restored = backup.restore(io.BytesIO(snap.read_bytes()), str(dest))
    assert {k: restored[k] for k in ('o"dd', "t")} == {'o"dd': 0, "t": 7}
    assert backup.counts(str(dest)) == restored
    assert set(restored) > {'o"dd', "t", "connection", "audit"}


@pytest.mark.parametrize("payload", [b"", b"garbage" * 1000])
def test_br_039_restore_refuses_corrupt_input(tmp_path: Path, payload: bytes) -> None:
    dest = tmp_path / "restored.db"
    with pytest.raises(backup.BackupError):
        backup.restore(io.BytesIO(payload), str(dest))
    assert list(tmp_path.iterdir()) == []


def test_br_039_restore_detects_damaged_page(tmp_path: Path) -> None:
    src = _db(tmp_path / "src.db", rows=2000)
    snap = tmp_path / "snap.db"
    backup.snapshot(str(src), str(snap))
    data = bytearray(snap.read_bytes())
    page = 4096
    data[page : page + 16] = b"\xff" * 16  # header of page 2 (root of t)
    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(bytes(data))
    with pytest.raises(backup.BackupError) as err:
        backup.restore(str(damaged), str(tmp_path / "restored.db"))
    assert err.value.step == "integrity_check"
    assert not (tmp_path / "restored.db").exists()


def test_br_038_cli_exists_codes(tmp_path: Path) -> None:
    src = _db(tmp_path / "src.db")
    assert backup.main(["exists", str(src)]) == 0
    assert backup.main(["exists", str(tmp_path / "absent.db")]) == backup.EXIT_MISSING
