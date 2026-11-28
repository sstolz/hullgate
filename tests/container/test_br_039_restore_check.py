"""BR-039 — `make restore-check`: restore into a throwaway volume and container."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.unit.test_br_132_vocabulary_migration import _v5

from .conftest import Stack, docker

COUNTS = (
    "import json, sqlite3\n"
    "c = sqlite3.connect('file:/data/hullgate.db?mode=ro', uri=True)\n"
    "names = [r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")]\n"
    "print(json.dumps({n: c.execute(f'SELECT count(*) FROM \"{n}\"').fetchone()[0]"
    " for n in sorted(names)}))\n"
)


def _service_counts(stack: Stack) -> dict[str, int]:
    out = docker("exec", stack.container(), "python", "-c", COUNTS).stdout
    result: dict[str, int] = json.loads(out)
    return result


@pytest.fixture
def fresh_backup(stack: Stack) -> Path:
    stack.sql(
        "CREATE TABLE IF NOT EXISTS restore_probe (v TEXT);"
        + "INSERT INTO restore_probe VALUES ('r');" * 7
    )
    result = stack.ops("backup")
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(r"BACKUP: OK (\S+)", result.stdout)
    assert match
    return Path(match.group(1))


def _restorecheck_leftovers() -> tuple[str, str]:
    ctr = docker("ps", "-a", "-q", "--filter", "name=hullgate-restorecheck-").stdout.strip()
    vol = docker("volume", "ls", "-q", "--filter", "name=hullgate-restorecheck-").stdout.strip()
    return ctr, vol


def test_br_039_restore_check_ok_without_touching_service(stack: Stack, fresh_backup: Path) -> None:
    service_before = stack.container()
    counts_before = _service_counts(stack)
    volume_before = docker("volume", "inspect", stack.volume).stdout
    result = stack.ops("restore-check", str(fresh_backup))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESTORE-CHECK: OK" in result.stdout
    assert "readyz=200" in result.stdout
    restored = json.loads(result.stdout.split("tables=", 1)[1].strip())
    assert restored["restore_probe"] >= 7
    # Service, container and database unchanged; nothing throwaway is left.
    assert stack.container() == service_before
    assert docker("inspect", "-f", "{{.State.Running}}", service_before).stdout.strip() == "true"
    assert _service_counts(stack) == counts_before
    assert docker("volume", "inspect", stack.volume).stdout == volume_before
    assert _restorecheck_leftovers() == ("", "")


def test_br_039_default_is_most_recent_backup(stack: Stack, fresh_backup: Path) -> None:
    result = stack.ops("restore-check", "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"RESTORE-CHECK: OK {fresh_backup}" in result.stdout


def test_br_039_corrupt_file_fails(stack: Stack, tmp_path: Path, fresh_backup: Path) -> None:
    data = bytearray(fresh_backup.read_bytes())
    data[4096 : 4096 + 16] = b"\xff" * 16
    corrupt = tmp_path / "other-corrupt.db"
    corrupt.write_bytes(bytes(data))
    result = stack.ops("restore-check", str(corrupt))
    assert result.returncode != 0
    assert "RESTORE-CHECK: KO step restore" in result.stdout
    assert _restorecheck_leftovers() == ("", "")


def test_br_039_v5_backup_passes_under_v6_code(stack: Stack, tmp_path: Path) -> None:
    """F-010 AC-13 (BR-039, BR-132): a pre-cutover backup (schema version 5, every old stored
    value, 20 audit rows) passes `restore-check` under the version-6 code: `backup restore`
    migrates the copy and counts after migration, the started copy reports the same counts (the
    comparison of `restore-check`), and no second marker row is written."""
    backup = tmp_path / "hullgate-20200101T000000Z.db"
    _v5(backup, audit_rows=20)
    result = stack.ops("restore-check", str(backup))
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(x for x in result.stdout.splitlines() if x.startswith("RESTORE-CHECK: OK"))
    assert re.search(r"connections=13 \(key_absent\)", line), line
    tables = json.loads(line.split("tables=", 1)[1])
    assert tables["audit"] == 21  # 20 rows of the backup + the one marker of migration 6
    assert tables["connection"] == 14
    assert _restorecheck_leftovers() == ("", "")


def test_br_039_garbage_file_fails(stack: Stack, tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not sqlite" * 500)
    result = stack.ops("restore-check", str(garbage))
    assert result.returncode != 0
    assert "RESTORE-CHECK: KO" in result.stdout
