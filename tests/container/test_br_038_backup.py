"""BR-038 — `make backup` (deploy/ops.sh backup): consistent hot copy, checked, rotation."""

from __future__ import annotations

import fcntl
import re
import sqlite3
import subprocess
from pathlib import Path

from .conftest import ROOT, Stack, docker

NAME = re.compile(r"^hullgate-\d{8}T\d{6}Z\.db$")  # the only prefix read (BR-038 v2)

WRITER = (
    "import sqlite3, time\n"
    "c = sqlite3.connect('/data/hullgate.db', isolation_level=None, timeout=5)\n"
    "c.execute('CREATE TABLE IF NOT EXISTS concurrent (v INTEGER)')\n"
    "end = time.monotonic() + 8\n"
    "i = 0\n"
    "while time.monotonic() < end:\n"
    "    c.execute('INSERT INTO concurrent VALUES (?)', (i,)); i += 1\n"
)


def _backups(stack: Stack) -> list[Path]:
    if not stack.backup_dir.is_dir():
        return []
    return sorted(p for p in stack.backup_dir.iterdir() if NAME.match(p.name))


def test_br_038_hot_backup_with_concurrent_writes(stack: Stack) -> None:
    stack.sql(
        "CREATE TABLE IF NOT EXISTS probe (v INTEGER);" + "INSERT INTO probe VALUES (1);" * 50
    )
    before = set(_backups(stack))
    writer = subprocess.Popen(
        ["docker", "exec", stack.container(), "python", "-c", WRITER],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )  # fmt: skip
    try:
        result = stack.ops("backup")
    finally:
        writer.wait(timeout=30)
    assert writer.returncode == 0, writer.stderr.read() if writer.stderr else b""
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BACKUP: OK" in result.stdout
    new = set(_backups(stack)) - before
    assert len(new) == 1
    path = new.pop()
    assert path.name.startswith("hullgate-")  # new backups take the product name (F-010 N4)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT count(*) FROM probe").fetchone()[0] >= 50
    finally:
        conn.close()
    assert not list(stack.backup_dir.glob("*.partial"))


def test_br_038_keeps_fourteen_most_recent(stack: Stack) -> None:
    stack.backup_dir.mkdir(parents=True, exist_ok=True)
    for existing in _backups(stack):
        existing.unlink()
    fakes = [stack.backup_dir / f"hullgate-2020010{d}T000000Z.db" for d in range(1, 10)]
    fakes += [stack.backup_dir / f"hullgate-202001{d}T000000Z.db" for d in range(10, 15)]
    for fake in fakes:
        fake.write_bytes(b"old")
    assert len(_backups(stack)) == 14
    result = stack.ops("backup")
    assert result.returncode == 0, result.stdout + result.stderr
    remaining = _backups(stack)
    assert len(remaining) == 14
    assert fakes[0] not in remaining  # the oldest one is removed
    assert all(f in remaining for f in fakes[1:])
    assert "rotation, removed hullgate-20200101T000000Z.db" in result.stdout


def test_br_038_failed_backup_keeps_existing(stack: Stack) -> None:
    stack.backup_dir.mkdir(parents=True, exist_ok=True)
    (stack.backup_dir / ".lock").touch()
    snapshot = {p.name: p.read_bytes() for p in _backups(stack)}
    assert snapshot
    stack.backup_dir.chmod(0o555)  # copy impossible: failure forced at the filing step
    try:
        result = stack.ops("backup")
    finally:
        stack.backup_dir.chmod(0o755)
    assert result.returncode != 0
    assert "BACKUP: KO" in result.stdout
    assert {p.name: p.read_bytes() for p in _backups(stack)} == snapshot
    assert not list(stack.backup_dir.glob("*.partial"))


def test_br_038_exclusive_lock(stack: Stack) -> None:
    stack.backup_dir.mkdir(parents=True, exist_ok=True)
    count = len(_backups(stack))
    with open(stack.backup_dir / ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = stack.ops("backup")
    assert result.returncode != 0
    assert "lock held" in result.stdout
    assert len(_backups(stack)) == count


def test_br_038_backup_with_service_stopped(stack: Stack) -> None:
    stack.compose("stop", "hullgate")
    try:
        result = stack.ops("backup")
    finally:
        stack.compose("start", "hullgate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "throwaway container" in result.stdout
    assert "BACKUP: OK" in result.stdout
    leftovers = docker("ps", "-a", "-q", "--filter", "name=hullgate-backup-").stdout
    assert leftovers.strip() == ""


def test_br_038_no_database_prints_and_exits_zero(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text(
        "TRUSTED_PROXIES=192.0.2.1/32\nMCP_HOST=x.invalid\nTRAEFIK_NETWORK=none-net\nTRAEFIK_ENTRYPOINT=w\n"
        "TRAEFIK_CERTRESOLVER=r\nHULLGATE_VOLUME=hullgate-ct-absent-volume\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["sh", "deploy/ops.sh", "backup"], cwd=ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "ENV_FILE": str(env_file),
             "BACKUP_DIR": str(tmp_path / "b"), "HOME": "/tmp"},
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BACKUP: no database" in result.stdout
    assert not [p for p in (tmp_path / "b").iterdir() if NAME.match(p.name)]
