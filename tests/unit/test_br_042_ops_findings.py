"""BR-042 / AC-OP — retouches from review 1 of HULL-6 (N1, N2, N4).

N1: the line reading the Traefik labels (O1) reads the list form `k=v` and the map form `k: v`.
N2: the Gitea host of O4's clone is a read value, not a hard-coded name (HTTPS since HULL-58).
N4: `deploy/ops.sh verify` under a held lock prints the banner `VERIFY: 0/6` and exits non-zero.
"""

from __future__ import annotations

import fcntl
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "docs/guides/install.md"


def _o1_label_command() -> str:
    text = INSTALL.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("grep -rhoE 'traefik")]
    assert len(lines) == 1, lines
    return lines[0]


def test_br_042_o1_label_regex_reads_list_and_map_forms(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "compose.yml").write_text(
        '    labels:\n      - "traefik.docker.network=proxy"\n'
        '      - "traefik.http.routers.foo.entrypoints=websecure"\n'
        "      - traefik.http.routers.foo.tls.certresolver=le\n",
        encoding="utf-8",
    )
    (tmp_path / "b" / "compose.yaml").write_text(
        "    labels:\n      traefik.docker.network: proxy\n"
        "      traefik.http.routers.bar.entrypoints: websecure\n"
        '      traefik.http.routers.bar.tls.certresolver: "le"\n'
        '      "traefik.http.routers.baz.entrypoints": websecure\n'
        "      traefik.http.routers.bar.rule: Host(`x`)\n",
        encoding="utf-8",
    )
    cmd = _o1_label_command().replace("<compose-root>", str(tmp_path))
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=True).stdout
    got = {ln.split(None, 1)[1]: int(ln.split(None, 1)[0]) for ln in out.strip().splitlines()}
    assert got == {
        "traefik.docker.network=proxy": 2,
        "traefik.http.routers.<x>.entrypoints=websecure": 3,
        "traefik.http.routers.<x>.tls.certresolver=le": 2,
    }


def test_br_042_o3_clone_host_is_a_placeholder() -> None:
    """O3 selects the repository URL; O4 clones it, without embedding credentials."""
    text = INSTALL.read_text(encoding="utf-8")
    clone = [ln for ln in text.splitlines() if "git clone " in ln]
    assert clone == ['git clone "$REPOSITORY_URL" /opt/hullgate &&']
    assert "REPOSITORY_URL='<repository-url>'" in text
    assert "https://github.com/sstolz/hullgate" in text
    assert "\nHost " not in text
    assert "ssh-keygen" not in text


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock absent")
def test_br_038_verify_under_busy_lock_prints_banner(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MCP_HOST=x.invalid\n", encoding="utf-8")
    (tmp_path / "backups").mkdir()
    with (tmp_path / "backups" / ".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = subprocess.run(
            ["sh", str(ROOT / "deploy/ops.sh"), "verify"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path)},
            check=False,
        )
    assert r.returncode != 0
    assert r.stdout.startswith("VERIFY: 0/6 — lock held"), r.stdout
    assert "VERIFY readyz" not in r.stdout
