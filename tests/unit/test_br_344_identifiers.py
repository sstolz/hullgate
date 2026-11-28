"""BR-344: no identifier names two tickets.

`GH-N` is the identifier cairn composes for GitHub issue #N (`tracker.ticket-prefix: GH`, Q11),
and appears in the tracked tree only in a cairn-composed field (F-025 §3.6 step 7): a spec's
`ticket:` key, the `Ticket` column of `features/index.md`, a rule's `introduced-by`, a
derogation's `ref`, the first cell of a rule's Traceability row, a rule's Versioning reason
cell, and the `Branch` row of a spec's Metadata table, which `start-feature` writes from the
branch cairn composes (operator, 2026-11-24, after review 1). The workshop (`.cairn/runs/`,
`.cairn/learnings/`) is outside the tree read here, as for BR-325. Fixtures build the token from
fragments, so this file carries none itself.
"""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / ".cairn/project-context.example.yaml"
WORKSHOP = (".cairn/runs/", ".cairn/learnings/", ".cairn/auth/")
PREFIX = "G" + "H"
TOKEN = re.compile(r"(?<![A-Za-z0-9])" + PREFIX + r"-\d+(?![A-Za-z0-9])")

# Pinned exclusions: (path, exact literal, reason), stripped from their file before the match; a
# literal no longer in its file fails `test_BR_344_003_exclusions_are_live`. None today.
EXCLUSIONS: list[tuple[str, str, str]] = []


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _only_in(line: str, cell: str) -> bool:
    """Every token of the table row `line` lies in `cell`."""
    return len(TOKEN.findall(line)) == len(TOKEN.findall(cell)) > 0


def violations(path: str, text: str) -> list[int]:
    """Line numbers of `path` carrying the token outside a cairn-composed field."""
    lines = text.splitlines()
    front_end = 0
    if lines and lines[0] == "---":
        front_end = next((i for i, x in enumerate(lines[1:], 1) if x == "---"), 0)
    is_spec = re.fullmatch(r"features/F-\d{3}-[^/]+\.md", path) is not None
    is_rule = re.fullmatch(r"business-rules/BR-\d{3,}\.md", path) is not None
    ticket_col = None
    section = ""
    block = ""
    bad = []
    for number, line in enumerate(lines, 1):
        if line.startswith("## "):
            section = line[3:].strip()
        index = number - 1
        in_front = 0 < index < front_end
        if in_front and re.match(r"[A-Za-z-]+:", line):
            block = line.split(":", 1)[0]
        if path == "features/index.md" and line.startswith("|") and "Ticket" in _cells(line):
            ticket_col = _cells(line).index("Ticket")
        if not TOKEN.search(line):
            continue
        ok = False
        if in_front and is_spec:
            ok = re.fullmatch(r"ticket:\s*['\"]?" + TOKEN.pattern + r"['\"]?\s*", line) is not None
        if in_front and is_rule and not ok:
            ok = re.fullmatch(r"introduced-by:\s*" + TOKEN.pattern + r"\s*", line) is not None
        if in_front and block == "derogations" and not ok:
            ok = (
                re.fullmatch(r"\s*-?\s*ref:\s*['\"]?" + TOKEN.pattern + r"['\"]?\s*", line)
                is not None
            )
        if path == "features/index.md" and ticket_col is not None and line.startswith("|"):
            cells = _cells(line)
            ok = len(cells) > ticket_col and _only_in(line, cells[ticket_col])
        if is_spec and section == "Metadata" and line.startswith("|") and not in_front:
            cells = _cells(line)
            ok = len(cells) == 2 and cells[0] in ("**Branch**", "Branch")
            ok = ok and _only_in(line, cells[1])
        if is_rule and line.startswith("|") and not in_front:
            cells = _cells(line)
            if section == "Traceability":
                ok = _only_in(line, cells[0])
            elif section == "Versioning":
                ok = _only_in(line, cells[-1])
        if not ok:
            bad.append(number)
    return bad


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    files = [f for f in out.stdout.decode().split("\0") if f]
    assert len(files) > 500, "git ls-files resolves to too few files"
    return [f for f in files if not f.startswith(WORKSHOP) and f != ".cairn/project-context.yaml"]


def test_BR_344_002_example_prefix_is_gh() -> None:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert data["tracker"]["ticket-prefix"] == PREFIX


def test_BR_344_003_tracked_tree() -> None:
    excluded: dict[str, list[str]] = {}
    for path, literal, _ in EXCLUSIONS:
        excluded.setdefault(path, []).append(literal)
    hits: list[str] = []
    for rel in _tracked():
        file = ROOT / rel
        if not file.is_file() or file.is_symlink():
            continue
        text = file.read_bytes().decode("utf-8", errors="replace")
        for literal in excluded.get(rel, []):
            text = text.replace(literal, "")
        hits.extend(f"{rel}:{n}" for n in violations(rel, text))
    assert hits == []


def test_BR_344_003_exclusions_are_live() -> None:
    for path, literal, reason in EXCLUSIONS:
        assert literal in (ROOT / path).read_text(encoding="utf-8"), (path, literal, reason)


GH = PREFIX + "-12"
SPEC = f"---\nid: F-099\nticket: {GH}\nstatus: draft\n---\n\n# F-099 · x\n"
METADATA = (
    "\n## Metadata\n\n| Field | Value |\n|-------|-------|\n"
    f"| **Branch** | `feat/{GH}-x` |\n| **Last updated** | 2026-12-01 |\n\n## 1. Context\n"
)
INDEX = (
    "| ID | Title | Status | Domain | BRs | Spec | Ticket |\n"
    "|----|-------|--------|--------|-----|------|--------|\n"
    f"| F-099 | x | draft | core | 0 | ./features/F-099-x.md | {GH} |\n"
)
RULE = (
    f"---\nid: BR-999\nintroduced-by: {GH}\nderogations:\n  - guard: INV-007\n"
    f"    ref: {GH}\n---\n\n# BR-999 · x\n\n## Versioning\n\n"
    "| Version | Date | Change | Reason |\n|---|---|---|---|\n"
    f"| 1 | 2026-12-01 | Creation | {GH} |\n\n## Traceability\n\n"
    f"| Feature | Relationship |\n|---|---|\n| {GH} | Introduced this BR (v1) |\n"
)


def test_BR_344_003_passing_fixtures() -> None:
    assert violations("features/F-099-x.md", SPEC) == []
    assert violations("features/F-099-x.md", SPEC + METADATA) == []
    assert violations("features/index.md", INDEX) == []
    assert violations("business-rules/BR-999.md", RULE) == []


def test_BR_344_003_positive_fixtures() -> None:
    assert violations("docs/x.md", f"Fixed in {GH}.\n") == [1]
    assert violations("features/F-099-x.md", SPEC + f"\nSee {GH}.\n") == [9]
    assert violations("features/F-099-x.md", SPEC.replace("ticket:", "title:")) == [3]
    versioning_change_cell = RULE.replace("| Creation |", f"| {GH} |")
    assert violations("business-rules/BR-999.md", versioning_change_cell) == [15]
    trace_second_cell = RULE.replace("| Introduced this BR (v1) |", f"| Touched by {GH} |")
    assert violations("business-rules/BR-999.md", trace_second_cell) == [21]
    assert violations("features/index.md", INDEX.replace("| x |", f"| {GH} |")) == [3]
    # The Branch row admits the token in its value only, and only in a spec's Metadata table.
    other_row = METADATA.replace("| 2026-12-01 |", f"| {GH} |")
    assert violations("features/F-099-x.md", SPEC + other_row) == [14]
    assert violations("docs/x.md", METADATA) == [6]
    elsewhere = SPEC + METADATA.replace("## Metadata", "## 9. Implementation Notes")
    assert violations("features/F-099-x.md", elsewhere) == [13]
    assert not TOKEN.search(PREFIX + "-12a")
    assert not TOKEN.search("X" + PREFIX + "-12")
