"""qa-sonar — SonarQube analysis of `main` (F-007, BR-104, BR-105).

Guard of `main`: the analysis is sent only on an explicit call (`make sonar`, never under
`make qa`), from the `main` branch, clean tree. The configured token goes only to
the scanner, through the `SONAR_TOKEN` environment variable: neither argument, nor output, nor log.
Usage: qa_sonar.py <role> <scanner image> [--aggregate].
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import qa_tree

import qa

NAME = "sonar"
COVERAGE_PATH = "coverage.xml"
SHA_NAME = "coverage.sha"  # written by the gate, on a clean tree only (qa_tree.py)
# What the scanner reads, and nothing else: never the whole repository (`.cairn/auth/` is in it).
MOUNTED = ("src", "tests", "sonar-project.properties")
RESULT_URL = re.compile(r"you can find the results at:\s*(\S+)")
UNAUTHORIZED = ("401", "not authorized", "unauthorized", "invalid token", "insufficient privileges")
UNREACHABLE = (
    "unknown host", "unknownhostexception", "connection refused", "connect timed out",
    "timed out", "no route to host", "failed to query server version", "connection reset",
    "network is unreachable", "temporary failure in name resolution",
)  # fmt: skip


_git = qa_tree.git


def guard() -> str | None:
    """Reason for a SKIP if the repository state forbids sending, otherwise None."""
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            return f"branch {branch!r}, the analysis is valid only on main"
        return qa_tree.dirty_reason()
    except (RuntimeError, OSError):
        return "repository state unreadable (git unavailable)"


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def stale_coverage(path: Path) -> str | None:
    """Reason for a SKIP if coverage.xml is not the head's, otherwise None (BR-104).

    `coverage.xml` is ignored by git: a file left over from another branch would otherwise go out
    with the analysis of `main`. The gate writes `coverage.sha` beside it: the SHA of the head it
    tested, on a clean tree only (`qa_tree.py`; on a dirty tree the file stays absent). The analysis
    is sent only if this SHA is the current head's.
    """
    if not path.is_file():
        return f"{path.name} absent: run make gate (the tests step writes it)"
    try:
        tested = path.with_name(SHA_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return f"{SHA_NAME} absent: {path.name} is tied to no commit, run make gate again"
    try:
        head = _git("rev-parse", "HEAD")
    except (RuntimeError, OSError):
        return "head unreadable (git unavailable)"
    if tested != head:
        return f"{path.name} tested on a commit other than the head: run make gate again"
    return None


def run_scanner(image: str, token: str, coverage: Path) -> tuple[str, str]:
    """Run the scanner; (verdict, detail). The token passes only through the environment."""
    cwd = Path.cwd()
    mounts: list[str] = []
    for item in MOUNTED:
        mounts += ["-v", f"{cwd / item}:/usr/src/{item}:ro"]
    mounts += ["-v", f"{coverage.resolve()}:/usr/src/{COVERAGE_PATH}:ro"]
    cmd = [
        "docker", "run", "--rm",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "SONAR_TOKEN", "-e", "SONAR_HOST_URL", "-e", "SONAR_USER_HOME=/tmp/.sonar",
        *mounts,
        image,
    ]  # fmt: skip
    env = {**os.environ, "SONAR_TOKEN": token}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    except OSError:
        return "ERROR", "docker unavailable: the scanner cannot be started"
    output = _scrub(proc.stdout + "\n" + proc.stderr, token)
    if proc.returncode == 0:
        found = RESULT_URL.search(output)
        where = found.group(1) if found else "dashboard of the hullgate project"
        return "OK", f"analysis of main sent: {where}"
    low = output.lower()
    if any(mark in low for mark in UNAUTHORIZED):
        return "ERROR", "token refused (401): renew SONAR_TOKEN or the token in SONAR_TOKEN_FILE"
    if any(mark in low for mark in UNREACHABLE):
        return "SKIP", "SonarQube server unreachable from this session"
    return "ERROR", f"the scanner failed (code {proc.returncode})"


def evaluate(image: str, aggregate: bool) -> tuple[str, str]:
    if aggregate:
        return "SKIP", "analysis on demand: run make sonar"
    if not os.environ.get("SONAR_HOST_URL", "").strip():
        return "SKIP", "SONAR_HOST_URL unset"
    token = os.environ.get("SONAR_TOKEN", "").strip()
    token_path = os.environ.get("SONAR_TOKEN_FILE", "")
    if not token and not token_path:
        return "SKIP", "SONAR_TOKEN and SONAR_TOKEN_FILE: no token configured"
    reason = guard()
    if reason:
        return "SKIP", reason
    if not token and token_path:
        try:
            token = Path(token_path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return "SKIP", "SONAR_TOKEN_FILE unreadable"
    if not token:
        return "SKIP", "SONAR_TOKEN and SONAR_TOKEN_FILE: no token configured"
    coverage = Path(os.environ.get("QA_SONAR_COVERAGE_FILE", COVERAGE_PATH))
    reason = stale_coverage(coverage)
    if reason:
        return "SKIP", reason
    verdict, detail = run_scanner(image, token, coverage)
    return verdict, _scrub(detail, token)


def main(argv: list[str]) -> int:
    role, image = argv[1], argv[2]
    verdict, detail = evaluate(image, "--aggregate" in argv[3:])
    return qa.report(NAME, role, verdict, detail)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
