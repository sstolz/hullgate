"""BR-034 — administration has its own port; the public listener does not serve /admin.

The last test starts the real process (`python -m hullgate`): two distinct listeners.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from hullgate.app import build_public_app
from hullgate.db import Database
from tests.admin_support import Console
from tests.support import Gateway, shared_key

PUBLIC_URL = "https://gw.example.test"


@pytest.fixture
def public(tmp_path: Path) -> TestClient:
    db = Database(str(tmp_path / "hullgate.db"))
    db.open()
    return TestClient(build_public_app(db, PUBLIC_URL, key=shared_key()))


@pytest.mark.parametrize("path", ["/admin", "/admin/", "/admin/login", "/admin/healthz"])
def test_br_034_public_listener_has_no_admin(public: TestClient, path: str) -> None:
    assert public.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/", "/readyz", "/admin", "/mcp", "/p/a/b/c/d/mcp"])
def test_br_034_admin_listener_serves_no_public_route(gw: Gateway, path: str) -> None:
    """F-016: the admin listener serves `/healthz` and `/admin` (shell and API, BR-068 v2) only;
    without a built bundle `/admin` is 404 too (BR-212)."""
    with Console(gw).client() as client:
        assert client.get(path).status_code == 404


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(url: str) -> httpx.Response:
    deadline = time.monotonic() + 15
    while True:
        try:
            return httpx.get(url, timeout=1)
        except httpx.TransportError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def test_br_034_process_runs_two_distinct_listeners(tmp_path: Path) -> None:
    public_port, admin_port = _free_port(), _free_port()
    env = {
        **os.environ,
        "PUBLIC_URL": f"http://127.0.0.1:{public_port}",
        "ADMIN_URL": f"http://localhost:{admin_port}",
        "PUBLIC_PORT": str(public_port),
        "ADMIN_PORT": str(admin_port),
        "DATA_DIR": str(tmp_path),
        "SIGNING_KEY_FILE": shared_key().path,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "hullgate"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait(f"http://127.0.0.1:{public_port}/readyz").status_code == 200
        assert _wait(f"http://127.0.0.1:{admin_port}/healthz").status_code == 200
        assert httpx.get(f"http://127.0.0.1:{public_port}/admin").status_code == 404
        admin_host = {"Host": f"localhost:{admin_port}"}
        assert (
            httpx.get(f"http://127.0.0.1:{admin_port}/readyz", headers=admin_host).status_code
            == 404
        )
        assert (tmp_path / "hullgate.db").is_file()
    finally:
        proc.terminate()
        code = proc.wait(timeout=15)
    assert code == 0, proc.stderr.read().decode() if proc.stderr else ""


def test_br_032_process_refuses_missing_variable(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in {"PUBLIC_URL", "ADMIN_URL"}}
    env["ADMIN_URL"] = "http://localhost:1"
    result = subprocess.run(
        [sys.executable, "-m", "hullgate"], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert "PUBLIC_URL" in result.stderr
