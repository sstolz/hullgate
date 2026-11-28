"""BR-038 v2, BR-039 (F-011 AC-03): backups are `hullgate-<ts>.db`, ordered by time.

Rotation and `restore-check` with no argument read `hullgate-<ts>.db` files only, newest first; a
file of another prefix (here `other-<ts>.db`, a foreign prefix under a neutral word) is neither
rotated nor selected, even when it is newer. The shell functions of `deploy/ops.sh` are driven
directly (`OPS_LIB=1` loads them without running).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = [  # oldest first
    "hullgate-20261101T000000Z.db",
    "hullgate-20261102T000000Z.db",
    "hullgate-20261103T000000Z.db",
    "hullgate-20261104T000000Z.db",
    "hullgate-20261105T000000Z.db",
]
FOREIGN = ["other-20261031T000000Z.db", "other-20261106T000000Z.db"]
NOISE = ["pre-fresh-start-20261107T000000Z.db", "hullgate-20261107T000000Z.db.partial", ".lock"]


def _ops(script: str, directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", f". deploy/ops.sh; {script}", "sh", str(directory)],
        cwd=ROOT,
        env={**os.environ, "OPS_LIB": "1"},
        capture_output=True,
        text=True,
        check=True,
    )


def _fixture(tmp_path: Path) -> Path:
    for name in FILES + FOREIGN + NOISE:
        (tmp_path / name).write_bytes(b"x")
    return tmp_path


def test_br_038_backup_order_newest_first_by_timestamp(tmp_path: Path) -> None:
    out = _ops('backups_newest_first "$1"', _fixture(tmp_path)).stdout.split()
    assert out == list(reversed(FILES))


def test_br_038_backup_order_rotation_keeps_the_three_newest(tmp_path: Path) -> None:
    directory = _fixture(tmp_path)
    result = _ops('backup_rotate "$1" 3', directory)
    left = sorted(p.name for p in directory.iterdir())
    assert left == sorted(FILES[2:] + FOREIGN + NOISE)  # foreign and aside files stay
    assert result.stdout.splitlines() == [
        "BACKUP: rotation, removed hullgate-20261102T000000Z.db",
        "BACKUP: rotation, removed hullgate-20261101T000000Z.db",
    ]


def test_br_038_backup_order_restore_check_selects_the_newest(tmp_path: Path) -> None:
    out = _ops('backups_newest_first "$1" | head -n 1', _fixture(tmp_path)).stdout.strip()
    assert out == "hullgate-20261105T000000Z.db"  # not the newer foreign file
    ops = (ROOT / "deploy" / "ops.sh").read_text(encoding="utf-8")
    assert 'file=$(backups_newest_first "$BACKUP_DIR" | head -n 1)' in ops
    assert 'backup_rotate "$BACKUP_DIR" "$BACKUP_KEEP"' in ops
    assert 'name="hullgate-$(date -u +%Y%m%dT%H%M%SZ).db"' in ops
