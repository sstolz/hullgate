"""BR-032 — a misconfigured container does not start, and says why."""

from __future__ import annotations

import time

import pytest

from .conftest import APP_ENV, IMAGE, KEY_MOUNT, docker, http_status


@pytest.mark.parametrize(
    ("env", "variable"),
    [
        (["-e", "ADMIN_URL=http://localhost:4791"], "PUBLIC_URL"),
        (["-e", "PUBLIC_URL=http://x.test"], "ADMIN_URL"),
        (["-e", "PUBLIC_URL=x.test", "-e", "ADMIN_URL=http://localhost:4791"], "PUBLIC_URL"),
        (["-e", "PUBLIC_URL=http://x.test", "-e", "ADMIN_URL=http://localhost:4791",
          "-e", "PUBLIC_PORT=70000"], "PUBLIC_PORT"),
        (["-e", "PUBLIC_URL=https://x.test/gw", "-e", "ADMIN_URL=http://localhost:4791"],
         "PUBLIC_URL"),
    ],
)  # fmt: skip
def test_br_032_container_refuses_to_start(env: list[str], variable: str) -> None:
    start = time.monotonic()
    result = docker("run", "--rm", "--read-only", *env, IMAGE, check=False, timeout=30)
    elapsed = time.monotonic() - start
    assert result.returncode != 0
    assert elapsed < 10
    assert variable in result.stderr
    assert "Traceback" not in result.stderr


def test_br_032_container_without_key_file_runs_but_is_not_ready(name: str) -> None:
    """AC-04 (BR-032 v2, BR-033 v2): without a key file, /healthz 200 and /readyz 503."""
    env = [a for a in APP_ENV if a not in KEY_MOUNT]
    docker("run", "-d", "--name", name, "--read-only", "-v", f"{name}:/data", *env, IMAGE)
    deadline = time.monotonic() + 30
    code = (
        "import json, urllib.request as u, urllib.error as e\n"
        "try:\n"
        "    u.urlopen('http://127.0.0.1:4790/readyz', timeout=3)\n"
        "except e.HTTPError as x:\n"
        "    print(x.code, json.loads(x.read())['reason'])\n"
    )
    while True:
        try:
            if http_status(name, "http://127.0.0.1:4790/healthz") == 200:
                break
        except Exception:  # noqa: S110 - listener not ready yet
            pass
        assert time.monotonic() < deadline, docker("logs", name, check=False).stderr
        time.sleep(0.5)
    assert docker("exec", name, "python", "-c", code).stdout.split() == ["503", "key_unavailable"]
    logs = docker("logs", name, check=False)
    assert "Traceback" not in logs.stdout + logs.stderr


def test_br_032_container_with_mounted_key_is_ready(name: str) -> None:
    docker("run", "-d", "--name", name, "--read-only", "-v", f"{name}:/data", *APP_ENV, IMAGE)
    from .conftest import wait_ready

    wait_ready(name)


def test_br_032_relative_secret_path_refused_at_startup() -> None:
    env = ["-e", "PUBLIC_URL=http://x.test", "-e", "ADMIN_URL=http://localhost:4791",
           "-e", "SIGNING_KEY_FILE=relative"]  # fmt: skip
    result = docker("run", "--rm", "--read-only", *env, IMAGE, check=False, timeout=30)
    assert result.returncode != 0
    assert "SIGNING_KEY_FILE" in result.stderr
