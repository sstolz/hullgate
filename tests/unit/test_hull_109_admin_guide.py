"""F-023 AC-07..10: the operator replays the guide, not a closed spec."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/guides/admin.md"


def blocks(text: str) -> list[str]:
    return re.findall(r"^\s*```sh\n(.*?)^\s*```", text, re.M | re.S)


def step(text: str, name: str) -> str:
    return re.split(rf"^## Step {name} — .*\n", text, flags=re.M)[1].split("\n## Step ", 1)[0]


def unquoted_expansions(text: str) -> set[str]:
    """Read shell quoting without executing any operator command or expanding secrets."""
    found: set[str] = set()
    quote = ""
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if char == "#" and not quote and (i == 0 or text[i - 1].isspace()):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end + 1
            continue
        if char in "\"'":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif char == "$" and not quote:
            match = re.match(r"\$\{?([A-Za-z_]\w*)", text[i:])
            if match:
                found.add(match[1])
        i += 1
    return found


def test_hull_109_no_unquoted_multiword_variables() -> None:
    text = GUIDE.read_text()
    shell = "\n".join(blocks(text))
    multiword = {
        name
        for name, rhs in re.findall(r"^\s*(?:export )?([A-Za-z_]\w*)=(.+)$", shell, re.M)
        if re.search(r"\s", rhs)
    }
    assert not (multiword & unquoted_expansions(shell))
    assert not re.search(r"^\s*[FC]=", shell, re.M)


@pytest.mark.parametrize("expansion", ["$CMD", "${CMD}"])
def test_multiword_guard_sees_unquoted_expansions(expansion: str) -> None:
    assert unquoted_expansions(f'CMD="two words"\n{expansion} arg') == {"CMD"}
    assert not unquoted_expansions(f'printf "%s" "{expansion}" # $CMD')


def test_hull_109_claude_code_and_unplace() -> None:
    text = GUIDE.read_text()
    b4, c2, c3 = (step(text, name) for name in ("B4", "C2", "C3"))
    assert "Remove placement" not in text
    for expected in (
        "Unplace",
        "Claude Code",
        "~/hullgate-acop02",
        "read -rs HG_TOKEN",
        "claude mcp add --transport http --scope local",
        "unset HG_TOKEN",
        '"Authorization: Bearer $HG_TOKEN"',
        "Reconnect",
        "claude mcp remove hullgate-acop02 --scope local",
        "revoke the token",
    ):
        assert expected in b4
    assert "Claude Code" in c2
    assert "B4" in c2
    assert "claude mcp add" not in c2
    assert "Reconnect" in c3
    assert "hullgate-acop02" in c3
    assert not any("claude mcp get" in block for block in blocks(text))


def test_hull_109_d2_replays_ac_op_03_commands_in_order() -> None:
    source = (ROOT / "features/F-016-admin-console.md").read_text()
    protocol = source.split("**AC-OP-03**", 1)[1].split("**AC-OP-05**", 1)[0]
    reference = blocks(protocol)
    assert reference  # No empty extraction may pass this guard.
    expected = [
        line.strip().replace("$P", "docker compose -p hullgate-acop03")
        for block in reference
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("P=")
    ]
    d2 = step(GUIDE.read_text(), "D2")
    actual = [line.strip() for block in blocks(d2) for line in block.splitlines()]
    position = 0
    for command in expected:
        position = actual.index(command, position) + 1
    assert "compose.prod.yaml" not in d2
    assert "docker exec hullgate-acop03 hullgate settings show /" in d2
    assert "Place in…" in d2
    assert "Connect…" in d2


def test_hull_109_header_shells_and_install_directory() -> None:
    header = GUIDE.read_text().split("## Step P1", 1)[0]
    for expected in ("bash", "zsh", "<install-dir>", "operator's choice", "/opt/hullgate"):
        assert expected in header
