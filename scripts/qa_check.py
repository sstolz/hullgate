"""Check of one run of a `qa-<name>` target by the `make qa` aggregator (F-007, BR-098, BR-099).

Usage: qa_check.py <name> <role> <exit code of the target>, the target's output on standard
input. Exits 0 if the target conforms and its verdict is not a failure; exits 1 otherwise,
then printing the line `QA <name> <role> ERROR — …` that says why if the target does not
conform. The aggregator keeps this failure only for a `blocking` tool (BR-099).
"""

from __future__ import annotations

import sys

import qa_contract

import qa


def check(name: str, role: str, returncode: int, output: str) -> tuple[bool, str | None]:
    """(failure, ERROR line to add) for one run of a target."""
    problems = qa_contract.target_problems(name, role, returncode, output)
    if problems:
        return True, qa.line(name, role, "ERROR", "non-conforming target — " + "; ".join(problems))
    verdict = qa_contract.reported_verdict(output) or "ERROR"
    return qa.exit_code(role, verdict) != 0, None


def main(argv: list[str]) -> int:
    name, role, returncode = argv[1], argv[2], int(argv[3])
    if role not in qa.ROLES:
        print(qa.line(name, "info", "ERROR", f"role {role!r} not in {qa.ROLES}"))
        return 1
    failed, extra = check(name, role, returncode, sys.stdin.read())
    if extra:
        print(extra)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
