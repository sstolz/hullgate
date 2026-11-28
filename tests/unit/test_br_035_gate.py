"""BR-035 v2 — the quality gate is one command: seven steps, in order (F-016: web)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXPECTED = [
    ("lock", "lock --check"),
    ("format", "ruff format --check"),
    ("lint", "ruff check ."),
    ("typecheck", "mypy --strict"),
    ("tests", "pytest tests/unit tests/integration"),
    ("sdk-guard", "python scripts/sdk_guard.py"),
    ("web", "$(WEB) gate"),  # F-016, BR-035 v2
]


def _gate_recipe() -> list[str]:
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = lines.index("gate:")
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return recipe


def test_br_035_gate_starts_with_locked_sync() -> None:
    recipe = _gate_recipe()
    assert "sync --locked" in recipe[1]


def test_br_035_gate_runs_seven_steps_in_order() -> None:
    steps = [line for line in _gate_recipe() if line.startswith("$(call gate_step,")]
    assert len(steps) == len(EXPECTED)
    for line, (name, command) in zip(steps, EXPECTED, strict=True):
        assert line.startswith(f"$(call gate_step,{name},"), line
        assert command in line, (name, line)


def test_br_035_gate_ends_with_banner() -> None:
    assert _gate_recipe()[-1] == "@echo 'GATE: OK'"


def test_br_035_each_step_prints_version_and_fails_with_ko() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    macro = text[text.index("define gate_step") : text.index("endef")]
    assert "$(2)" in macro  # version command printed
    assert "GATE: KO $(1)" in macro
    assert "exit 1" in macro
