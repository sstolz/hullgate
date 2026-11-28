"""Helpers of the analysis tool tests (F-007): a fake Makefile that includes the real one."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_make(
    tmp_path: Path, tools: str, fake_targets: dict[str, str], goal: str = "qa"
) -> subprocess.CompletedProcess[str]:
    """Run `make <goal>` with QA_TOOLS overridden and fake `qa-<name>` targets.

    `fake_targets`: name -> shell commands of the recipe (one line each).
    """
    assert shutil.which("make"), "make is required by the gate"
    parts = [f"include {ROOT / 'Makefile'}"]
    for name, recipe in fake_targets.items():
        parts.append(f"\n.PHONY: qa-{name}\nqa-{name}:")
        parts.extend(f"\t{line}" for line in recipe.splitlines())
    fake = tmp_path / "fake.mk"
    fake.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return subprocess.run(
        [
            "make",
            "-f",
            str(fake),
            "-C",
            str(ROOT),
            "--no-print-directory",
            goal,
            f"QA_TOOLS={tools}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def fake_tool(name: str, verdict: str) -> str:
    """Recipe of a fake target: version, QA line of the role read from QA_TOOLS, exit code.

    The exit code follows §3.1: non-zero iff FINDINGS/ERROR when blocking, ERROR when informative.
    """
    return (
        f"@echo '== QA {name} : fake 1.0'\n"
        f"@echo 'QA {name} '$(call qa_role,{name})' {verdict} — trial'\n"
        f"@case '$(call qa_role,{name})/{verdict}' in "
        "blocking/FINDINGS|blocking/ERROR|info/ERROR) exit 1 ;; esac"
    )
