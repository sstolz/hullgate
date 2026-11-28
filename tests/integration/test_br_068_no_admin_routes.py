"""BR-068 v2 (AC-35) — no admin route without authentication.

The sweep walks every registered route of the admin application and asserts 401 without a session,
except the listed ones: `/healthz`, the data-free shell and the sign-in and enrolment ceremonies.
The public listener keeps 404 on `/admin`. No network position is treated as authentication.
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
from starlette.routing import Mount, Route

from hullgate.admin.app import API_PREFIX, CEREMONIES
from tests.admin_support import Console, bundle
from tests.support import Gateway, shared_key, standard_tree


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(url: str, host: str | None = None) -> httpx.Response:
    deadline = time.monotonic() + 15
    while True:
        try:
            return httpx.get(url, timeout=1, headers={"Host": host} if host else None)
        except httpx.TransportError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def test_br_068_sweep_every_route_needs_a_session(gw: Gateway, tmp_path: Path) -> None:
    console = Console(gw, static_dir=bundle(tmp_path))
    starlette = console.app.app
    registered: list[tuple[str, str]] = []
    for route in starlette.routes:  # type: ignore[attr-defined]
        if isinstance(route, Mount):
            for sub in route.routes:
                assert isinstance(sub, Route)
                registered += [(m, route.path + sub.path) for m in sorted(sub.methods or ())]
        elif isinstance(route, Route):
            registered += [(m, route.path) for m in sorted(route.methods or ())]
    declared = {(s.method, f"{API_PREFIX}/{s.path}") for s in console.app.api_routes}
    assert declared <= set(registered)
    listed = {("GET", "/healthz"), ("HEAD", "/healthz"), ("GET", "/admin"), ("HEAD", "/admin"),
              ("GET", "/admin/{rest:path}"), ("HEAD", "/admin/{rest:path}")}  # fmt: skip
    listed |= {("POST", f"{API_PREFIX}/{p}") for p in CEREMONIES}
    with console.client() as client:
        for method, path in registered:
            concrete = path.replace("{credential_id}", "x").replace("{rest:path}", "tree")
            response = client.request(method, concrete, json={}, headers=console.headers(csrf=""))
            if (method, path) in listed:
                assert response.status_code != 401, (method, path)
            else:
                assert response.status_code == 401, (method, path, response.text)
                if method != "HEAD":
                    assert response.json()["code"] == "unauthenticated"


def test_br_068_real_process_serves_no_admin_data_without_session(tmp_path: Path) -> None:
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
        stderr=subprocess.DEVNULL,
    )
    admin = f"http://127.0.0.1:{admin_port}"
    host = f"localhost:{admin_port}"
    public = f"http://127.0.0.1:{public_port}"
    try:
        assert _wait(f"{admin}/healthz").status_code == 200
        assert _wait(f"{public}/readyz").status_code == 200
        for path in ("/admin/api/v1/session", "/admin/api/v1/meta", "/admin/api/v1/passkeys",
                     "/admin/api/v1/allowlist"):  # fmt: skip
            assert httpx.get(admin + path, headers={"Host": host}).status_code == 401, path
        for path in ("/tree", "/tokens", "/audit", "/export"):
            for method in ("GET", "POST"):
                response = httpx.request(method, admin + path, headers={"Host": host})
                assert response.status_code == 404, (method, path)
        # a neighbour reaching the port by another name: 421, no body
        assert httpx.get(f"{admin}/admin/api/v1/session").status_code == 421
        for path in ("/admin", "/admin/key", "/admin/store", "/admin/api/v1/session", "/tree"):
            for method in ("GET", "POST"):
                assert httpx.request(method, public + path).status_code == 404, (method, path)
    finally:
        proc.terminate()
        proc.wait(timeout=15)


@pytest.mark.parametrize(
    "path", ["/p", "/p/", "/p/a/mcp", "/oauth/p/a/b/c/d/token", "/x", "/admin"]
)
def test_br_068_public_listener_only_serves_probes_and_endpoints(gw: Gateway, path: str) -> None:
    standard_tree(gw)
    with gw.client() as client:
        assert client.get(path).status_code == 404
        assert client.post(path, json={}).status_code == 404


def _raw_get(port: int, path: str, host: str = "evil.example") -> tuple[int, dict[str, str], bytes]:
    """One HTTP/1.1 request on a raw socket: the path goes out byte for byte, never normalised."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = dict(line.lower().split(": ", 1) for line in lines[1:] if ": " in line)
    return int(lines[0].split()[1]), headers, body


def test_br_068_probes_answer_only_their_exact_path_on_a_real_listener(tmp_path: Path) -> None:
    """Review 587, B1: on uvicorn, `/healthz/` and `/readyz/` answered 307 with a `Location` built
    from the request's Host. Every other spelling of a probe now answers the JSON 404 of any path
    that is no route, without `Location`; the probes themselves still answer at the root."""
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
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait(f"http://127.0.0.1:{public_port}/readyz").status_code == 200
        for path in ("/healthz", "/readyz"):
            status, headers, _ = _raw_get(public_port, path)
            assert status == 200, path
            assert "location" not in headers
        for path in ("/healthz/", "/readyz/", "/%68ealthz", "/readyz%2F", "//healthz", "/HEALTHZ"):
            status, headers, body = _raw_get(public_port, path)
            assert status == 404, (path, status, headers)
            assert "location" not in headers, (path, headers)
            assert body == b'{"error":"not_found"}', (path, body)
    finally:
        proc.terminate()
        proc.wait(timeout=15)
