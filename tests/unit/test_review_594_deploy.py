"""Review 594 deployment guidance and pre-Docker prerequisite regressions."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from hullgate.admin.client_address import client_address
from hullgate.config import ConfigError, load


def test_n2_runbook_limits_trust_and_checks_forged_headers() -> None:
    runbook = Path("docs/guides/admin.md").read_text().split("## Step D3", 1)[1]
    example = Path("deploy/env.example").read_text()
    for text in (runbook, example):
        assert "Traefik's own address" in text
        assert "not its subnet" in text
        assert "network gateway" in text
        assert "X-Forwarded-For" in text
    assert "Both requests must return **403**" in runbook
    assert '-H "X-Forwarded-For: ${ALLOWED_CALLER_IP}"' in runbook
    assert "http://127.0.0.1:${ADMIN_LOCAL_PORT}/admin/api/v1/session" in runbook
    proxy = re.search(r"^TRUSTED_PROXIES=(.+)$", runbook, re.MULTILINE)
    assert proxy is not None
    cfg = load(
        {
            "PUBLIC_URL": "https://mcp.example.test",
            "ADMIN_URL": "https://admin.example.test",
            "TRUSTED_PROXIES": proxy[1],
        }
    )
    assert client_address("172.18.0.1", ["203.0.113.7"], cfg.trusted_proxies) == "172.18.0.1"
    assert client_address("172.18.0.2", ["203.0.113.7"], cfg.trusted_proxies) == "203.0.113.7"
    assert (
        "127.0.0.1:${ADMIN_LOCAL_PORT:-14791}:4791" in Path("deploy/compose.prod.yaml").read_text()
    )


@pytest.mark.parametrize("host", ["Admin.Example.Test", "admin.example.test"])
@pytest.mark.parametrize("url_host", ["Admin.Example.Test", "admin.example.test"])
def test_n3_ops_and_application_agree_before_docker(
    tmp_path: Path, host: str, url_host: str
) -> None:
    values = {
        "PUBLIC_URL": "https://mcp.example.test",
        "ADMIN_HOST": host,
        "ADMIN_URL": "https://" + url_host,
        "TRUSTED_PROXIES": "172.18.0.2/32",
    }
    accepted = url_host == "admin.example.test"
    if accepted:
        assert load(values).admin_host == "admin.example.test"
    else:
        with pytest.raises(ConfigError, match="ADMIN_URL"):
            load(values)
    env_file = tmp_path / "env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    # Never invoke real Docker, even when reproducing the broken prerequisite check.
    result = subprocess.run(
        ["sh", "-c", ". deploy/ops.sh; docker() { echo DOCKER; }; require_env; compose ps"],
        env={**os.environ, "ENV_FILE": str(env_file), "OPS_LIB": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if accepted else 1), result.stdout
    assert ("DOCKER" in result.stdout) == accepted
    if not accepted:
        assert "ADMIN_URL must equal" in result.stdout
    assert not result.stderr
