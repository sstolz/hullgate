"""BR-131 v2 (F-011 §3.2 F4, review 554 B1): the fresh start moves the gateway to the new volume.

Once `.env` names a new volume (F2), whether a plain `up -d` recreates the running container
depends on the Compose release: v5.7.1 creates the volume and keeps the container on the old one
(review 554), v5.6.1 recreates it. `up -d --force-recreate`, as step F4 plays it, ends on the new
volume under either, which is what this test pins. Played on the production composition
(`deploy/compose.prod.yaml` unchanged) under a throwaway project of its own, beside the session's
shared stack.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from .conftest import PROD, ROOT, Stack, docker, inspect, wait_ready


def _data_mount(container: str) -> str:
    mounts = [m for m in inspect("container", container)["Mounts"] if m["Destination"] == "/data"]
    assert len(mounts) == 1, mounts
    name: str = mounts[0]["Name"]
    return name


def test_br_131_force_recreate_moves_the_gateway_to_the_new_volume(
    stack: Stack, tmp_path: Path
) -> None:
    project = f"hullgate-ct-fs-{uuid.uuid4().hex[:8]}"
    old, new = f"{project}_old", f"{project}_new"
    template = stack.env_file.read_text(encoding="utf-8")
    env = tmp_path / "env"

    def write_env(volume: str) -> None:
        lines = []
        for line in template.splitlines():
            key = line.split("=", 1)[0]
            if key == "COMPOSE_PROJECT_NAME":
                line = f"COMPOSE_PROJECT_NAME={project}"
            elif key == "HULLGATE_VOLUME":
                line = f"HULLGATE_VOLUME={volume}"
            elif key == "ADMIN_LOCAL_PORT":
                line = "ADMIN_LOCAL_PORT=34792"  # the shared stack holds 34791
            elif key == "UPSTREAMS_NETWORK":
                line = f"UPSTREAMS_NETWORK={project}_upstreams"
            lines.append(line)
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def compose(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-f", PROD, "--env-file", str(env), *args],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=180,
        )  # fmt: skip

    def container() -> str:
        return compose("ps", "-q", "hullgate").stdout.strip()

    write_env(old)
    try:
        compose("up", "-d", "--no-build")
        wait_ready(container())
        assert _data_mount(container()) == old
        write_env(new)  # F2
        compose("up", "-d", "--no-build", "--force-recreate")  # F4 as written
        wait_ready(container())
        assert _data_mount(container()) == new
        assert inspect("volume", new)["Name"] == new
        assert docker("ps", "-q", "--filter", f"volume={old}").stdout.strip() == ""
    finally:
        compose("down", "--remove-orphans")
        docker("volume", "rm", old, new, check=False)
