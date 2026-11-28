"""BR-231 (AC-33) — a console lockfile with an unexempted known vulnerability fails the audit;
build-only findings are reported, never blocking. The tool enters `info` (BR-100)."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import qa_web_audit
from qa_audit import Exemption

import qa

ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 11, 14)
ADVISORY = "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"


def _audit(*names: str, prod: int = 3) -> dict[str, Any]:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            n: {"name": n, "severity": "high", "via": [{"source": 1, "url": ADVISORY}, "dep"]}
            for n in names
        },
        "metadata": {"dependencies": {"prod": prod, "dev": 100}},
    }


def _reports(prod: tuple[str, ...] = (), build: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"production": _audit(*prod), "all": _audit(*prod, *build)}


def test_br_231_clean_lock_is_ok() -> None:
    verdict, detail = qa_web_audit.judge(_reports(), [], TODAY)
    assert verdict == "OK"
    assert "3 production packages" in detail


def test_br_231_bundle_finding_without_exemption_is_findings() -> None:
    verdict, detail = qa_web_audit.judge(_reports(prod=("svelte",)), [], TODAY)
    assert verdict == "FINDINGS"
    assert "GHSA-aaaa-bbbb-cccc (svelte)" in detail


def test_br_231_dated_exemption_covers_it_until_its_day() -> None:
    exemption = Exemption("GHSA-aaaa-bbbb-cccc", date(2026, 11, 30), "fix released next week")
    assert qa_web_audit.judge(_reports(prod=("svelte",)), [exemption], TODAY)[0] == "OK"
    late = date(2026, 12, 1)
    verdict, detail = qa_web_audit.judge(_reports(prod=("svelte",)), [exemption], late)
    assert verdict == "FINDINGS"
    assert "expired" in detail


def test_br_231_build_only_finding_is_reported_not_blocking() -> None:
    verdict, detail = qa_web_audit.judge(_reports(build=("vite",)), [], TODAY)
    assert verdict == "OK"
    assert "build-only, not blocking: GHSA-aaaa-bbbb-cccc (vite)" in detail


def test_br_231_unreachable_registry_is_error() -> None:
    assert qa_web_audit.judge(None, [], TODAY)[0] == "ERROR"
    broken = {"production": {"error": {"code": "ENOTFOUND"}}, "all": {}}
    assert qa_web_audit.judge(broken, [], TODAY)[0] == "ERROR"


def test_br_231_evaluate_runs_the_web_script(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(_reports()), "")

    verdict, _ = qa_web_audit.evaluate("26.13.0", "node@x", tmp_path / "none.txt", TODAY, runner)
    assert verdict == "OK"
    assert seen == [["sh", "scripts/web.sh", "26.13.0", "node@x", "audit"]]


def test_br_231_line_is_the_qa_form(capsys: Any) -> None:
    assert qa.report("web-audit", "info", "FINDINGS", "x") == 0  # info never fails alone
    out = capsys.readouterr().out.strip()
    assert out == "QA web-audit info FINDINGS — x"


def test_br_231_declared_info_with_its_target() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "web-audit:info" in makefile  # BR-100: info until its first green line on main
    assert "\nqa-web-audit:\n" in makefile
    assert (ROOT / "qa" / "web-audit-exemptions.txt").is_file()
