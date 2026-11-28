"""BR-098 — an analysis tool is a target that returns a normalised QA line (AC-01, AC-02)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import qa_contract

import qa
from tests.qa_support import ROOT, fake_tool, run_make


def test_br_098_repository_satisfies_the_contract() -> None:
    assert qa_contract.static_problems(ROOT) == []


def test_br_098_first_batch_is_declared_with_roles_in_one_place() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    tools = dict(qa_contract.declared_tools(makefile))
    assert set(tools) == {"cov", "audit", "sonar", "web-audit", "secrets"}  # F-016: web-audit
    assert all(role in qa.ROLES for role in tools.values())


@pytest.mark.parametrize("name", ["cov", "audit", "sonar", "web-audit", "secrets"])
def test_br_098_every_declared_tool_has_a_target(name: str) -> None:
    proc = subprocess.run(
        ["make", "-n", f"qa-{name}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_br_098_aggregator_prints_one_version_line_and_one_qa_line_per_tool(
    tmp_path: Path,
) -> None:
    proc = run_make(
        tmp_path,
        "a:info b:blocking",
        {"a": fake_tool("a", "OK"), "b": fake_tool("b", "OK")},
    )
    out = proc.stdout.splitlines()
    assert proc.returncode == 0, proc.stderr
    assert [ln for ln in out if ln.startswith("== QA ")] == [
        "== QA a : fake 1.0",
        "== QA b : fake 1.0",
    ]
    assert [ln for ln in out if ln.startswith("QA ")] == [
        "QA a info OK — trial",
        "QA b blocking OK — trial",
    ]
    assert out[-1] == "QA: OK"


# --- AC-02: the contract test fails on each of these deviations ------------------------------


def _tree(tmp_path: Path, makefile: str, dev: str = '"ruff==0.17.3"') -> Path:
    (tmp_path / "Makefile").write_text(makefile, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        f"[dependency-groups]\ndev = [{dev}]\n", encoding="utf-8"
    )
    return tmp_path


def test_br_098_contract_accepts_a_conforming_tree(tmp_path: Path) -> None:
    root = _tree(tmp_path, "QA_TOOLS := x:info\nqa-x:\n\t@true\n")
    assert qa_contract.static_problems(root) == []


def test_br_098_contract_fails_on_tool_without_target(tmp_path: Path) -> None:
    root = _tree(tmp_path, "QA_TOOLS := x:info\n")
    assert any("no qa-x target" in p for p in qa_contract.static_problems(root))


def test_br_098_contract_fails_on_role_outside_the_set(tmp_path: Path) -> None:
    root = _tree(tmp_path, "QA_TOOLS := x:warn\nqa-x:\n\t@true\n")
    assert any("role 'warn'" in p for p in qa_contract.static_problems(root))


def test_br_098_contract_fails_on_unpinned_dev_dependency(tmp_path: Path) -> None:
    root = _tree(tmp_path, "QA_TOOLS := x:info\nqa-x:\n\t@true\n", dev='"ruff>=0.17"')
    assert any("not pinned" in p for p in qa_contract.static_problems(root))


def test_br_098_contract_fails_on_image_without_digest(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        "QA_TOOLS := x:info\nQA_IMAGE_x := scanner:12\nqa-x:\n\t@true\n",
    )
    assert any("without @sha256" in p for p in qa_contract.static_problems(root))


def test_br_098_contract_fails_on_zero_or_two_qa_lines() -> None:
    assert qa_contract.line_problems("x", "info", "== QA x : v1\n")
    two = "QA x info OK — a\nQA x info OK — b\n"
    assert qa_contract.line_problems("x", "info", two)


def test_br_098_contract_fails_on_verdict_outside_the_set() -> None:
    assert qa_contract.line_problems("x", "info", "QA x info PASS — ok\n")


def test_br_098_contract_fails_on_role_outside_the_set_in_line() -> None:
    assert qa_contract.line_problems("x", "info", "QA x warn OK — ok\n")


def test_br_098_contract_fails_on_blocking_skip_and_wrong_role() -> None:
    assert qa_contract.line_problems("x", "blocking", "QA x blocking SKIP — why\n")
    assert qa_contract.line_problems("x", "blocking", "QA x info OK — ok\n")


def test_br_098_contract_accepts_a_conforming_line() -> None:
    assert qa_contract.line_problems("x", "info", "== QA x : v\nQA x info SKIP — reason\n") == []


def test_br_098_aggregator_flags_a_target_printing_two_lines(tmp_path: Path) -> None:
    two = "@echo 'QA t info OK — a'\n@echo 'QA t info OK — b'"
    proc = run_make(tmp_path, "t:blocking", {"t": two})
    assert proc.returncode != 0
    assert "QA t blocking ERROR — non-conforming target — t: 2 QA line(s) instead of one" in (
        proc.stdout
    )
    assert proc.stdout.splitlines()[-1] == "QA: KO t"


def test_br_098_aggregator_flags_a_declared_tool_without_target(tmp_path: Path) -> None:
    proc = run_make(tmp_path, "ghost:blocking", {})
    assert proc.returncode != 0
    assert proc.stdout.splitlines()[-1] == "QA: KO ghost"
