"""qa-audit — known vulnerabilities of the runtime lock of uv.lock (F-007, BR-103).

Exports the runtime lock (`uv export --locked --no-dev`), queries it with pip-audit (PyPI
database), applies the exemptions of `qa/audit-exemptions.txt` and returns a QA line.
Usage: qa_audit.py <role> [exemptions file].
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import qa

NAME = "audit"
DEFAULT_EXEMPTIONS = Path("qa/audit-exemptions.txt")


class ExemptionError(ValueError):
    """An exemption line is invalid (no date, no reason, malformed date)."""


@dataclass(frozen=True)
class Exemption:
    ident: str
    until: date
    reason: str


def parse_exemptions(text: str) -> list[Exemption]:
    found: list[Exemption] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        content = raw.strip()
        if not content or content.startswith("#"):
            continue
        parts = content.split(None, 2)
        if len(parts) < 3:
            raise ExemptionError(f'line {number}: expected "<identifier> <YYYY-MM-DD> <reason>"')
        ident, stamp, reason = parts
        try:
            until = date.fromisoformat(stamp)
        except ValueError:
            raise ExemptionError(f"line {number}: malformed date {stamp!r}") from None
        found.append(Exemption(ident, until, reason.strip()))
    return found


def _dependencies(report: Any) -> list[dict[str, Any]]:
    deps = report.get("dependencies", []) if isinstance(report, dict) else []
    return [d for d in deps if isinstance(d, dict)]


def judge(report: Any, exemptions: list[Exemption], today: date) -> tuple[str, str]:
    """Verdict and detail for a pip-audit JSON report."""
    active = {e.ident for e in exemptions if e.until >= today}
    expired = sorted(e.ident for e in exemptions if e.until < today)
    open_findings: list[str] = []
    for dep in _dependencies(report):
        for vuln in dep.get("vulns", []):
            ids = {vuln["id"], *vuln.get("aliases", [])}
            if not ids & active:
                open_findings.append(f"{vuln['id']} ({dep['name']}=={dep['version']})")
    problems: list[str] = []
    if open_findings:
        problems.append("vulnerability(ies) without exemption: " + ", ".join(sorted(open_findings)))
    if expired:
        problems.append("expired exemption(s): " + ", ".join(expired))
    if problems:
        return "FINDINGS", "; ".join(problems)
    kept = f", {len(active)} exemption(s) in force" if active else ""
    skipped = sorted(str(d.get("name")) for d in _dependencies(report) if d.get("skip_reason"))
    count = len(_dependencies(report)) - len(skipped)
    ignored = f", {len(skipped)} skipped, not audited: {', '.join(skipped)}" if skipped else ""
    return (
        "OK",
        f"no known vulnerability in {count} audited packages of the runtime lock{ignored}{kept}",
    )


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def evaluate(exemptions_path: Path, today: date, runner: Runner = _run) -> tuple[str, str]:
    try:
        text = exemptions_path.read_text(encoding="utf-8") if exemptions_path.is_file() else ""
        exemptions = parse_exemptions(text)
    except ExemptionError as exc:
        return "ERROR", f"{exemptions_path}: {exc}"
    with tempfile.TemporaryDirectory() as tmp:
        req = Path(tmp) / "requirements.txt"
        export = runner(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "-o",
                str(req),
                "-q",
            ]
        )
        if export.returncode != 0 or not req.is_file():
            return "ERROR", "export of the runtime lock impossible (uv export failed)"
        proc = runner(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "-r",
                str(req),
                "--no-deps",
                "--disable-pip",
                "-f",
                "json",
                "--progress-spinner",
                "off",
            ]
        )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return (
            "ERROR",
            "vulnerability database unreachable or pip-audit failed: run again with the network",
        )
    return judge(report, exemptions, today)


def main(argv: list[str]) -> int:
    role = argv[1]
    path = Path(argv[2]) if len(argv) > 2 else DEFAULT_EXEMPTIONS
    verdict, detail = evaluate(path, datetime.now(UTC).date())
    return qa.report(NAME, role, verdict, detail)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
