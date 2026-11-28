"""BR-104 — SonarQube analyses only `main`, clean tree (AC-05); fake `git` and fake `docker`."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import qa_contract

from tests.qa_support import ROOT

FAKE_GIT = """#!/bin/sh
echo "git $*" >> "$FAKE_LOG"
case "$1" in
  rev-parse)
    if [ "$2" = HEAD ]; then echo "${FAKE_HEAD_SHA:-aaaa}"; else echo "${FAKE_BRANCH:-main}"; fi ;;
  status) printf '%s' "$FAKE_DIRTY" ;;
  ls-files) printf '%s' "$FAKE_UNTRACKED" ;;
esac
"""

# The fake docker records its call and the environment it received (token), and returns the
# output of the requested mode.
FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
echo "docker $*" >> "$FAKE_DOCKER_ARGS"
printf '%s' "$SONAR_TOKEN" > "$FAKE_DOCKER_ENV"
case "$FAKE_DOCKER_MODE" in
  ok)
    echo "INFO ANALYSIS SUCCESSFUL, you can find the results at: https://qube.example/dashboard"
    exit 0 ;;
  401)
    echo "ERROR Not authorized. Please check the properties sonar.token for $SONAR_TOKEN" >&2
    exit 1 ;;
  down) echo "ERROR Failed to query server version: Connection refused" >&2; exit 1 ;;
  *) echo "ERROR boom" >&2; exit 3 ;;
esac
"""

TOKEN = "squ_FAKE_0123456789abcdef"  # noqa: S105 - fake value


class Fakes:
    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        self.args = tmp_path / "docker-args.log"
        self.env_seen = tmp_path / "docker-env.txt"
        self.token_file = tmp_path / "token"
        self.coverage = tmp_path / "coverage.xml"
        self.coverage.write_text("<coverage/>", encoding="utf-8")
        (tmp_path / "coverage.sha").write_text("aaaa\n", encoding="utf-8")  # written by the gate
        for name, body in (("git", FAKE_GIT), ("docker", FAKE_DOCKER)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def env(self, **extra: str) -> dict[str, str]:
        base = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_DOCKER_ARGS": str(self.args),
            "FAKE_DOCKER_ENV": str(self.env_seen),
            "SONAR_TOKEN_FILE": str(self.token_file),
            "SONAR_HOST_URL": "https://sonar.example.test",
            "QA_SONAR_COVERAGE_FILE": str(self.coverage),
        }
        base.pop("SONAR_TOKEN", None)
        return {**base, **extra}

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def docker_calls(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("docker ")]


def run_sonar(fakes: Fakes, *flags: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/qa_sonar.py"), "info", "scanner@sha256:abc", *flags],
        capture_output=True,
        text=True,
        check=False,
        env=fakes.env(**env),
        cwd=fakes.bin.parent,
    )


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    made = Fakes(tmp_path)
    made.token_file.write_text(TOKEN + "\n", encoding="utf-8")
    return made


def test_br_104_on_a_work_branch_it_skips_without_calling_the_server(fakes: Fakes) -> None:
    proc = run_sonar(fakes, FAKE_BRANCH="feat/HULL-40-x")
    assert proc.stdout.startswith("QA sonar info SKIP — ")
    assert "feat/HULL-40-x" in proc.stdout
    assert proc.returncode == 0
    assert fakes.docker_calls() == []


def test_br_104_on_main_with_a_tracked_modification_it_skips(fakes: Fakes) -> None:
    proc = run_sonar(fakes, FAKE_DIRTY=" M src/hullgate/app.py\n")
    assert proc.stdout.startswith("QA sonar info SKIP — ")
    assert "dirty" in proc.stdout
    assert fakes.docker_calls() == []


def test_br_104_on_main_with_an_untracked_file_under_src_or_tests_it_skips(fakes: Fakes) -> None:
    proc = run_sonar(fakes, FAKE_UNTRACKED="tests/unit/test_new.py\n")
    assert proc.stdout.startswith("QA sonar info SKIP — ")
    assert fakes.docker_calls() == []


def test_br_104_under_the_aggregator_it_skips_even_on_clean_main(fakes: Fakes) -> None:
    proc = run_sonar(fakes, "--aggregate")
    assert proc.stdout == "QA sonar info SKIP — analysis on demand: run make sonar\n"
    assert fakes.calls() == []  # neither git nor docker: nothing is even read


def test_br_104_through_make_qa_the_tool_is_skipped_and_never_calls_docker(
    fakes: Fakes,
) -> None:
    proc = subprocess.run(
        ["make", "--no-print-directory", "qa", "QA_TOOLS=sonar:info"],
        capture_output=True,
        text=True,
        check=False,
        env=fakes.env(),
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "QA sonar info SKIP — analysis on demand: run make sonar" in proc.stdout
    assert proc.stdout.splitlines()[-1] == "QA: OK"
    assert fakes.docker_calls() == []


def test_br_104_on_clean_main_through_make_sonar_the_scanner_runs_once(fakes: Fakes) -> None:
    proc = subprocess.run(
        ["make", "--no-print-directory", "sonar"],
        capture_output=True,
        text=True,
        check=False,
        env=fakes.env(FAKE_DOCKER_MODE="ok"),
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "QA sonar info OK — analysis of main sent: https://qube.example/dashboard" in proc.stdout
    assert len(fakes.docker_calls()) == 1
    assert "@sha256:" in fakes.docker_calls()[0]  # the pinned image of the Makefile


def test_br_104_the_line_is_conforming(fakes: Fakes) -> None:
    out = run_sonar(fakes, FAKE_BRANCH="x").stdout
    assert qa_contract.line_problems("sonar", "info", out) == []


# --- N2 / N1 (review 426): fresh coverage.xml, and only what the scanner reads ----------------


def test_br_104_missing_coverage_is_a_skip_and_never_reaches_the_scanner(fakes: Fakes) -> None:
    fakes.coverage.unlink()
    proc = run_sonar(fakes)
    assert proc.stdout.startswith("QA sonar info SKIP — coverage.xml absent")
    assert fakes.docker_calls() == []


def test_br_104_coverage_from_another_commit_is_a_skip(fakes: Fakes) -> None:
    # the case of review 428: coverage made on another branch, then checkout of main
    proc = run_sonar(fakes, FAKE_HEAD_SHA="bbbb")
    assert proc.stdout.startswith("QA sonar info SKIP — coverage.xml tested on a commit other")
    assert fakes.docker_calls() == []


def test_br_104_coverage_without_a_recorded_commit_is_a_skip(fakes: Fakes) -> None:
    (fakes.coverage.parent / "coverage.sha").unlink()
    proc = run_sonar(fakes)
    assert proc.stdout.startswith("QA sonar info SKIP — coverage.sha absent")
    assert fakes.docker_calls() == []


def test_br_104_coverage_tested_on_the_head_goes_to_the_scanner_whatever_the_dates(
    fakes: Fakes,
) -> None:
    os.utime(fakes.coverage, (1_000_000, 1_000_000))  # old: no effect, only the SHA counts
    proc = run_sonar(fakes, FAKE_HEAD_SHA="aaaa", FAKE_DOCKER_MODE="ok")
    assert proc.stdout.startswith("QA sonar info OK — ")
    assert len(fakes.docker_calls()) == 1


def test_br_104_scanner_mounts_only_what_it_reads_read_only(fakes: Fakes) -> None:
    run_sonar(fakes, FAKE_DOCKER_MODE="ok")
    args = fakes.docker_calls()[0].split()
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    targets = sorted(m.split(":")[1] for m in mounts)
    assert targets == [
        "/usr/src/coverage.xml",
        "/usr/src/sonar-project.properties",
        "/usr/src/src",
        "/usr/src/tests",
    ]
    assert all(m.endswith(":ro") for m in mounts)
    assert not any(".cairn" in m or m.split(":")[1] == "/usr/src" for m in mounts)


# --- HULL-45: coverage.sha is written only on a clean tree, by the same definition as the guard ---


def run_stamp(fakes: Fakes, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/qa_tree.py"), "coverage.sha"],
        capture_output=True,
        text=True,
        check=False,
        env=fakes.env(**env),
        cwd=fakes.bin.parent,
    )


def test_br_104_the_gate_writes_coverage_sha_on_a_clean_tree(fakes: Fakes) -> None:
    sha = fakes.coverage.with_name("coverage.sha")
    sha.unlink()
    proc = run_stamp(fakes, FAKE_HEAD_SHA="cccc")
    assert proc.returncode == 0
    assert sha.read_text(encoding="utf-8").strip() == "cccc"


@pytest.mark.parametrize(
    "dirt",
    [{"FAKE_DIRTY": " M src/hullgate/app.py\n"}, {"FAKE_UNTRACKED": "tests/unit/test_new.py\n"}],
    ids=["tracked-modification", "untracked-under-src-or-tests"],
)
def test_br_104_the_gate_leaves_coverage_sha_absent_on_a_dirty_tree(
    fakes: Fakes, dirt: dict[str, str]
) -> None:
    sha = fakes.coverage.with_name("coverage.sha")
    assert sha.exists()  # left over by a previous gate: it must disappear, not survive
    proc = run_stamp(fakes, **dirt)
    assert proc.returncode == 0  # a gate on a dirty tree stays possible, only the proof is missing
    assert not sha.exists()
    assert "dirty" in proc.stdout


def test_br_104_dirty_gate_then_revert_ends_in_skip_not_in_a_stale_rate(fakes: Fakes) -> None:
    # the scenario of HULL-45: gate on a dirty tree, modifications reverted, HEAD unchanged
    run_stamp(fakes, FAKE_DIRTY=" M src/hullgate/app.py\n")
    proc = run_sonar(fakes, FAKE_DOCKER_MODE="ok")  # tree clean again
    assert proc.stdout.startswith("QA sonar info SKIP — coverage.sha absent")
    assert "run make gate again" in proc.stdout
    assert fakes.docker_calls() == []


def test_br_104_unreadable_git_leaves_coverage_sha_absent(fakes: Fakes) -> None:
    (fakes.bin / "git").write_text("#!/bin/sh\nexit 128\n", encoding="utf-8")
    sha = fakes.coverage.with_name("coverage.sha")
    proc = run_stamp(fakes)
    assert proc.returncode == 0
    assert not sha.exists()
    assert "unreadable" in proc.stdout


def test_br_104_the_gate_and_the_guard_share_one_definition_of_a_clean_tree() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "rev-parse HEAD > coverage.sha" not in makefile
    assert "scripts/qa_tree.py coverage.sha" in makefile
    assert "qa_tree.dirty_reason()" in (ROOT / "scripts/qa_sonar.py").read_text(encoding="utf-8")


def test_br_104_missing_host_through_make_skips_without_calls(fakes: Fakes) -> None:
    env = fakes.env()
    env.pop("SONAR_HOST_URL", None)
    proc = subprocess.run(
        ["make", "--no-print-directory", "sonar"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "SKIP — SONAR_HOST_URL unset" in proc.stdout
    assert fakes.calls() == []
    properties = (ROOT / "sonar-project.properties").read_text()
    assert "sonar.host.url" not in properties
