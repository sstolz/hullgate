"""HULL-58 — no tracked file or path carries a former name of the product or of its tickets.

Read from `git ls-files`: every tracked path, and the content of every tracked file. The former
product name is matched case-insensitively on its first four letters plus `son`, which also catches
its accented and inflected forms; the former ticket prefix is matched case-sensitively (a
case-insensitive prefix would match English words). Both patterns are built from fragments, so this
file spells neither; each has a positive fixture, so a mistyped pattern cannot pass by matching
nothing. Git history is not read.

Once this guard is in `make gate`, a record, a spec or a report that spells a former name turns the
gate red: write `the former name`, or build it from fragments as here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORMER_NAME = re.compile("cloi" + "son", re.IGNORECASE)
FORMER_PREFIX = re.compile("CL" + "OI")

# Pinned exclusions: (path, exact literal, reason). Each literal is stripped from its file before
# the match, so the rest of the file is still swept; a literal no longer in its file fails
# `test_former_names_exclusions_are_live`, so an entry cannot outlive its reason. None since the v1
# bench replay of HULL-58 rewrote the two harness ids of `evidence/results-v1.json`.
EXCLUSIONS: list[tuple[str, str, str]] = []


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    files = [f for f in out.split("\0") if f]
    assert len(files) > 500, "git ls-files resolves to too few files"
    return files


def _hits(text: str) -> list[str]:
    return [m.group(0) for p in (FORMER_NAME, FORMER_PREFIX) for m in p.finditer(text)]


def test_former_names_patterns_match_their_fixtures() -> None:
    name, prefix = "cloi" + "sonne", "CL" + "OI"
    for sample in (name, name.upper(), name.capitalize(), name[:-1] + "ée", name[:-1] + "ment"):
        assert FORMER_NAME.search(sample), sample
    for sample in (prefix + "-16", "feat/" + prefix + "-5-x", prefix):
        assert FORMER_PREFIX.search(sample), sample
    for sample in ("hullgate", "HULL-16", "Clois", "exclOIt", "cloi", "Cl" + "oi"):
        assert not _hits(sample), sample


def test_no_tracked_path_carries_a_former_name() -> None:
    hits = [f for f in _tracked() if _hits(f)]
    assert hits == []


def test_no_tracked_file_carries_a_former_name() -> None:
    excluded: dict[str, list[str]] = {}
    for path, literal, _ in EXCLUSIONS:
        excluded.setdefault(path, []).append(literal)
    hits: list[str] = []
    for rel in _tracked():
        file = ROOT / rel
        if not file.is_file():
            continue
        text = file.read_bytes().decode("utf-8", errors="replace")
        for literal in excluded.get(rel, []):
            text = text.replace(literal, "")
        for i, line in enumerate(text.splitlines(), 1):
            if _hits(line):
                hits.append(f"{rel}:{i}: {line.strip()[:120]}")
    assert hits == []


def test_former_names_exclusions_are_live() -> None:
    for path, literal, reason in EXCLUSIONS:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert literal in text, (path, literal, reason)
