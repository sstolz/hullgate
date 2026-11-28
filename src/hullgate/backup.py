"""Database backup and restore check (BR-038, BR-039).

Run in a container of the image, by `deploy/ops.sh`:

    python -m hullgate.backup exists   <db>            # exit 0 if the database exists, 3 if not
    python -m hullgate.backup snapshot <db> <copy>     # VACUUM INTO + integrity_check
    python -m hullgate.backup restore  <copy|-> <db>   # checked copy, migrated, fresh database
    python -m hullgate.backup counts   <db>            # number of rows per table (JSON)
    python -m hullgate.backup discard  <file>          # removal of a working file
    python -m hullgate.backup connections <db>         # connections and state of their key (JSON)

Each failure exits ≠ 0 naming the step, without leaving a partial file.

`connections` (F-006, BR-039 clarified): number of live connections and the reason the
connections key is unavailable (`CONNECTIONS_KEY_FILE`, otherwise the default path), `null` if it
is usable; `restore-check` starts without a key and expects `key_absent`.

`restore` (F-010, BR-039, BR-132) applies the code's migrations to the restored copy
(`migrations.migrate`, the ones the gateway would run at startup) and counts its rows **after**
them: `restore-check` thus compares what the started gateway reads back with what the migrated
backup holds, and a backup older than the schema passes.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import BinaryIO

from hullgate import migrations
from hullgate.cipher import ConnectionsKey
from hullgate.config import DEFAULT_CONNECTIONS_KEY_FILE

BUSY_TIMEOUT_MS = 5000
EXIT_MISSING = 3


class BackupError(RuntimeError):
    def __init__(self, step: str, detail: str) -> None:
        # `<step> : <detail>`, space before the colon included: `deploy/ops.sh` parses the
        # `BACKUP-STEP KO <step> : ` prefix with `sed`.
        super().__init__(f"{step} : {detail}")
        self.step = step


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # No temporary file outside /data (read-only file system, BR-031).
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def integrity(path: str) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return "ok" if rows == [("ok",)] else "; ".join(str(r[0]) for r in rows[:5])


def counts(path: str) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        quoted = {name: '"' + name.replace('"', '""') + '"' for name in names}
        return {
            name: int(conn.execute(f"SELECT count(*) FROM {quoted[name]}").fetchone()[0])  # noqa: S608
            for name in names
        }
    finally:
        conn.close()


def connections(path: str, key_file: str) -> dict[str, object]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        live = int(conn.execute("SELECT count(*) FROM connection WHERE removed = 0").fetchone()[0])
        return {"connections": live, "key": ConnectionsKey(key_file).status(conn)}
    finally:
        conn.close()


def _remove(path: str) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(path + suffix).unlink(missing_ok=True)


def snapshot(src: str, dest: str) -> dict[str, int]:
    """Consistent hot copy of `src` to `dest` (absent), checked; nothing is left on failure."""
    if not Path(src).is_file():
        raise BackupError("source", f"database absent: {src}")
    if Path(dest).exists():
        raise BackupError("destination", f"file already present: {dest}")
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = _connect(src)
        try:
            conn.execute("VACUUM INTO ?", (dest,))
        finally:
            conn.close()
        result = integrity(dest)
        if result != "ok":
            raise BackupError("integrity_check", result)
        return counts(dest)
    except sqlite3.Error as exc:
        _remove(dest)
        raise BackupError("vacuum_into", str(exc)) from exc
    except BaseException:
        _remove(dest)
        raise


def _migrate(path: str) -> None:
    """The code's migrations run on the copy (BR-069), as at the gateway's startup."""
    conn = _connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        migrations.migrate(conn)
    except migrations.SchemaError as exc:
        raise BackupError("migrate", str(exc)) from exc
    finally:
        conn.close()


def restore(src: str | BinaryIO, dest: str) -> dict[str, int]:
    """Restores a checked backup as a fresh, migrated database (`restore-check` throwaway volume).

    `src` is a path or a binary stream (standard input); the copy is checked under a working
    name, migrated to the code's schema, then renamed to `dest`: a corrupt file or a failed
    migration leaves no database. The counts returned are those after migration.
    """
    if Path(dest).exists():
        raise BackupError("destination", f"file already present: {dest}")
    incoming = dest + ".incoming"
    try:
        if isinstance(src, str):
            if not Path(src).is_file():
                raise BackupError("source", f"backup absent: {src}")
            shutil.copyfile(src, incoming)
        else:
            with open(incoming, "wb") as out:
                shutil.copyfileobj(src, out)
        if Path(incoming).stat().st_size == 0:
            raise BackupError("source", "empty backup")
        try:
            result = integrity(incoming)
        except sqlite3.Error as exc:
            raise BackupError("integrity_check", str(exc)) from exc
        if result != "ok":
            raise BackupError("integrity_check", result)
        _migrate(incoming)
        Path(incoming).rename(dest)
    except BaseException:
        _remove(incoming)
        raise
    return counts(dest)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 64
    command, args = argv[0], argv[1:]
    try:
        if command == "exists" and len(args) == 1:
            return 0 if Path(args[0]).is_file() else EXIT_MISSING
        if command == "snapshot" and len(args) == 2:
            print(json.dumps(snapshot(args[0], args[1]), sort_keys=True))
            return 0
        if command == "restore" and len(args) == 2:
            src: str | BinaryIO = sys.stdin.buffer if args[0] == "-" else args[0]
            print(json.dumps(restore(src, args[1]), sort_keys=True))
            return 0
        if command == "counts" and len(args) == 1:
            print(json.dumps(counts(args[0]), sort_keys=True))
            return 0
        if command == "connections" and len(args) == 1:
            key_file = os.environ.get("CONNECTIONS_KEY_FILE") or DEFAULT_CONNECTIONS_KEY_FILE
            print(json.dumps(connections(args[0], key_file), ensure_ascii=False, sort_keys=True))
            return 0
        if command == "discard" and len(args) == 1:
            _remove(args[0])
            return 0
    except BackupError as exc:
        print(f"BACKUP-STEP KO {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"BACKUP-STEP KO {command} : {exc}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
