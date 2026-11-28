"""BR-331: followed text has no instruction to use the historical private tracker."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IDIOM = re.compile(
    r"\b(?:post|paste|report|read|warn)\b[^\n]*?\b(?:on|to|into|from|at)\s+"
    r"(?:HULL-\d+|#\d+|the ticket)\b|\btemplate\s+as\s+#\d+\b",
    re.I,
)


def test_br_331_no_ticket_reporting() -> None:
    paths = [ROOT / "README.md", ROOT / "Makefile"]
    for directory in ("docs/guides", "deploy", "src", "tests"):
        paths.extend(
            p
            for p in (ROOT / directory).rglob("*")
            if p.is_file() and p.suffix not in (".pyc", ".png")
        )
    hits = []
    for path in paths:
        if path == Path(__file__):
            continue  # This file holds the negative-control reporting idioms below.
        for number, line in enumerate(
            path.read_bytes().decode("utf-8", errors="replace").splitlines(), 1
        ):
            if IDIOM.search(line) or (
                "docs/guides" in path.as_posix()
                and (re.search(r"#\d+\b", line) or "**Additional check**" in line)
            ):
                hits.append(f"{path.relative_to(ROOT)}:{number}")
    assert len(paths) > 100
    assert hits == []


@pytest.mark.parametrize(
    "line",
    [
        "post the output on HULL-9",
        "paste it to #8",
        "report this on the ticket",
        "read the answer from #7",
        "warn the operator on the ticket",
        "(template as #3)",
        "Template as #42",
    ],
)
def test_br_331_reporting_idioms(line: str) -> None:
    assert IDIOM.search(line)


def test_br_331_historical_identifier_is_not_an_instruction() -> None:
    assert IDIOM.search("HULL-9 introduced this check") is None
