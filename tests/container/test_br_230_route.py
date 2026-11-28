"""AC-31: opt-in smoke, dedicated and shared names. Docker-only, never production."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("host", ["", "admin.example.test", "mcp.example.test"])
def test_br_230_smoke(host: str) -> None:
    # MCP_HOST is read from the smoke fixture for the shared-name case.
    if host == "mcp.example.test":
        from pathlib import Path

        host = next(
            line.split("=", 1)[1]
            for line in Path("deploy/smoke/env.test").read_text().splitlines()
            if line.startswith("MCP_HOST=")
        )
    result = subprocess.run(
        ["sh", "deploy/smoke/smoke.sh"],
        env={**os.environ, "SMOKE_ADMIN_HOST": host},
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"SMOKE {'admin-routed' if host else 'admin-not-routed'} OK" in result.stdout
    assert "SMOKE client-address OK" in result.stdout
    assert "SMOKE edge-404 OK" in result.stdout
