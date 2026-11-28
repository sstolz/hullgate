"""BR-039, BR-132 (review A of F-010, nit-1): `restore-check` names the step that failed.

`backup restore` prints `BACKUP-STEP KO <step> : <detail>` on stderr. A migration abort of the
restored copy (`migrate`, BR-132: a value outside the mapping, `schema_error`) is reported as a
migration failure, an unreadable copy as an `integrity_check` failure. The shell function of
`deploy/ops.sh` is driven directly (`OPS_LIB=1`), with `docker` replaced by a shell function that
plays the restore container's answer.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FAKE_DOCKER = """
docker() {
  case "$1" in
    image|volume|run|rm) return 0 ;;
    exec) cat >/dev/null; printf '%s\\n' "$STEP_ERR" >&2; return 1 ;;
  esac
}
"""


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            "BACKUP-STEP KO migrate : schema_error vocab_guard_connection_last_outcome",
            "RESTORE-CHECK: KO step migration (schema_error vocab_guard_connection_last_outcome)"
            ": {file}",
        ),
        (
            "BACKUP-STEP KO integrity_check : *** in database main ***",
            "RESTORE-CHECK: KO step restore (integrity_check): {file}",
        ),
    ],
)
def test_br_039_restore_check_names_the_failed_step(
    tmp_path: Path, stderr: str, expected: str
) -> None:
    backup = tmp_path / "hullgate-20261101T000000Z.db"
    backup.write_bytes(b"x")
    result = subprocess.run(
        ["sh", "-c", f". deploy/ops.sh\n{FAKE_DOCKER}\nIMAGE=i; restore_check_core \"$1\"", "sh",
         str(backup)],
        cwd=ROOT,
        env={**os.environ, "OPS_LIB": "1", "STEP_ERR": stderr},
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert result.returncode == 1
    assert result.stdout.splitlines() == [expected.format(file=backup)]
