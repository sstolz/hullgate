"""Contract of the analysis tools (F-007, BR-098): read by the contract test, hence by the gate.

Depends on the standard library only.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import qa

TOOLS_RE = re.compile(r"^QA_TOOLS\s*:?=\s*(.*)$", re.MULTILINE)
IMAGE_RE = re.compile(r"^QA_IMAGE_(\w+)\s*:?=\s*(\S+)\s*$", re.MULTILINE)
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def declared_tools(makefile: str) -> list[tuple[str, str]]:
    """[(name, role)] of `QA_TOOLS`; the role is the one written, valid or not."""
    found = TOOLS_RE.search(makefile)
    if not found:
        return []
    tools: list[tuple[str, str]] = []
    for item in found.group(1).split():
        name, _, role = item.partition(":")
        tools.append((name, role))
    return tools


def static_problems(root: Path) -> list[str]:
    """Contract deviations readable in the files: target, role, pinning of the tools."""
    problems: list[str] = []
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    tools = declared_tools(makefile)
    if not tools:
        problems.append("QA_TOOLS absent or empty")
    for name, role in tools:
        if not TOOL_NAME_RE.match(name):
            problems.append(f"invalid tool name: {name!r}")
        if role not in qa.ROLES:
            problems.append(f"{name}: role {role!r} not in {qa.ROLES}")
        if not re.search(rf"^qa-{re.escape(name)}\s*:", makefile, re.MULTILINE):
            problems.append(f"{name}: no qa-{name} target in the Makefile")
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    for dep in pyproject.get("dependency-groups", {}).get("dev", []):
        if not re.match(r"^[A-Za-z0-9_.\-\[\]]+==[^=<>~!,\s]+$", dep):
            problems.append(f"dev dependency not pinned with ==: {dep!r}")
    for name, image in IMAGE_RE.findall(makefile):
        if not DIGEST_RE.search(image):
            problems.append(f"image of {name} without @sha256:<64 hex>: {image!r}")
    return problems


def line_problems(name: str, role: str, output: str) -> list[str]:
    """Deviations of a `qa-<name>` target's output: one QA line, conforming, of the right role."""
    lines = [ln for ln in output.splitlines() if ln.startswith("QA ")]
    if len(lines) != 1:
        return [f"{name}: {len(lines)} QA line(s) instead of one"]
    parsed = qa.parse_line(lines[0])
    if parsed is None:
        return [f"{name}: non-conforming QA line: {lines[0]!r}"]
    got_name, got_role, verdict, _ = parsed
    problems: list[str] = []
    if got_name != name:
        problems.append(f"{name}: the line carries the name {got_name!r}")
    if got_role != role:
        problems.append(f"{name}: the line carries the role {got_role!r}, QA_TOOLS says {role!r}")
    if role == "blocking" and verdict == "SKIP":
        problems.append(f"{name}: a blocking tool does not return SKIP (it is an ERROR)")
    return problems


def reported_verdict(output: str) -> str | None:
    """Verdict of the single QA line of an output, or None if it is not readable."""
    lines = [ln for ln in output.splitlines() if ln.startswith("QA ")]
    parsed = qa.parse_line(lines[0]) if len(lines) == 1 else None
    return parsed[2] if parsed else None


def target_problems(name: str, role: str, returncode: int, output: str) -> list[str]:
    """Deviations of one run of a `qa-<name>` target: version, QA line, and the verdict's exit code.

    The exit code of a target run alone follows §3.1 (`qa.exit_code`): zero or non-zero, never
    finer, because `make` returns 2 when a recipe fails.
    """
    problems = line_problems(name, role, output)
    prefix = f"== QA {name} : "
    if not any(ln.startswith(prefix) and ln[len(prefix) :].strip() for ln in output.splitlines()):
        problems.append(f"{name}: no non-empty version line `== QA {name} : <version>`")
    if problems:
        return problems
    verdict = reported_verdict(output)
    if verdict is None or (returncode != 0) != (qa.exit_code(role, verdict) != 0):
        problems.append(
            f"{name}: verdict {verdict} ({role}) and exit code {returncode} inconsistent"
        )
    return problems
