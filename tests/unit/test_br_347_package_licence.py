"""BR-347: the package metadata declares the MIT licence."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_BR_347_001_pyproject_declares_mit() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project.get("license") == "MIT"
