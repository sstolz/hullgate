"""qa-cov — line coverage rate of src/, read from coverage.xml (F-007, BR-102).

Usage: qa_cov.py <role> [path of coverage.xml]. No threshold: the rate is reported, never judged.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import qa

NAME = "cov"


def evaluate(role: str, path: Path) -> tuple[str, str]:
    """(verdict, detail) for the coverage.xml `path`."""
    if not path.is_file():
        return "SKIP", f"{path.name} absent: run make gate (the tests step writes it)"
    try:
        rate = float(ET.parse(path).getroot().attrib["line-rate"])  # noqa: S314
    except (ET.ParseError, KeyError, ValueError) as exc:
        return "ERROR", f"{path.name} unreadable ({type(exc).__name__})"
    stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%M UTC")
    pct = f"{rate * 100:.1f}"
    return "OK", f"line coverage of src/: {pct}% ({path.name} of {stamp})"


def main(argv: list[str]) -> int:
    role = argv[1]
    path = Path(argv[2] if len(argv) > 2 else "coverage.xml")
    verdict, detail = evaluate(role, path)
    return qa.report(NAME, role, verdict, detail)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
