"""BR-030 — the container never runs as root."""

from __future__ import annotations

import json
import subprocess

from .conftest import APP_ENV, IMAGE, ROOT, Stack, docker, inspect, wait_ready


def test_br_030_image_declares_numeric_non_root_user() -> None:
    user = inspect("image", IMAGE)["Config"]["User"]
    assert user == "10001:10001"


def test_br_030_process_runs_as_10001(name: str) -> None:
    docker("run", "-d", "--name", name, "--read-only", "-v", f"{name}:/data", *APP_ENV, IMAGE)
    wait_ready(name)
    assert docker("exec", name, "id", "-u").stdout.strip() == "10001"
    status = docker("exec", name, "cat", "/proc/1/status").stdout
    uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
    assert set(uid_line.split()[1:]) == {"10001"}


def test_br_030_prod_compose_does_not_override_user(stack: Stack) -> None:
    config = json.loads(stack.compose("config", "--format", "json").stdout)
    assert "user" not in config["services"]["hullgate"]
    pid1 = docker("exec", stack.container(), "id", "-u").stdout.strip()
    assert pid1 == "10001"


def test_br_030_dev_compose_does_not_override_user() -> None:
    out = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "user" not in json.loads(out)["services"]["hullgate"]
