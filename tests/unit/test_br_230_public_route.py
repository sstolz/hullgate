"""Opt-in deployment prerequisites; test ops refusal before it can call Docker."""

import os
import subprocess
from pathlib import Path

import pytest

from hullgate.config import ConfigError, load

BASE = {
    "PUBLIC_URL": "https://mcp.example.test",
    "ADMIN_URL": "https://admin.example.test",
    "ADMIN_HOST": "admin.example.test",
}


@pytest.mark.parametrize("proxies", ["", " ", ",", "invalid"])
def test_br_230_refuses_missing_or_invalid_proxy_trust(proxies: str) -> None:
    with pytest.raises(ConfigError, match="TRUSTED_PROXIES"):
        load({**BASE, "TRUSTED_PROXIES": proxies})


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:14791",
        "https://elsewhere.example.test",
        "https://admin.example.test/",
        "https://admin.example.test:443",
    ],
)
def test_br_230_public_route_requires_exact_origin(url: str) -> None:
    with pytest.raises(ConfigError, match="ADMIN_URL"):
        load({**BASE, "ADMIN_URL": url, "TRUSTED_PROXIES": "10.0.0.0/8"})


@pytest.mark.parametrize("host", ["admin.example.test", "mcp.example.test"])
def test_br_230_dedicated_and_shared_names_are_supported(host: str) -> None:
    cfg = load(
        {
            **BASE,
            "ADMIN_HOST": host,
            "ADMIN_URL": "https://" + host,
            "TRUSTED_PROXIES": "10.0.0.0/8",
        }
    )
    assert cfg.admin_host == host


def test_br_230_ops_refuses_before_docker(tmp_path: Path) -> None:
    env = tmp_path / "env"
    env.write_text("ADMIN_HOST=admin.example.test\nADMIN_URL=https://admin.example.test\n")
    result = subprocess.run(
        ["sh", "deploy/ops.sh", "status"],
        env={**os.environ, "ENV_FILE": str(env)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "TRUSTED_PROXIES required" in result.stdout
    assert not result.stderr


def test_br_230_base_is_unrouted_and_overlay_routes_only_admin() -> None:
    base = Path("deploy/compose.prod.yaml").read_text()
    overlay = Path("deploy/compose.admin.yaml").read_text()
    assert "traefik.http.routers.hullgate-admin" not in base
    assert "Host(`${ADMIN_HOST}`) && PathPrefix(`/admin`)" in overlay
    assert 'traefik.http.services.hullgate-admin.loadbalancer.server.port: "4791"' in overlay
    assert 'traefik.http.routers.hullgate-admin.tls: "true"' in overlay
