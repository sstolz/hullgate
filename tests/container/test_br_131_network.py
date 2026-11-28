"""BR-131, BR-106 (F-010 §3.2 ops table, AC-24; F-011 AC-02): the upstreams network is
`hullgate_upstreams`, the data volume `hullgate_data`.

`deploy/compose.prod.yaml` creates the network named by `UPSTREAMS_NETWORK` (default
`hullgate_upstreams`, not derived from the project name) and `deploy/upstreams.compose.yaml`
(project `hullgate-upstreams`) joins the same name: Compose itself resolves both files, and an
upstream started by the second file is reached by name from the gateway of the first, on a private
address of that network.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .conftest import ROOT, Stack, docker, inspect

UPSTREAMS = "deploy/upstreams.compose.yaml"


def _config(file: str, env: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "compose", "-f", file, "--env-file", str(env), "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=60,
    )  # fmt: skip
    data: dict[str, Any] = json.loads(result.stdout)
    return data


def test_br_131_network_names_resolved_by_compose(tmp_path: Path) -> None:
    env = tmp_path / "env"
    env.write_text(
        "TRUSTED_PROXIES=192.0.2.1/32\nMCP_HOST=x.invalid\nTRAEFIK_NETWORK=t\nTRAEFIK_ENTRYPOINT=w\nTRAEFIK_CERTRESOLVER=r\n"
        "GITEA_URL=http://gitea.invalid\n",
        encoding="utf-8",
    )
    prod = _config("deploy/compose.prod.yaml", env)
    assert prod["name"] == "hullgate"
    assert prod["networks"]["upstreams"]["name"] == "hullgate_upstreams"
    assert prod["volumes"]["data"]["name"] == "hullgate_data"  # HULLGATE_VOLUME unset (F-011 AC-02)
    assert set(prod["services"]["hullgate"]["networks"]) == {"traefik", "upstreams"}
    upstreams = _config(UPSTREAMS, env)
    assert upstreams["name"] == "hullgate-upstreams"
    assert upstreams["networks"]["upstreams"]["name"] == "hullgate_upstreams"
    assert upstreams["networks"]["upstreams"]["external"] is True
    env.write_text(env.read_text(encoding="utf-8") + "UPSTREAMS_NETWORK=other\n", encoding="utf-8")
    assert _config("deploy/compose.prod.yaml", env)["networks"]["upstreams"]["name"] == "other"
    assert _config(UPSTREAMS, env)["networks"]["upstreams"]["name"] == "other"


def test_br_131_network_upstream_joins_the_gateway(stack: Stack, tmp_path: Path) -> None:
    network = f"{stack.project}_upstreams"  # UPSTREAMS_NETWORK of the stack's env file
    gateway = stack.container()
    assert network in inspect("container", gateway)["NetworkSettings"]["Networks"]
    env = tmp_path / "env"
    env.write_text(f"GITEA_URL=http://gitea.invalid\nUPSTREAMS_NETWORK={network}\n", "utf-8")
    project = f"hullgate-ct-up-{uuid.uuid4().hex[:8]}"
    up = ["docker", "compose", "-p", project, "-f", UPSTREAMS, "--env-file", str(env)]
    try:
        subprocess.run([*up, "up", "-d"], cwd=ROOT, capture_output=True, check=True, timeout=120)
        members = inspect("network", network)["Containers"].values()
        assert {m["Name"] for m in members} >= {f"{project}-gitea-mcp-1"}
        probe = "import socket; print(socket.getaddrinfo('gitea-mcp', 8080)[0][4][0])"
        address = docker("exec", gateway, "python", "-c", probe).stdout.strip()
        assert address.startswith(("172.", "10.", "192.168.")), address
    finally:
        subprocess.run([*up, "down"], cwd=ROOT, capture_output=True, check=False, timeout=120)
