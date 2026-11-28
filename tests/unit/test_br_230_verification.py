"""BR-230: exercise the real shell verdicts with local, deterministic probe responses."""

from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP = "mcp.example.test"
ADMIN = "admin.example.test"


@pytest.mark.parametrize(
    ("admin_host", "public_status", "isolation", "expected"),
    [
        ("", 404, True, "OK"),
        (ADMIN, 404, True, "OK"),
        (MCP, 200, True, "OK"),
        (ADMIN, 200, True, "KO"),
        (ADMIN, 404, False, "KO"),
    ],
)
def test_br_230_smoke_aggregates_public_isolation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    admin_host: str,
    public_status: int,
    isolation: bool,
    expected: str,
) -> None:
    probe = runpy.run_path(str(ROOT / "deploy/smoke/probe.py"))
    traefik = probe["traefik"]
    routers = [
        {
            "provider": "docker",
            "name": "hullgate@docker",
            "entryPoints": ["websecure"],
            "tls": {"certResolver": "le"},
        }
    ]
    services = [
        {
            "provider": "docker",
            "name": "hullgate@docker",
            "loadBalancer": {"servers": [{"url": "http://gateway:4790"}]},
        }
    ]
    if admin_host:
        routers.append(
            {
                "provider": "docker",
                "name": "hullgate-admin@docker",
                "entryPoints": ["websecure"],
                "tls": {"certResolver": "le"},
                "service": "hullgate-admin",
                "rule": f"Host(`{admin_host}`) && PathPrefix(`/admin`)",
            }
        )
        services.append(
            {
                "provider": "docker",
                "name": "hullgate-admin@docker",
                "loadBalancer": {"servers": [{"url": "http://gateway:4791"}]},
            }
        )

    def api(path: str) -> Any:
        return routers if path.endswith("routers") else services

    def status(host: str, path: str) -> int:
        if path == "/healthz":
            return 200
        if path == "/admin/api/v1/session":
            return 401
        return public_status if host == MCP else 200

    monkeypatch.setitem(traefik.__globals__, "api", api)
    monkeypatch.setitem(traefik.__globals__, "https_status", status)
    traefik(MCP, "websecure", "le", admin_host)
    output = capsys.readouterr().out
    if not isolation:
        output = "\n".join(line for line in output.splitlines() if "public-admin-404" not in line)
    output += "\nCHECK no-public-port OK\nCHECK admin-loopback-only OK\n"
    output += "CHECK admin-route-config OK\n" if admin_host else "CHECK no-admin-label OK\n"
    (tmp_path / "probe.log").write_text(output)
    # Execute the production aggregation and result counter, without the Docker/deploy stages.
    smoke = (ROOT / "deploy/smoke/smoke.sh").read_text()
    result_function = smoke.split("result() {", 1)[1].split("\ncleanup()", 1)[0]
    aggregation = smoke.split("all_ok() {", 1)[1].split("# 5.", 1)[0]
    script = (
        "set -u\nWORK=$1; HOST=$2; ADMIN_HOST=$3; ok=0\n"
        'say() { printf "%s\\n" "$*"; }\nresult() {'
        + result_function
        + "\nall_ok() {"
        + aggregation
        + '\n[ "$ok" -eq 2 ]\n'
    )
    result = subprocess.run(
        ["sh", "-c", script, "smoke-test", str(tmp_path), MCP, admin_host],
        capture_output=True,
        text=True,
    )
    name = "admin-routed" if admin_host else "admin-not-routed"
    assert f"SMOKE {name} {expected}" in result.stdout, result.stdout
    assert (result.returncode == 0) == (expected == "OK"), result.stderr


# All HTTP is intercepted: these tests cannot reach a deployment or invoke Docker.
OPS_STUBS = r"""
compose() { printf '  "public_id": "abcdefghijkl"\n'; }
backup_core() { printf 'BACKUP: OK test.db\n'; }
restore_check_core() { return 0; }
curl() {
  request_host=''; headers=0; url=''
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -H) request_host=$2; shift ;;
      -D) headers=1; shift ;;
      http://*|https://*) url=$1 ;;
    esac
    shift
  done
  case "$url" in
    */healthz|*/readyz) printf 200 ;;
    http://127.0.0.1:*/admin*)
      if [ "$request_host" != "Host: $EXPECTED_HOST" ]; then printf 421
      elif [ "$headers" = 1 ]; then
        printf "Content-Security-Policy: default-src 'self'; frame-ancestors 'none'\r\n"
      else printf 401; fi ;;
    https://*/admin/api/v1/session) printf '%s' "$API_STATUS" ;;
    https://*/admin)
      if [ "$url" = "https://$PUBLIC_HOST/admin" ]; then printf '%s' "$PUBLIC_STATUS"
      else printf '%s' "$SHELL_STATUS"; fi ;;
    */.well-known/*)
      printf '{"resource":"https://%s/o/abcdefghijkl/p/a/b/c/d/mcp"}\n200' "$PUBLIC_HOST" ;;
    */mcp)
      metadata="https://$PUBLIC_HOST/.well-known/oauth-protected-resource"
      metadata="$metadata/o/abcdefghijkl/p/a/b/c/d/mcp"
      printf 'HTTP/1.1 401 Unauthorized\r\n'
      printf 'WWW-Authenticate: Bearer resource_metadata="%s"\r\n' "$metadata" ;;
    *) return 99 ;;
  esac
}
"""


@pytest.mark.parametrize("mode", ["tunnel", "dedicated", "shared"])
@pytest.mark.parametrize("command", ["verify_admin", "verify_auth", "verify"])
def test_br_230_ops_verifies_effective_origin(tmp_path: Path, mode: str, command: str) -> None:
    result = _ops(tmp_path, mode, command)
    assert result.returncode == 0, result.stdout + result.stderr
    count = "6/6" if command == "verify" else "4/4"
    assert f": {count} OK" in result.stdout


@pytest.mark.parametrize(
    ("mode", "public_status", "shell_status", "api_status"),
    [
        ("tunnel", 200, 200, 401),
        ("dedicated", 200, 200, 401),
        ("dedicated", 404, 404, 401),
        ("shared", 404, 200, 401),
        ("shared", 200, 200, 200),
        ("dedicated", 404, 200, 200),
    ],
)
def test_br_230_ops_rejects_routing_deviations(
    tmp_path: Path, mode: str, public_status: int, shell_status: int, api_status: int
) -> None:
    result = _ops(tmp_path, mode, "verify", public_status, shell_status, api_status)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "VERIFY: 5/6 OK" in result.stdout


def _ops(
    tmp_path: Path,
    mode: str,
    command: str,
    public_status: int | None = None,
    shell_status: int = 200,
    api_status: int = 401,
) -> subprocess.CompletedProcess[str]:
    admin_host = {"tunnel": "", "dedicated": ADMIN, "shared": MCP}[mode]
    # A nondefault tunnel port and an unused ADMIN_URL catch composition/probe drift.
    origin = f"https://{admin_host}" if admin_host else "https://unused.example.test"
    (tmp_path / ".env").write_text(
        f"MCP_HOST={MCP}\nADMIN_LOCAL_PORT=24791\nADMIN_HOST={admin_host}\n"
        f"ADMIN_URL={origin}\nTRUSTED_PROXIES=10.0.0.0/8\n"
    )
    (tmp_path / ".deployed-ref").write_text("test-revision\n")
    status = public_status if public_status is not None else (200 if mode == "shared" else 404)
    script = 'OPS_LIB=1; . "$1"\n' + OPS_STUBS + '\n"$2" a/b/c/d\n'
    return subprocess.run(
        ["sh", "-c", script, "ops-test", str(ROOT / "deploy/ops.sh"), command],
        cwd=tmp_path,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PUBLIC_HOST": MCP,
            "EXPECTED_HOST": admin_host or "localhost:24791",
            "PUBLIC_STATUS": str(status),
            "SHELL_STATUS": str(shell_status),
            "API_STATUS": str(api_status),
        },
        capture_output=True,
        text=True,
    )
