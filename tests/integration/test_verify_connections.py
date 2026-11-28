"""F-006 AC-OP instrument (`make verify-connections`) played against the fake provider: four
lines with the exact pattern, a banner on every path, no token in its output (F-010 §3.2: check
names `state token renewal output`, verdicts `OK`/`KO`)."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from hullgate import connections, lifecycle, verify_connections
from tests.support import REGISTRY, Gateway, standard_tree

LINE = re.compile(r"^VERIFY-CONNECTIONS (state|token|renewal|output): (OK|KO .+)$")


def _run(gw: Gateway, name: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    code = verify_connections.run(name, gw.env, out=gw.outbound)
    return code, capsys.readouterr().out.splitlines()


def test_verify_connections_four_of_four(gw: Gateway, capsys: pytest.CaptureFixture[str]) -> None:
    standard_tree(gw)
    gw.oauth()
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.expire("mpx/gitea-dev", 120)  # the instrument runs on the real clock
    code, lines = _run(gw, "mpx/gitea-dev", capsys)
    assert code == 0, lines
    checks = [line for line in lines if LINE.match(line)]
    assert checks == [f"VERIFY-CONNECTIONS {c}: OK" for c in verify_connections.CHECKS]
    assert lines[-1] == "VERIFY-CONNECTIONS: 4/4 OK"
    assert "VERIFY-CONNECTIONS reading gitea-api: 200" in lines
    assert "VERIFY-CONNECTIONS reading refresh_token rotated: yes" in lines
    assert "VERIFY-CONNECTIONS reading account: cairn-ai" in lines
    blob = "\n".join(lines)
    for value in REGISTRY.secrets:
        assert value not in blob


def test_verify_connections_renewal_in_the_same_second(
    gw: Gateway, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 419 m1: token within the 60 s margin, the first access renews by itself and
    `renew` follows in the same second — equal deadlines, yet a successful renewal.
    Clock frozen for both renewals: the edge case made deterministic."""
    standard_tree(gw)
    gw.oauth()
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.expire("mpx/gitea-dev", 30)  # within the margin: the `token` check already renews
    frozen = float(int(time.time()))
    original = lifecycle.access

    def same_second(*args: Any, **kwargs: Any) -> connections.Access:
        return original(*args, **{**kwargs, "clock": lambda: frozen})

    monkeypatch.setattr(lifecycle, "access", same_second)
    before = gw.tokens("mpx/gitea-dev")
    code, lines = _run(gw, "mpx/gitea-dev", capsys)
    renewal = [line for line in lines if line.startswith("VERIFY-CONNECTIONS renewal:")]
    assert renewal == ["VERIFY-CONNECTIONS renewal: OK"]
    assert code == 0, lines
    assert lines[-1] == "VERIFY-CONNECTIONS: 4/4 OK"
    after = gw.tokens("mpx/gitea-dev")
    assert before is not None
    assert after is not None
    assert after["access_token"] != before["access_token"]


def test_verify_connections_banner_on_failure_paths(
    gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    standard_tree(gw)
    gw.service()
    gw.application()
    gw.open_oauth()  # pending: state and token fail
    code, lines = _run(gw, "mpx/gitea-dev", capsys)
    assert code == 1
    assert lines[-1].startswith("VERIFY-CONNECTIONS: ")
    assert lines[-1].endswith("/4 OK")
    assert "VERIFY-CONNECTIONS state: KO state pending, account unknown" in lines
    assert verify_connections.main(["mal-forme"]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "VERIFY-CONNECTIONS: 0/4 OK"


def test_verify_connections_ops_banner_without_env() -> None:
    result = subprocess.run(
        ["sh", "deploy/ops.sh", "verify-connections", "mpx/gitea-dev"],
        env={"ENV_FILE": "/nonexistent/.env", "PATH": "/usr/bin:/bin"},
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert lines[-1] == "VERIFY-CONNECTIONS: 0/4 OK"
    assert all(LINE.match(line) for line in lines[:-1])
