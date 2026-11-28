"""BR-099 — the role is declared in one place; only a blocking tool fails `make qa`."""

from __future__ import annotations

from pathlib import Path

import pytest

import qa
from tests.qa_support import fake_tool, run_make

ALL = [(role, verdict) for role in qa.ROLES for verdict in qa.VERDICTS]


@pytest.mark.parametrize(("role", "verdict"), ALL)
def test_br_099_aggregator_fails_iff_blocking_findings_or_error(
    tmp_path: Path, role: str, verdict: str
) -> None:
    proc = run_make(tmp_path, f"t:{role}", {"t": fake_tool("t", verdict)})
    # a blocking SKIP is an ERROR (§3.1, BR-098): the aggregator does not let it through
    fails = role == "blocking" and verdict in ("FINDINGS", "ERROR", "SKIP")
    last = proc.stdout.splitlines()[-1]
    assert (proc.returncode != 0) is fails, proc.stdout
    assert last == ("QA: KO t" if fails else "QA: OK")


def test_br_099_ko_lists_only_blocking_failures_in_declared_order(tmp_path: Path) -> None:
    targets = {
        "a": fake_tool("a", "FINDINGS"),
        "b": fake_tool("b", "ERROR"),
        "c": fake_tool("c", "OK"),
        "d": fake_tool("d", "ERROR"),
    }
    proc = run_make(tmp_path, "a:blocking b:info c:blocking d:blocking", targets)
    assert proc.returncode != 0
    assert proc.stdout.splitlines()[-1] == "QA: KO a d"


def test_br_099_info_tools_never_change_the_ending(tmp_path: Path) -> None:
    targets = {"a": fake_tool("a", "ERROR"), "b": fake_tool("b", "SKIP")}
    proc = run_make(tmp_path, "a:info b:info", targets)
    assert proc.returncode == 0
    assert proc.stdout.splitlines()[-1] == "QA: OK"
    assert "QA a info ERROR" in proc.stdout  # the line stays visible


def test_br_099_role_comes_from_qa_tools_only(tmp_path: Path) -> None:
    targets = {"t": fake_tool("t", "FINDINGS")}
    as_info = run_make(tmp_path, "t:info", targets)
    as_blocking = run_make(tmp_path, "t:blocking", targets)
    assert as_info.returncode == 0
    assert as_blocking.returncode != 0
    assert "QA t info FINDINGS" in as_info.stdout
    assert "QA t blocking FINDINGS" in as_blocking.stdout


@pytest.mark.parametrize(("role", "verdict"), ALL)
def test_br_099_solo_exit_code(role: str, verdict: str) -> None:
    # §3.1: blocking, FINDINGS or ERROR (a blocking SKIP becomes ERROR); informative, ERROR
    failing = {"blocking": ("FINDINGS", "ERROR", "SKIP"), "info": ("ERROR",)}
    effective = qa.effective_verdict(role, verdict)
    assert qa.exit_code(role, effective) == (1 if verdict in failing[role] else 0)


def test_br_099_blocking_skip_becomes_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = qa.report("t", "blocking", "SKIP", "nothing to do")
    assert capsys.readouterr().out == "QA t blocking ERROR — nothing to do\n"
    assert code == 1
