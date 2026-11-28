"""Tests of category `container`: built image and production composition (Docker required).

`make test-container` builds `hullgate:dev` and the two `hullgate:multiarch-*` images, then runs
these tests. No test is skipped: an absent prerequisite fails the test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROD = "deploy/compose.prod.yaml"
IMAGE = os.environ.get("HULLGATE_IMAGE", "hullgate:dev")
# Throwaway mounted secrets (F-005): under the repository root, a path the session's dockerd sees
# too; mode 0444 for the image's UID 10001; destroyed at the end of the test session.
SECRETS = ROOT / f".ct-secrets-{os.getpid()}"
SIGNING_KEY = SECRETS / "signing_key"
TOTP_SECRET = SECRETS / "authorize_totp"
CONNECTIONS_KEY = SECRETS / "connections_key"  # F-006: mounted secret of the composition
KEY_MOUNT = ["-v", f"{SIGNING_KEY}:/run/secrets/signing_key:ro"]
APP_ENV = [
    "-e", "PUBLIC_URL=http://hullgate.test", "-e", "ADMIN_URL=http://localhost:4791",
    *KEY_MOUNT,
]  # fmt: skip


@pytest.fixture(scope="session", autouse=True)
def mounted_secrets() -> Iterator[Path]:
    from hullgate import cipher, keys

    SECRETS.mkdir(mode=0o755)
    try:
        keys.generate_signing_key(str(SIGNING_KEY))
        keys.generate_totp_secret(str(TOTP_SECRET), "container-tests")
        cipher.generate_connections_key(str(CONNECTIONS_KEY))
        SIGNING_KEY.chmod(0o444)
        TOTP_SECRET.chmod(0o444)
        CONNECTIONS_KEY.chmod(0o444)
        yield SECRETS
    finally:
        shutil.rmtree(SECRETS, ignore_errors=True)


READY_PROBE = (
    "import sys, urllib.request as u\n"
    "try:\n"
    "    sys.exit(0 if u.urlopen('http://127.0.0.1:4790/readyz', timeout=2).status == 200 else 1)\n"
    "except Exception:\n"
    "    sys.exit(1)\n"
)


def docker(
    *args: str, check: bool = True, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=timeout
    )


def inspect(kind: str, name: str) -> dict[str, Any]:
    data: list[dict[str, Any]] = json.loads(docker(kind, "inspect", name).stdout)
    return data[0]


def wait_ready(container: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while docker("exec", container, "python", "-c", READY_PROBE, check=False).returncode != 0:
        if time.monotonic() > deadline:
            logs = docker("logs", container, check=False)
            raise AssertionError(f"/readyz ≠ 200 after {timeout} s\n{logs.stdout}{logs.stderr}")
        time.sleep(0.5)


def http_status(container: str, url: str) -> int:
    code = (
        "import urllib.request as u, urllib.error as e\n"
        "try:\n"
        f"    print(u.urlopen({url!r}, timeout=3).status)\n"
        "except e.HTTPError as x:\n"
        "    print(x.code)\n"
    )
    return int(docker("exec", container, "python", "-c", code).stdout.strip())


@pytest.fixture
def name() -> Iterator[str]:
    """Unique container and volume name, removed after the test."""
    value = f"hullgate-test-{uuid.uuid4().hex[:10]}"
    yield value
    docker("rm", "-f", value, check=False)
    docker("volume", "rm", "-f", value, check=False)


@dataclass
class Stack:
    """Production composition (`deploy/compose.prod.yaml` unchanged), session dockerd."""

    project: str
    env_file: Path
    backup_dir: Path
    volume: str
    network: str

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "ENV_FILE": str(self.env_file),
            "BACKUP_DIR": str(self.backup_dir),
        }

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-f", PROD, "--env-file", str(self.env_file), *args],
            cwd=ROOT, capture_output=True, text=True, check=check, timeout=180,
        )  # fmt: skip

    def ops(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "deploy/ops.sh", *args],
            cwd=ROOT, env=self.env(), capture_output=True, text=True, timeout=timeout,
        )  # fmt: skip

    def container(self) -> str:
        return self.compose("ps", "-q", "hullgate").stdout.strip()

    def sql(self, statement: str) -> None:
        code = (
            "import sqlite3\n"
            "c = sqlite3.connect('/data/hullgate.db', isolation_level=None, timeout=5)\n"
            f"c.executescript({statement!r})\n"
        )
        docker("exec", self.container(), "python", "-c", code)


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory, mounted_secrets: Path) -> Iterator[Stack]:
    suffix = uuid.uuid4().hex[:8]
    base = tmp_path_factory.mktemp("stack")
    project = f"hullgate-ct-{suffix}"
    network = f"hullgate-ct-traefik-{suffix}"
    env_file = base / "env"
    env_file.write_text(
        "\n".join(
            [
                f"COMPOSE_PROJECT_NAME={project}",
                "MCP_HOST=hullgate.ct.invalid",
                f"TRAEFIK_NETWORK={network}",
                "TRAEFIK_ENTRYPOINT=websecure",
                "TRUSTED_PROXIES=192.0.2.1/32",
                "TRAEFIK_CERTRESOLVER=ct",
                "TRAEFIK_HTTPS_PORT=443",
                "ADMIN_LOCAL_PORT=34791",
                f"HULLGATE_VOLUME={project}_data",
                f"HULLGATE_IMAGE={IMAGE}",
                f"UPSTREAMS_NETWORK={project}_upstreams",
                f"SIGNING_KEY_PATH={SIGNING_KEY}",
                f"AUTHORIZE_TOTP_PATH={TOTP_SECRET}",
                f"CONNECTIONS_KEY_PATH={CONNECTIONS_KEY}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    docker("network", "create", network)
    st = Stack(project, env_file, base / "backups", f"{project}_data", network)
    try:
        st.compose("up", "-d", "--no-build")
        wait_ready(st.container())
        yield st
    finally:
        st.compose("down", "-v", "--remove-orphans", check=False)
        docker("network", "rm", network, check=False)
