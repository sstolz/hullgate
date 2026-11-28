"""BR-105 — the SonarQube token never leaves its file nor the scanner's environment."""

from __future__ import annotations

import re
import subprocess

import pytest

from tests.qa_support import ROOT
from tests.unit.test_br_104_qa_sonar_guard import TOKEN, Fakes, run_sonar


@pytest.fixture
def fakes(tmp_path_factory: pytest.TempPathFactory) -> Fakes:
    made = Fakes(tmp_path_factory.mktemp("sonar-token"))
    made.token_file.write_text(TOKEN + "\n", encoding="utf-8")
    return made


def _everything_visible(fakes: Fakes, proc: subprocess.CompletedProcess[str]) -> str:
    parts = [proc.stdout, proc.stderr]
    for path in (fakes.log, fakes.args):
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_br_105_token_reaches_the_scanner_by_environment_only(fakes: Fakes) -> None:
    proc = run_sonar(fakes, FAKE_DOCKER_MODE="ok")
    assert proc.stdout.startswith("QA sonar info OK — ")
    assert fakes.env_seen.read_text(encoding="utf-8") == TOKEN  # the scanner received it
    assert TOKEN not in _everything_visible(fakes, proc)  # neither argument, nor output, nor log
    call = fakes.docker_calls()[0]
    assert " -e SONAR_TOKEN " in call  # the variable's name, never its value


def test_br_105_token_is_never_echoed_even_when_the_scanner_prints_it(fakes: Fakes) -> None:
    proc = run_sonar(fakes, FAKE_DOCKER_MODE="401")
    assert proc.stdout.startswith("QA sonar info ERROR — ")
    assert "renew" in proc.stdout  # free text (HULL-57)
    assert "SONAR_TOKEN_FILE" in proc.stdout
    assert TOKEN not in _everything_visible(fakes, proc)
    assert proc.returncode == 1  # ERROR when informative: visible by hand


def test_br_105_missing_token_is_a_skip_naming_the_expected_path(fakes: Fakes) -> None:
    fakes.token_file.unlink()
    proc = run_sonar(fakes)
    assert proc.stdout.startswith("QA sonar info SKIP — ")
    assert "SONAR_TOKEN_FILE" in proc.stdout
    assert fakes.docker_calls() == []


def test_br_105_empty_token_file_is_a_skip(fakes: Fakes) -> None:
    fakes.token_file.write_text("\n", encoding="utf-8")
    assert run_sonar(fakes).stdout.startswith("QA sonar info SKIP — ")
    assert fakes.docker_calls() == []


def test_br_105_unreachable_server_is_a_skip_and_scanner_failure_an_error(fakes: Fakes) -> None:
    down = run_sonar(fakes, FAKE_DOCKER_MODE="down")
    assert down.stdout.startswith("QA sonar info SKIP — ")
    assert "unreachable" in down.stdout
    boom = run_sonar(fakes, FAKE_DOCKER_MODE="boom")
    assert boom.stdout.startswith("QA sonar info ERROR — ")
    assert "code 3" in boom.stdout
    assert TOKEN not in _everything_visible(fakes, boom)


def test_br_105_make_sonar_output_never_holds_the_token(fakes: Fakes) -> None:
    proc = subprocess.run(
        ["make", "sonar"],
        capture_output=True,
        text=True,
        check=False,
        env=fakes.env(FAKE_DOCKER_MODE="401"),
        cwd=ROOT,
    )
    assert TOKEN not in _everything_visible(fakes, proc)
    assert "make: *** " in proc.stderr or proc.returncode != 0


def test_br_105_no_tracked_file_holds_a_token_value() -> None:
    files = subprocess.run(
        ["git", "ls-files", "-z", "--", ".", ":!uv.lock"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout.split("\0")
    for name in filter(None, files):
        path = ROOT / name
        if path.is_file() and path.stat().st_size < 2_000_000:
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert not re.search(r"\bsq[pua]_[0-9a-f]{40}\b", text), name


def test_br_105_neither_token_variable_skips_without_scanner(fakes: Fakes) -> None:
    env = fakes.env()
    env.pop("SONAR_TOKEN", None)
    env.pop("SONAR_TOKEN_FILE", None)
    env["FAKE_BRANCH"] = "feature-branch"  # Missing configuration is named on any branch.
    proc = subprocess.run(
        ["make", "--no-print-directory", "sonar"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "SKIP" in proc.stdout
    assert "SONAR_TOKEN and SONAR_TOKEN_FILE" in proc.stdout
    assert fakes.docker_calls() == []


def test_br_105_environment_token_precedes_file(fakes: Fakes) -> None:
    fakes.token_file.write_text("different-fake-token")
    proc = run_sonar(fakes, SONAR_TOKEN=TOKEN, FAKE_DOCKER_MODE="ok")
    assert fakes.env_seen.read_text() == TOKEN
    assert TOKEN not in _everything_visible(fakes, proc)
    assert " -e SONAR_HOST_URL " in fakes.docker_calls()[0]
