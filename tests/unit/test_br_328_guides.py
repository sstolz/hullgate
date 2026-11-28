"""BR-328: mapping rows are executable documentation checks; completeness is reviewed."""

import re
from pathlib import Path

import pytest

from tests.unit.test_br_042_runbooks import _steps

ROOT = Path(__file__).resolve().parents[2]
GUIDES = ("install", "update", "backup-restore", "admin", "keys-and-upstreams")


def problems(text: str) -> list[str]:
    rows = re.findall(r"^\| ([^|]+) \| ([A-Z]\d+) \| ([^|]+) \|$", text, re.MULTILINE)
    steps = _steps(text)
    errors = []
    if not rows:
        errors.append("no mapping rows")
    mapped = []
    for role, source, target in rows:
        if target.startswith("not carried — "):
            if not target.removeprefix("not carried — ").strip():
                errors.append(f"{role}:{source}: reason missing")
            continue
        mapped.append(target)
        body = steps.get(target, "")
        if not body or not re.search(r"^```\w*\n.+?\n```", body, re.M | re.S):
            errors.append(f"{target}: block missing")
        for marker in ("**Expected result**", "**If the result differs**"):
            if marker not in body:
                errors.append(f"{target}: {marker} missing")
    if len(mapped) != len(set(mapped)) or set(mapped) != set(steps):
        errors.append("mapping and step identifiers differ")
    return errors


@pytest.mark.parametrize("guide", GUIDES)
def test_br_328_mapping_and_steps(guide: str) -> None:
    text = (ROOT / "docs/guides" / f"{guide}.md").read_text()
    assert text.index("## Step mapping") < text.index("## Step ", text.index("## Step mapping") + 1)
    assert problems(text) == []


@pytest.mark.parametrize(
    "remove",
    ["```sh\ntrue\n```", "**Expected result**", "**If the result differs**", "## Step X1 — check"],
)
def test_br_328_missing_part_fails(remove: str) -> None:
    text = (
        "| trial | X1 | X1 |\n\n## Step X1 — check\n\n```sh\ntrue\n```\n"
        "**Expected result**: OK\n**If the result differs**: stop\n"
    )
    assert problems(text) == []
    assert problems(text.replace(remove, ""))
