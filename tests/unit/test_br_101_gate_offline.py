"""BR-101 — the gate carries no analysis tool, except the coverage measurement (AC-04)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _gate_recipe() -> list[str]:
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = lines.index("gate:")
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return recipe


def test_br_101_gate_recipe_calls_no_qa_tool() -> None:
    for line in _gate_recipe():
        assert not re.search(r"\bqa\b|\bqa-|\bsonar\b|\bpip-audit\b|\bpip_audit\b", line), line


def test_br_101_gate_still_has_the_seven_steps_of_br_035() -> None:
    steps = [line for line in _gate_recipe() if line.startswith("$(call gate_step,")]
    assert len(steps) == 7  # BR-035 v2 (F-016): the `web` step
    assert _gate_recipe()[-1] == "@echo 'GATE: OK'"


def test_br_101_tests_step_writes_coverage_without_threshold() -> None:
    (step,) = [line for line in _gate_recipe() if line.startswith("$(call gate_step,tests,")]
    assert "pytest tests/unit tests/integration" in step
    assert "--cov " in step + " "
    assert "--cov-report=xml" in step
    assert "fail-under" not in step
    assert "fail_under" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_br_101_coverage_config_measures_src_with_relative_paths() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "coverage"
    ]
    assert config["run"]["source"] == ["src"]
    assert config["run"]["relative_files"] is True
    assert config["xml"]["output"] == "coverage.xml"


def test_br_101_coverage_artefacts_are_ignored_by_git() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/coverage.xml" in ignored
    assert ".scannerwork/" in ignored
