"""Normalised QA line of the analysis tools (F-007, BR-098, BR-099).

Each `qa-<name>` target of the Makefile prints exactly one line
`QA <name> <role> <verdict> — <detail>`; this module composes it and gives the exit code of a tool
run alone. The role is declared only in `QA_TOOLS` (Makefile): the scripts receive it as an
argument, they do not copy it.
"""

from __future__ import annotations

ROLES = ("blocking", "info")
VERDICTS = ("OK", "FINDINGS", "SKIP", "ERROR")


def effective_verdict(role: str, verdict: str) -> str:
    """A blocking tool does not return SKIP: an impossible check is an ERROR."""
    if role == "blocking" and verdict == "SKIP":
        return "ERROR"
    return verdict


def line(name: str, role: str, verdict: str, detail: str) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r}")
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict: {verdict!r}")
    one_line = " ".join(detail.split())
    return f"QA {name} {role} {verdict} — {one_line}"


def exit_code(role: str, verdict: str) -> int:
    """Tool run alone: non-zero iff FINDINGS/ERROR when blocking, or ERROR when informative."""
    if role == "blocking":
        return 1 if verdict in ("FINDINGS", "ERROR") else 0
    return 1 if verdict == "ERROR" else 0


def report(name: str, role: str, verdict: str, detail: str) -> int:
    """Print the QA line (on standard output) and return the exit code."""
    verdict = effective_verdict(role, verdict)
    print(line(name, role, verdict, detail), flush=True)
    return exit_code(role, verdict)


def parse_line(text: str) -> tuple[str, str, str, str] | None:
    """(name, role, verdict, detail) of a QA line, or None if it does not conform."""
    parts = text.split(" ", 4)
    if len(parts) != 5 or parts[0] != "QA" or parts[4][:2] != "— ":
        return None
    _, name, role, verdict, rest = parts
    if role not in ROLES or verdict not in VERDICTS:
        return None
    return name, role, verdict, rest[2:]
