"""BR-103 — a runtime lock with a known, unexempted vulnerability fails the audit (AC-03)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pytest
import qa_audit

TODAY = date(2026, 10, 29)


def _report(*vulns: Mapping[str, object]) -> dict[str, object]:
    return {
        "dependencies": [
            {"name": "mcp", "version": "2.3.0", "vulns": list(vulns)},
            {"name": "idna", "version": "3.20", "vulns": []},
        ]
    }


VULN = {"id": "GHSA-aaaa-bbbb-cccc", "aliases": ["CVE-2026-0001"], "fix_versions": ["2.3.1"]}


def test_br_103_clean_lock_is_ok() -> None:
    verdict, detail = qa_audit.judge(_report(), [], TODAY)
    assert verdict == "OK"
    assert "2 audited packages" in detail


def test_br_103_unexempted_vulnerability_is_findings() -> None:
    verdict, detail = qa_audit.judge(_report(VULN), [], TODAY)
    assert verdict == "FINDINGS"
    assert "GHSA-aaaa-bbbb-cccc (mcp==2.3.0)" in detail


@pytest.mark.parametrize("ident", ["GHSA-aaaa-bbbb-cccc", "CVE-2026-0001"])
def test_br_103_running_exemption_by_id_or_alias_is_ok(ident: str) -> None:
    exemptions = qa_audit.parse_exemptions(f"{ident} 2026-10-29 no fix published\n")
    verdict, detail = qa_audit.judge(_report(VULN), exemptions, TODAY)
    assert verdict == "OK"
    assert "1 exemption(s) in force" in detail


def test_br_103_expired_exemption_is_findings_even_without_vulnerability() -> None:
    exemptions = qa_audit.parse_exemptions("GHSA-aaaa-bbbb-cccc 2026-10-28 reason\n")
    verdict, detail = qa_audit.judge(_report(), exemptions, TODAY)
    assert verdict == "FINDINGS"
    assert "expired" in detail


def test_br_103_expired_exemption_does_not_cover_the_vulnerability() -> None:
    exemptions = qa_audit.parse_exemptions("GHSA-aaaa-bbbb-cccc 2026-10-01 reason\n")
    verdict, detail = qa_audit.judge(_report(VULN), exemptions, TODAY)
    assert verdict == "FINDINGS"
    assert "without exemption" in detail
    assert "expired" in detail


@pytest.mark.parametrize(
    "text",
    ["GHSA-x\n", "GHSA-x 2026-10-29\n", "GHSA-x 29/10/2026 reason\n", "GHSA-x 2026-13-40 reason\n"],
)
def test_br_103_malformed_exemption_line_is_invalid(text: str) -> None:
    with pytest.raises(qa_audit.ExemptionError):
        qa_audit.parse_exemptions(text)


def test_br_103_comments_and_blank_lines_are_ignored() -> None:
    assert qa_audit.parse_exemptions("# note\n\n  \n") == []
    assert (Path(__file__).resolve().parents[2] / "qa/audit-exemptions.txt").is_file()


def _runner(report: object, export_ok: bool = True, stdout: str | None = None) -> qa_audit.Runner:
    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "uv":
            if export_ok:
                Path(cmd[cmd.index("-o") + 1]).write_text("mcp==2.3.0\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0 if export_ok else 2, "", "")
        return subprocess.CompletedProcess(
            cmd, 1, stdout if stdout is not None else json.dumps(report), ""
        )

    return run


def test_br_103_evaluate_on_fake_tool_output(tmp_path: Path) -> None:
    ex = tmp_path / "exemptions.txt"
    ex.write_text("# empty\n", encoding="utf-8")
    assert qa_audit.evaluate(ex, TODAY, _runner(_report()))[0] == "OK"
    assert qa_audit.evaluate(ex, TODAY, _runner(_report(VULN)))[0] == "FINDINGS"
    ex.write_text("CVE-2026-0001 2026-12-31 waiting for the upstream fix\n", encoding="utf-8")
    assert qa_audit.evaluate(ex, TODAY, _runner(_report(VULN)))[0] == "OK"
    ex.write_text("CVE-2026-0001 2026-01-31 waiting for the upstream fix\n", encoding="utf-8")
    assert qa_audit.evaluate(ex, TODAY, _runner(_report(VULN)))[0] == "FINDINGS"


def test_br_103_unreachable_database_or_failed_export_is_error(tmp_path: Path) -> None:
    ex = tmp_path / "exemptions.txt"
    assert qa_audit.evaluate(ex, TODAY, _runner(None, stdout="Connection error"))[0] == "ERROR"
    assert qa_audit.evaluate(ex, TODAY, _runner(_report(), export_ok=False))[0] == "ERROR"


def test_br_103_malformed_exemption_file_is_error(tmp_path: Path) -> None:
    ex = tmp_path / "exemptions.txt"
    ex.write_text("GHSA-x 2026-10-29\n", encoding="utf-8")
    verdict, detail = qa_audit.evaluate(ex, TODAY, _runner(_report()))
    assert verdict == "ERROR"
    assert "line 1" in detail


def test_br_103_skipped_package_is_not_counted_as_audited() -> None:
    report = {
        "dependencies": [
            {"name": "mcp", "version": "2.3.0", "vulns": []},
            {"name": "idna", "version": "3.20", "vulns": []},
            {"name": "local-thing", "skip_reason": "not on PyPI"},
        ]
    }
    verdict, detail = qa_audit.judge(report, [], TODAY)
    assert verdict == "OK"
    assert "2 audited packages" in detail  # 3 in the report, 1 skipped
    assert "1 skipped, not audited: local-thing" in detail
