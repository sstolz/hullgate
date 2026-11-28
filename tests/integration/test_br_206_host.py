"""BR-206 (AC-06) — the admin listener answers only to its configured host."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tests.admin_support import Console
from tests.support import Gateway


@pytest.mark.parametrize(
    "host",
    ["evil.test", "localhost", "localhost:14790", "127.0.0.1:14791", "hullgate:4791", "", "x:y"],
)
@pytest.mark.parametrize("path", ["/admin", "/admin/api/v1/session", "/admin/api/v1/auth/options"])
def test_br_206_foreign_host_gets_421_without_body(gw: Gateway, host: str, path: str) -> None:
    console = Console(gw)
    with console.client() as client:
        for method in ("GET", "POST"):
            response = client.request(method, path, headers={"Host": host})
            assert response.status_code == 421, (host, method)
            assert response.content == b""


def test_br_206_configured_host_is_served(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        assert (
            client.get("/admin/api/v1/session", headers={"Host": "LOCALHOST:14791"}).status_code
            == 401
        )


@pytest.mark.parametrize("host", ["admin.example.test", "admin.example.test:443"])
def test_br_206_default_port_is_normalised(gw: Gateway, host: str) -> None:
    console = Console(gw, admin_url="https://admin.example.test")
    client = TestClient(console.app, base_url="https://admin.example.test")
    assert client.get("/admin/api/v1/session", headers={"Host": host}).status_code == 401
    assert (
        client.get("/admin/api/v1/session", headers={"Host": "admin.example.test:80"}).status_code
        == 421
    )


@pytest.mark.parametrize("host", ["evil.test", "", "127.0.0.1:4791"])
def test_br_206_healthz_answers_any_host(gw: Gateway, host: str) -> None:
    console = Console(gw)
    with console.client() as client:
        assert client.get("/healthz", headers={"Host": host}).status_code == 200
