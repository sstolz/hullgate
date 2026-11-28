"""BR-098, BR-099 — the aggregator checks the QA line of each target, even one written in shell.

Review 426 (B1): a shell target declared `blocking` that printed `PASS`, `SKIP`, `findings`, or
an `OK` line followed by `exit 3` took `make qa` to `QA: OK`. These cases are replayed here with
targets that use none of the shipped helpers (`qa.py`), the path the next tool will take.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import qa_contract

from tests.qa_support import run_make

HEADER = "@echo '== QA t : fake 1.0'\n"


def _shell_tool(*lines: str) -> str:
    return HEADER + "\n".join(lines)


@pytest.mark.parametrize(
    ("label", "recipe"),
    [
        ("verdict outside the set (PASS)", _shell_tool("@echo 'QA t blocking PASS — ok'")),
        ("blocking SKIP", _shell_tool("@echo 'QA t blocking SKIP — nothing'")),
        ("lowercase verdict", _shell_tool("@echo 'QA t blocking findings — x'")),
        ("OK then exit code 3", _shell_tool("@echo 'QA t blocking OK — ok'", "@exit 3")),
        ("FINDINGS, zero exit code", _shell_tool("@echo 'QA t blocking FINDINGS — x'")),
        ("wrong role in the line", _shell_tool("@echo 'QA t info OK — ok'")),
        ("wrong name in the line", _shell_tool("@echo 'QA u blocking OK — ok'")),
        ("separator absent", _shell_tool("@echo 'QA t blocking OK - ok'")),
        ("no version line", "@echo 'QA t blocking OK — ok'"),
        ("empty version line", "@echo '== QA t : '\n@echo 'QA t blocking OK — ok'"),
        ("no QA line", _shell_tool("@true")),
    ],
)
def test_br_098_aggregator_fails_a_blocking_shell_target_off_contract(
    tmp_path: Path, label: str, recipe: str
) -> None:
    proc = run_make(tmp_path, "t:blocking", {"t": recipe})
    assert proc.returncode != 0, label + "\n" + proc.stdout
    assert proc.stdout.splitlines()[-1] == "QA: KO t", label


def test_br_098_aggregator_names_why_a_target_is_off_contract(tmp_path: Path) -> None:
    proc = run_make(tmp_path, "t:blocking", {"t": _shell_tool("@echo 'QA t blocking PASS — ok'")})
    assert "QA t blocking ERROR — non-conforming target" in proc.stdout


def test_br_098_aggregator_accepts_conforming_shell_targets(tmp_path: Path) -> None:
    ok = run_make(tmp_path, "t:blocking", {"t": _shell_tool("@echo 'QA t blocking OK — ok'")})
    assert ok.returncode == 0, ok.stdout
    assert ok.stdout.splitlines()[-1] == "QA: OK"
    failing = _shell_tool("@echo 'QA t blocking FINDINGS — x'", "@exit 1")
    ko = run_make(tmp_path, "t:blocking", {"t": failing})
    assert ko.stdout.splitlines()[-1] == "QA: KO t"
    assert "non-conforming target" not in ko.stdout  # a conforming failure is not a non-conformity


def test_br_098_off_contract_info_target_is_reported_but_never_fails(tmp_path: Path) -> None:
    proc = run_make(tmp_path, "t:info", {"t": _shell_tool("@echo 'QA t info PASS — ok'")})
    assert proc.returncode == 0, proc.stdout
    assert "QA t info ERROR — non-conforming target" in proc.stdout
    assert proc.stdout.splitlines()[-1] == "QA: OK"


def test_br_098_target_problems_cover_version_line_and_exit_code() -> None:
    good = "== QA x : v\nQA x blocking OK — ok\n"
    assert qa_contract.target_problems("x", "blocking", 0, good) == []
    assert qa_contract.target_problems("x", "blocking", 2, good)  # OK but non-zero code
    findings = "== QA x : v\nQA x blocking FINDINGS — x\n"
    assert qa_contract.target_problems("x", "blocking", 0, findings)
    assert qa_contract.target_problems("x", "blocking", 2, findings) == []
    assert qa_contract.target_problems("x", "info", 0, "QA x info OK — ok\n")  # without a version
    empty = "== QA x : \nQA x blocking OK — ok\n"  # empty version line (review 428, N-3)
    assert qa_contract.target_problems("x", "blocking", 0, empty)
    assert qa_contract.target_problems("x", "blocking", 0, "== QA x :\nQA x blocking OK — ok\n")
