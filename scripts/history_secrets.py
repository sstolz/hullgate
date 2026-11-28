"""Prepare BR-327 evidence from an existing local mirror; never fetch or contact a host.

Usage: python scripts/history_secrets.py /path/to/mirror.git
Reports default to the workshop. This is run by the orchestrator, then the operator
fills the pending verdicts. Never overwrites an existing report or verdict file.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import qa_secrets
import secret_scanner


def scan_mirror(mirror: Path) -> tuple[int, int, list[tuple[str, secret_scanner.Finding]]]:
    git = qa_secrets.git
    if git(mirror, "rev-parse", "--is-bare-repository").strip() != b"true":
        raise ValueError("a bare mirror clone is required")
    if git(mirror, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("shallow mirrors are incomplete")
    if git(mirror, "config", "--get", "remote.origin.mirror").strip() != b"true":
        raise ValueError("remote.origin.mirror must be true")
    if (
        b"+refs/*:refs/*"
        not in git(mirror, "config", "--get-all", "remote.origin.fetch").splitlines()
    ):
        raise ValueError("the mirror must include all refs, including pull-request refs")
    refs = git(mirror, "for-each-ref", "--format=%(refname)").splitlines()
    if not any(ref.startswith(b"refs/pull/") for ref in refs):
        raise ValueError("no pull-request refs: forge mirror completeness is unproven")
    commits = git(mirror, "rev-list", "--all").decode().splitlines()
    if not commits:
        raise ValueError("empty mirror")
    cache: dict[bytes, list[secret_scanner.Finding]] = {}
    candidates: list[tuple[str, secret_scanner.Finding]] = []
    for commit in commits:
        for entry in git(mirror, "ls-tree", "-r", "-z", commit).split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            _, kind, oid = metadata.split()
            if kind != b"blob":
                raise ValueError("submodule history requires a separate mirror scan")
            if oid not in cache:
                cache[oid] = secret_scanner.scan("", git(mirror, "cat-file", "blob", oid.decode()))
            path = raw_path.decode("utf-8", errors="backslashreplace")
            candidates.extend(
                (commit, secret_scanner.Finding(path, f.line, f.detector, f.fingerprint))
                for f in cache[oid]
            )
    return len(refs), len(commits), sorted(candidates)


def reports(root: Path, mirror: Path) -> tuple[str, str]:
    qa_secrets.validate_pin(root)
    allowed = qa_secrets.allowlist(root)
    refs, commits, candidates = scan_mirror(mirror)
    fingerprints = sorted({f.fingerprint for _, f in candidates})
    stamp = datetime.now(UTC).date().isoformat()
    report = [
        "# HULL-98 history scan",
        "",
        f"Date: {stamp}; scanner: {secret_scanner.VERSION}; "
        f"runtime: native {platform.python_implementation()} {platform.python_version()}",
        f"Refs: {refs}; commits: {commits}; candidates: {len(candidates)}; "
        f"distinct fingerprints: {len(fingerprints)}",
        "Population: all commits reachable from all mirror refs, including refs/pull/.",
        "No fetch performed. The orchestrator must attest that the mirror matches the forge.",
        "",
        "commit · path (JSON) · line · detector · SHA-256/96 fingerprint · mask",
    ]
    report.extend(
        f"{commit} · {json.dumps(f.path)} · {f.line} · {f.detector} · "
        f"{f.fingerprint} · {secret_scanner.MASK}"
        for commit, f in candidates
    )
    reasons: dict[str, set[str]] = {}
    for (_, _, fingerprint), reason in allowed.items():
        reasons.setdefault(fingerprint, set()).add(reason)
    verdicts = [
        "# HULL-98 history verdicts",
        "",
        "Replace each PENDING with not a credential, not live, or rotated and its date.",
        "The operator must also reconcile the two candidates recorded on 2026-11-14.",
        "",
    ]
    for fingerprint in fingerprints:
        if fingerprint in reasons:
            reason = json.dumps("; ".join(sorted(reasons[fingerprint])))
            verdicts.append(f"{fingerprint} · not a credential · {stamp} · {reason}")
        else:
            verdicts.append(f"{fingerprint} · PENDING · YYYY-MM-DD")
    return "\n".join(report) + "\n", "\n".join(verdicts) + "\n"


def validate_verdicts(report: str, verdicts: str) -> bool:
    count = re.search(r"distinct fingerprints: (\d+)$", report, re.MULTILINE)
    if not report.startswith("# HULL-98 history scan\n") or count is None:
        return False
    expected = set(re.findall(r" · ([0-9a-f]{24}) · \[REDACTED\]$", report, re.MULTILINE))
    if len(expected) != int(count[1]):
        return False
    rows = [line for line in verdicts.splitlines() if re.match(r"^[0-9a-f]{24} · ", line)]
    seen = set()
    for line in rows:
        match = re.fullmatch(
            r"([0-9a-f]{24}) · (?:not a credential|not live|rotated) · "
            r"(\d{4}-\d{2}-\d{2})(?: · .+)?",
            line,
        )
        if match is None or match[1] in seen:
            return False
        try:
            datetime.strptime(match[2], "%Y-%m-%d")
        except ValueError:
            return False
        seen.add(match[1])
    return expected == seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mirror", type=Path)
    parser.add_argument("--check-verdicts", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    report_path = root / ".cairn/runs/acts/HULL-98-history-scan.md"
    verdict_path = root / ".cairn/runs/acts/HULL-98-history-verdicts.md"
    try:
        if args.check_verdicts:
            valid = validate_verdicts(report_path.read_text(), verdict_path.read_text())
            print("History verdicts: " + ("complete" if valid else "incomplete"))
            return 0 if valid else 1
        if report_path.exists() or verdict_path.exists():
            raise ValueError("reports already exist")
        report, verdicts = reports(root, args.mirror.resolve())
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("x") as handle:
            handle.write(report)
        with verdict_path.open("x") as handle:
            handle.write(verdicts)
    except (OSError, ValueError, KeyError, TypeError):
        print("History scan ERROR: mirror/configuration/output failure; details withheld")
        return 1
    print("Masked history report and verdict template written in the workshop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
