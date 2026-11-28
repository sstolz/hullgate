"""BR-033 — /healthz and /readyz probes without authentication."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from hullgate import config
from hullgate.app import build_admin_app, build_public_app
from hullgate.db import Database
from tests.support import shared_key

PUBLIC_URL = "https://gw.example.test"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(str(tmp_path / "hullgate.db"))
    database.open()
    yield database
    database.close()


def _assert_no_leak(body: str, tmp_path: Path) -> None:
    assert str(tmp_path) not in body
    assert "Error" not in body
    assert "Traceback" not in body


def test_br_033_healthz_and_readyz_ok(db: Database) -> None:
    client = TestClient(build_public_app(db, PUBLIC_URL, key=shared_key()))
    health = client.get("/healthz")
    ready = client.get("/readyz")
    assert (health.status_code, health.json()) == (200, {"status": "ok"})
    assert (ready.status_code, ready.json()) == (200, {"status": "ok"})


def test_br_033_no_authentication_required(db: Database) -> None:
    client = TestClient(build_public_app(db, PUBLIC_URL, key=shared_key()))
    response = client.get("/readyz", headers={})
    assert response.status_code == 200
    assert "www-authenticate" not in response.headers


def test_br_033_database_created_in_wal_mode(db: Database, tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "hullgate.db")
    assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    conn.close()


def test_br_033_readyz_503_when_database_absent(tmp_path: Path) -> None:
    database = Database(str(tmp_path / "missing-dir" / "hullgate.db"))
    assert database.open() is False
    client = TestClient(build_public_app(database, PUBLIC_URL, key=shared_key()))
    assert client.get("/healthz").status_code == 200
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json() == {"status": "unavailable", "reason": "db_unavailable"}
    _assert_no_leak(ready.text, tmp_path)


def test_br_033_readyz_503_when_database_read_only(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    seed = Database(str(path))
    seed.open()
    seed.close()
    path.chmod(0o444)
    database = Database(str(path))
    database.open()
    try:
        ready = TestClient(build_public_app(database, PUBLIC_URL, key=shared_key())).get("/readyz")
    finally:
        database.close()
        path.chmod(0o644)
    assert ready.status_code == 503
    assert ready.json() == {"status": "unavailable", "reason": "db_read_only"}
    _assert_no_leak(ready.text, tmp_path)


def test_br_033_readyz_503_when_database_file_removed(db: Database, tmp_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"hullgate.db{suffix}").unlink(missing_ok=True)
    ready = TestClient(build_public_app(db, PUBLIC_URL, key=shared_key())).get("/readyz")
    assert ready.status_code == 503
    assert ready.json() == {"status": "unavailable", "reason": "db_unavailable"}


def test_br_033_readyz_503_when_write_lock_held(db: Database, tmp_path: Path) -> None:
    holder = sqlite3.connect(tmp_path / "hullgate.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        ready = TestClient(build_public_app(db, PUBLIC_URL, key=shared_key())).get("/readyz")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert ready.status_code == 503
    assert ready.json() == {"status": "unavailable", "reason": "db_locked"}
    # Once the lock is released, the probe returns to 200 without a restart.
    assert (
        TestClient(build_public_app(db, PUBLIC_URL, key=shared_key())).get("/readyz").status_code
        == 200
    )


def test_br_033_readiness_writes_nothing(db: Database, tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"

    def snapshot() -> list[object]:
        conn = sqlite3.connect(path)
        try:
            rows: list[object] = list(conn.execute("SELECT * FROM sqlite_master"))
            rows += [conn.execute("PRAGMA user_version").fetchone()]
            for table in ("node", "setting", "audit"):
                rows += list(conn.execute(f"SELECT * FROM {table}"))  # noqa: S608
            return rows
        finally:
            conn.close()

    before = snapshot()
    client = TestClient(build_public_app(db, PUBLIC_URL, key=shared_key()))
    for _ in range(20):
        assert client.get("/readyz").status_code == 200
    assert snapshot() == before


def test_br_033_admin_listener_serves_healthz(db: Database) -> None:
    admin = build_admin_app(db, config.admin_origin("http://localhost:14791"))
    response = TestClient(admin).get("/healthz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_br_033_readiness_is_thread_safe(db: Database) -> None:
    results: list[bool] = []

    def probe() -> None:
        results.append(db.readiness().ok)

    threads = [threading.Thread(target=probe) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 10


def test_br_033_readyz_503_key_unavailable_without_key(db: Database) -> None:
    client = TestClient(build_public_app(db, PUBLIC_URL))
    assert client.get("/healthz").status_code == 200
    ready = client.get("/readyz")
    assert (ready.status_code, ready.json()) == (
        503,
        {"status": "unavailable", "reason": "key_unavailable"},
    )


def test_br_033_readyz_follows_key_file_removal_and_return(db: Database, tmp_path: Path) -> None:
    from hullgate import keys

    path = tmp_path / "k.pem"
    keys.generate_signing_key(str(path))
    client = TestClient(build_public_app(db, PUBLIC_URL, key=keys.SigningKey(str(path))))
    assert client.get("/readyz").status_code == 200
    saved = path.read_bytes()
    path.chmod(0o600)
    path.unlink()
    assert client.get("/readyz").json()["reason"] == "key_unavailable"
    path.write_bytes(saved)
    assert client.get("/readyz").status_code == 200


def test_br_033_database_reasons_come_before_key(tmp_path: Path) -> None:
    database = Database(str(tmp_path / "missing" / "hullgate.db"))
    database.open()
    ready = TestClient(build_public_app(database, PUBLIC_URL)).get("/readyz")
    assert ready.json()["reason"] == "db_unavailable"


def test_br_033_invalid_pem_is_key_unavailable(db: Database, tmp_path: Path) -> None:
    """S24: an unparseable PEM makes the key unavailable."""
    from hullgate import keys

    path = tmp_path / "k.pem"
    path.write_text("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
    ready = TestClient(build_public_app(db, PUBLIC_URL, key=keys.SigningKey(str(path)))).get(
        "/readyz"
    )
    assert (ready.status_code, ready.json()["reason"]) == (503, "key_unavailable")
