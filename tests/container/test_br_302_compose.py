"""BR-302: Compose rejects absent/empty proxy trust before any container starts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from .conftest import ROOT


@pytest.mark.parametrize("proxies", [None, "", "192.0.2.2/32,2001:db8::2/128"])
@pytest.mark.parametrize("admin", [False, True])
def test_br_302_compose_requires_proxy_trust(
    tmp_path: Path, proxies: str | None, admin: bool
) -> None:
    env_file = tmp_path / "env"
    env_file.write_text(
        "MCP_HOST=mcp.example.test\nTRAEFIK_NETWORK=unused\n"
        "TRAEFIK_ENTRYPOINT=websecure\nTRAEFIK_CERTRESOLVER=test\n"
        + ("ADMIN_HOST=admin.example.test\nADMIN_URL=https://admin.example.test\n" if admin else "")
        + (f"TRUSTED_PROXIES={proxies}\n" if proxies is not None else ""),
        encoding="utf-8",
    )
    files = ["-f", "deploy/compose.prod.yaml"]
    if admin:
        files += ["-f", "deploy/compose.admin.yaml"]
    # An operator's exported variables must not fill in the deliberately missing test value.
    result = subprocess.run(
        ["docker", "compose", *files, "--env-file", str(env_file), "config", "--format", "json"],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proxies:
        assert result.returncode == 0, result.stderr
        service = json.loads(result.stdout)["services"]["hullgate"]
        assert service["environment"]["TRUSTED_PROXIES"] == proxies
    else:
        assert result.returncode != 0
        assert "TRUSTED_PROXIES" in result.stderr
