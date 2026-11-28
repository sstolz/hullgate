"""BR-031 — read-only file system except /data."""

from __future__ import annotations

import json

import pytest

from .conftest import APP_ENV, IMAGE, Stack, docker, http_status, wait_ready

WRITE = (
    "import errno, sys\n"
    "try:\n"
    "    open(sys.argv[1], 'w').write('x')\n"
    "except OSError as e:\n"
    "    print(errno.errorcode[e.errno]); sys.exit(1)\n"
    "print('written')\n"
)


@pytest.fixture
def running(name: str) -> str:
    docker("run", "-d", "--name", name, "--read-only", "-v", f"{name}:/data", *APP_ENV, IMAGE)
    wait_ready(name)
    return name


def test_br_031_ready_on_read_only_root(running: str) -> None:
    assert http_status(running, "http://127.0.0.1:4790/readyz") == 200


@pytest.mark.parametrize("path", ["/app/x", "/x", "/home/x", "/tmp/x", "/usr/local/lib/x"])
def test_br_031_writes_outside_data_fail_erofs(running: str, path: str) -> None:
    result = docker("exec", running, "python", "-c", WRITE, path, check=False)
    assert result.returncode == 1
    assert result.stdout.strip() in {"EROFS", "EACCES"}
    if path in {"/app/x", "/x", "/tmp/x"}:
        assert result.stdout.strip() == "EROFS"


def test_br_031_write_under_data_succeeds(running: str) -> None:
    result = docker("exec", running, "python", "-c", WRITE, "/data/probe")
    assert result.stdout.strip() == "written"


def test_br_031_no_pycache_written(running: str) -> None:
    env = docker("exec", running, "env").stdout
    assert "PYTHONDONTWRITEBYTECODE=1" in env.splitlines()


def test_br_031_prod_compose_read_only_without_tmpfs(stack: Stack) -> None:
    service = json.loads(stack.compose("config", "--format", "json").stdout)["services"]["hullgate"]
    assert service["read_only"] is True
    assert "tmpfs" not in service
    mounts = [(v["type"], v["target"]) for v in service["volumes"]]
    assert mounts == [("volume", "/data")]
    state = docker("inspect", stack.container()).stdout
    assert json.loads(state)[0]["HostConfig"]["ReadonlyRootfs"] is True
