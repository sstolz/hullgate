"""qa-web-audit — known vulnerabilities of the console's lockfile (F-016, BR-231).

Runs `npm audit` on `web/package-lock.json` through `scripts/web.sh` (local Node of the pinned
version, else the pinned image), once on the production dependencies (what the bundle ships) and
once on all of them. A production finding without a dated exemption of
`qa/web-audit-exemptions.txt` returns `FINDINGS`; build-only findings are reported in the detail,
never blocking. Same exemption form as `qa-audit` (BR-103).
Usage: qa_web_audit.py <role> <node-version> <node-image> [exemptions file].
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from qa_audit import Exemption, ExemptionError, parse_exemptions

import qa

NAME = "web-audit"
DEFAULT_EXEMPTIONS = Path("qa/web-audit-exemptions.txt")


def _advisories(report: Any) -> dict[str, set[str]]:
    """Package name → advisory identifiers (GHSA id from the URL, else npm's source number)."""
    out: dict[str, set[str]] = {}
    vulns = report.get("vulnerabilities", {}) if isinstance(report, dict) else {}
    for name, entry in vulns.items() if isinstance(vulns, dict) else ():
        for via in entry.get("via", []) if isinstance(entry, dict) else []:
            if not isinstance(via, dict):
                continue  # a string names the dependency the finding comes through
            url = str(via.get("url", ""))
            ident = url.rsplit("/", 1)[-1] if "/advisories/" in url else str(via.get("source"))
            out.setdefault(str(name), set()).add(ident)
    return out


def _count(report: Any, kind: str) -> int:
    meta = report.get("metadata", {}) if isinstance(report, dict) else {}
    deps = meta.get("dependencies", {}) if isinstance(meta, dict) else {}
    value = deps.get(kind, 0) if isinstance(deps, dict) else 0
    return int(value) if isinstance(value, int) else 0


def judge(reports: Any, exemptions: list[Exemption], today: date) -> tuple[str, str]:
    """Verdict and detail for `{"production": <npm audit>, "all": <npm audit>}`."""
    if not isinstance(reports, dict) or "production" not in reports or "all" not in reports:
        return "ERROR", "npm audit gave no report: replay with the registry reachable"
    for report in (reports["production"], reports["all"]):
        if isinstance(report, dict) and "error" in report:
            return "ERROR", f"npm audit failed: {report['error']}"
    active = {e.ident for e in exemptions if e.until >= today}
    expired = sorted(e.ident for e in exemptions if e.until < today)
    production = _advisories(reports["production"])
    everything = _advisories(reports["all"])
    open_findings = sorted(
        f"{ident} ({name})" for name, ids in production.items() for ident in ids - active
    )
    build_only = sorted(
        f"{ident} ({name})"
        for name, ids in everything.items()
        for ident in ids - production.get(name, set())
    )
    problems: list[str] = []
    if open_findings:
        problems.append("unexempted vulnerabilities in the bundle: " + ", ".join(open_findings))
    if expired:
        problems.append("expired exemptions: " + ", ".join(expired))
    build = f"; build-only, not blocking: {', '.join(build_only)}" if build_only else ""
    if problems:
        return "FINDINGS", "; ".join(problems) + build
    prod_count = _count(reports["production"], "prod")
    kept = f", {len(active)} exemption(s) in force" if active else ""
    return (
        "OK",
        f"no known vulnerability in {prod_count} production packages of web/package-lock.json"
        f"{kept}{build}",
    )


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def evaluate(
    version: str, image: str, exemptions_path: Path, today: date, runner: Runner = _run
) -> tuple[str, str]:
    try:
        text = exemptions_path.read_text(encoding="utf-8") if exemptions_path.is_file() else ""
        exemptions = parse_exemptions(text)
    except ExemptionError as exc:
        return "ERROR", f"{exemptions_path}: {exc}"
    proc = runner(["sh", "scripts/web.sh", version, image, "audit"])
    try:
        reports = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        reports = None
    return judge(reports, exemptions, today)


def main(argv: list[str]) -> int:
    role, version, image = argv[1], argv[2], argv[3]
    path = Path(argv[4]) if len(argv) > 4 else DEFAULT_EXEMPTIONS
    verdict, detail = evaluate(version, image, path, datetime.now(UTC).date())
    return qa.report(NAME, role, verdict, detail)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
