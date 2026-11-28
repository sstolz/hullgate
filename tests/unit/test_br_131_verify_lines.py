"""BR-131 (F-010 §3.2 ops table, AC-23): the verify instruments speak the English ops vocabulary.

`make verify-auth`, `make verify-connections` and `make verify-relay` print only `VERIFY-AUTH`,
`VERIFY-CONNECTIONS` and `VERIFY-RELAY` lines: a check line with the verdict `OK`, `KO` or `SKIP`, a
`reading` line, then the banner. The check names and the line patterns are pinned here as literals;
the modules (`hullgate.verify_connections`, `hullgate.verify_relay`) and the shell fallbacks of
`deploy/ops.sh` (the banner printed when the instrument cannot run) are both driven.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
from pathlib import Path

import pytest

from hullgate import verify_connections, verify_relay

ROOT = Path(__file__).resolve().parents[2]

CONNECTION_CHECKS = ("state", "token", "renewal", "output")
RELAY_CHECKS = ("list", "call", "refusal", "sensitive", "output")
AUTH_CHECKS = ("key-loaded", "admin-routes", "discovery", "challenge")
LINES = {
    "VERIFY-CONNECTIONS": re.compile(
        r"^VERIFY-CONNECTIONS (?:(state|token|renewal|output): (?:OK|KO .+)|reading [^:]+: .*)$"
        r"|^VERIFY-CONNECTIONS: [0-4]/4 OK$"
    ),
    "VERIFY-RELAY": re.compile(
        r"^VERIFY-RELAY (?:(list|call|refusal|sensitive|output): (?:OK|KO .+|SKIP .+)"
        r"|reading [^:]+: .*)$|^VERIFY-RELAY: [0-5]/5 OK$"
    ),
    "VERIFY-AUTH": re.compile(
        r"^VERIFY-AUTH (key-loaded|admin-routes|discovery|challenge) (?:OK|KO)( .*)?$"
        r"|^VERIFY-AUTH: [0-4]/4 OK$"
    ),
    # F-016 lot A (AC-OP-01): `make verify-admin`
    "VERIFY-ADMIN": re.compile(
        r"^VERIFY-ADMIN (healthz|foreign-host|api-session|shell-csp) (?:OK|KO)( .*)?$"
        r"|^VERIFY-ADMIN: [0-4]/4 OK$"
    ),
}


def _only_pinned_lines(prefix: str, out: str) -> list[str]:
    lines = out.strip().splitlines()
    assert lines, out
    assert all(LINES[prefix].match(line) for line in lines), out
    return lines


def test_verify_lines_check_names_are_pinned() -> None:
    assert verify_connections.CHECKS == CONNECTION_CHECKS
    assert verify_connections.PREFIX == "VERIFY-CONNECTIONS"
    assert verify_relay.CHECKS == RELAY_CHECKS
    assert verify_relay.PREFIX == "VERIFY-RELAY"


def test_verify_lines_connections_report(capsys: pytest.CaptureFixture[str]) -> None:
    report = verify_connections.Report()
    report.reading("account", "smoke-bot")
    report.check("state", None)
    report.check("token", "expiry None")
    assert report.finish() == 1
    lines = _only_pinned_lines("VERIFY-CONNECTIONS", capsys.readouterr().out)
    assert lines == [
        "VERIFY-CONNECTIONS reading account: smoke-bot",
        "VERIFY-CONNECTIONS state: OK",
        "VERIFY-CONNECTIONS token: KO expiry None",
        "VERIFY-CONNECTIONS renewal: KO not run",
        "VERIFY-CONNECTIONS output: KO incomplete run (no token obtained)",
        "VERIFY-CONNECTIONS: 1/4 OK",
    ]


def test_verify_lines_connections_bad_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert verify_connections.main(["bad"]) == 1
    lines = _only_pinned_lines("VERIFY-CONNECTIONS", capsys.readouterr().out)
    assert lines[-1] == "VERIFY-CONNECTIONS: 0/4 OK"
    assert "VERIFY-CONNECTIONS state: KO CONNECTION absent or malformed" in lines[0]


def test_verify_lines_relay_report(capsys: pytest.CaptureFixture[str]) -> None:
    report = verify_relay.Report([])
    report.reading("tool", "gt__get_me")
    report.check("list", None)
    report.check("call", None)
    report.check("refusal", "HTTP 200 None")
    report.skip("sensitive", "no sensitive role named")
    assert report.finish() == 1
    lines = _only_pinned_lines("VERIFY-RELAY", capsys.readouterr().out)
    assert lines == [
        "VERIFY-RELAY reading tool: gt__get_me",
        "VERIFY-RELAY list: OK",
        "VERIFY-RELAY call: OK",
        "VERIFY-RELAY refusal: KO HTTP 200 None",
        "VERIFY-RELAY sensitive: SKIP no sensitive role named",
        "VERIFY-RELAY output: OK",
        "VERIFY-RELAY: 3/5 OK",
    ]


def test_verify_lines_relay_bad_role(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert verify_relay.main(["bad"]) == 1
    lines = _only_pinned_lines("VERIFY-RELAY", capsys.readouterr().out)
    assert lines[-1] == "VERIFY-RELAY: 1/5 OK"
    assert verify_relay.UNKNOWN == "zz__unknown"


@pytest.mark.parametrize(
    ("target", "args", "prefix", "banner"),
    [
        ("verify-auth", ["a/b/c/d"], "VERIFY-AUTH", "VERIFY-AUTH: 0/4 OK"),
        ("verify-connections", ["a/b"], "VERIFY-CONNECTIONS", "VERIFY-CONNECTIONS: 0/4 OK"),
        ("verify-connections", [""], "VERIFY-CONNECTIONS", "VERIFY-CONNECTIONS: 0/4 OK"),
        ("verify-relay", ["a/b/c/d"], "VERIFY-RELAY", "VERIFY-RELAY: 1/5 OK"),
        ("verify-relay", ["a/b"], "VERIFY-RELAY", "VERIFY-RELAY: 1/5 OK"),
        ("verify-admin", [], "VERIFY-ADMIN", "VERIFY-ADMIN: 0/4 OK"),
    ],
)
def test_verify_lines_ops_fallback_banner(
    tmp_path: Path, target: str, args: list[str], prefix: str, banner: str
) -> None:
    """The shell side: without `.env` (or with a malformed name) no instrument runs, and
    `deploy/ops.sh` still prints the check lines and the banner in the pinned vocabulary."""
    env_file = tmp_path / ".env"  # absent
    result = subprocess.run(
        ["sh", "deploy/ops.sh", target, *args],
        cwd=ROOT,
        env={"ENV_FILE": str(env_file), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    lines = _only_pinned_lines(prefix, result.stdout)
    assert lines[-1] == banner


def test_verify_lines_ops_greps_the_new_banners() -> None:
    """AC-23: `deploy/ops.sh` recognises the instruments' banners by their new spelling."""
    ops = (ROOT / "deploy" / "ops.sh").read_text(encoding="utf-8")
    assert ops.count("grep -q '^VERIFY-CONNECTIONS: [0-9]/4 OK$'") == 1
    assert ops.count("grep -q '^VERIFY-RELAY: [0-9]/5 OK$'") == 1
    assert "python -m hullgate.verify_connections" in ops
    assert "python -m hullgate.verify_relay" in ops
    for name in ("verify-auth", "verify-connections", "verify-relay"):
        assert f"    {name}) verify_" in ops
