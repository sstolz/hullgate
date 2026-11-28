"""BR-350: no tracked text cites an issue or pull request of the private forge as `#N`.

A **ticket-shaped** `#N` is `#` followed by one to four decimal digits, the first not zero, not
glued to a word, a `#`, an `&` or a backslash before it, nor to a word character or a hyphen
after it. The exclusion patterns below set apart the shapes that are not ticket references
(markdown anchors, colours, line and step numbers); the pinned literals set apart exact strings
of one file, as `tests/unit/test_former_names.py` pins its own. Both are reviewed with the pass
of F-025 §3.8, which wrote every other private `#N` of the tree `HULL-N`.

The guard: a tracked line outside the workshop that carries a ticket-shaped `#N` outside the
exclusions, and that is also a line of the tree of tag `v1.0.0` — where every such `#N` named a
private ticket — fails; the tag absent fails, naming it. A `#N` written after the switch names a
GitHub issue and is not a line of that tree. This module spells no ticket-shaped `#N` itself:
fixtures build them from fragments. The publication check (`scripts/publication_check.py`,
`catch-up` mode) imports `outside_workshop` and `ticket_lines` from it.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAG = "v1.0.0"
WORKSHOP = (".cairn/runs/", ".cairn/learnings/", ".cairn/auth/")
CONTEXT = ".cairn/project-context.yaml"
HASH = "#"
TICKET_SHAPED = re.compile(r"(?<![\w#&\\])" + HASH + r"[1-9]\d{0,3}(?![\w-])")

# Exclusion patterns: (name, pattern, sample). Each span a pattern matches is masked before the
# ticket-shaped match; each sample must be matched by its pattern and carry a ticket-shaped token.
# The patterns stay narrow (review 1, N1): a colour only as the value of a CSS colour property,
# a line or step number only after `line`, `l.` or `step` — never a quoted, backticked or
# `: …;` token on its own, shapes a private citation can take.
PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("markdown anchor", re.compile(r"\]\(" + HASH + r"[^)\s]*\)"), "[x](" + HASH + "12)"),
    (
        "colour",
        re.compile(
            r"(?<![\w-])(?:color|background(?:-color)?|border(?:-[a-z]+)*|outline(?:-color)?"
            r"|fill|stroke|caret-color|accent-color)\s*:\s*"
            + HASH
            + r"\d{3,4}(?=\s*(?:[;}]|!important))"
        ),
        "color: " + HASH + "333;",
    ),
    (
        "line or step number",
        re.compile(r"(?i)\b(?:lines?|l\.|steps?)\s?" + HASH + r"\d+"),
        "see line " + HASH + "12",
    ),
]

# Pinned literals: (path, exact literal, reason). Masked in their file before the match; a
# literal no longer in its file fails `test_BR_350_002_pinned_exclusions_are_live`.
PINS: list[tuple[str, str, str]] = [
    (
        "tests/unit/test_br_331_no_ticket_reporting.py",
        '"paste it to ' + HASH + '8",',
        "BR-331's reporting-idiom fixture: the idiom it must detect, not a ticket",
    ),
    (
        "tests/unit/test_br_331_no_ticket_reporting.py",
        '"read the answer from ' + HASH + '7",',
        "BR-331's reporting-idiom fixture: the idiom it must detect, not a ticket",
    ),
    (
        "tests/unit/test_br_331_no_ticket_reporting.py",
        '"(template as ' + HASH + '3)",',
        "BR-331's reporting-idiom fixture: the idiom it must detect, not a ticket",
    ),
    (
        "tests/unit/test_br_331_no_ticket_reporting.py",
        '"Template as ' + HASH + '42",',
        "BR-331's reporting-idiom fixture: the idiom it must detect, not a ticket",
    ),
]


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def masked(path: str, text: str, pins: Iterable[tuple[str, str, str]] = ()) -> str:
    """`text` with pinned literals of `path` and exclusion-pattern spans blanked, same offsets."""
    for pin_path, literal, _reason in pins:
        if pin_path == path:
            text = text.replace(literal, re.sub(r"[^\n]", " ", literal))
    for _name, pattern, _sample in PATTERNS:
        text = pattern.sub(_blank, text)
    return text


def ticket_spans(
    path: str, text: str, pins: Iterable[tuple[str, str, str]] = PINS
) -> list[tuple[int, int]]:
    """Offsets `(start, end)` of the ticket-shaped `#N` of `text` outside the exclusions."""
    return [m.span() for m in TICKET_SHAPED.finditer(masked(path, text, pins))]


def ticket_lines(
    path: str, text: str, pins: Iterable[tuple[str, str, str]] = PINS
) -> list[tuple[int, str]]:
    """`(line number, line)` of each line of `text` carrying a ticket-shaped `#N` outside them."""
    shown = masked(path, text, pins).split("\n")
    return [
        (number, line)
        for number, (line, mask) in enumerate(zip(text.split("\n"), shown, strict=True), 1)
        if TICKET_SHAPED.search(mask)
    ]


def outside_workshop(path: str) -> bool:
    return not path.startswith(WORKSHOP) and path != CONTEXT


def findings(files: Iterable[tuple[str, str]], baseline: set[str]) -> list[str]:
    """`path:line` of each line carrying a ticket-shaped `#N` that is a line of the baseline."""
    return [
        f"{path}:{number}"
        for path, text in files
        if outside_workshop(path)
        for number, line in ticket_lines(path, text)
        if line in baseline
    ]


def stale_pins(
    pins: Iterable[tuple[str, str, str]], read: Callable[[str], str | None]
) -> list[str]:
    """Pins whose file is absent or no longer carries the literal."""
    stale = []
    for path, literal, _reason in pins:
        text = read(path)
        if text is None or literal not in text:
            stale.append(path)
    return stale


def _git(*args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=ROOT, input=data, capture_output=True, check=False)
    if result.returncode:
        raise LookupError(" ".join(args[:2]))
    return result.stdout


def tag_baseline(tag: str = TAG) -> set[str]:
    """Lines of the tag's tree, outside the workshop, that carry a ticket-shaped `#N`."""
    try:
        _git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}")
    except LookupError:
        raise AssertionError(f"tag {tag} absent: BR-350's guard reads its tree") from None
    listing = _git("ls-tree", "-r", "-z", f"refs/tags/{tag}^{{tree}}").split(b"\0")
    entries = [e.decode().split("\t", 1) for e in listing if e]
    blobs = [(meta.split()[2], path) for meta, path in entries if meta.split()[1] == "blob"]
    blobs = [(oid, path) for oid, path in blobs if outside_workshop(path)]
    out = _git("cat-file", "--batch", data="".join(f"{oid}\n" for oid, _ in blobs).encode())
    lines: set[str] = set()
    offset = 0
    for _oid, path in blobs:
        header_end = out.index(b"\n", offset)
        size = int(out[offset:header_end].split()[2])
        raw = out[header_end + 1 : header_end + 1 + size]
        offset = header_end + 1 + size + 1
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        lines.update(line for _, line in ticket_lines(path, text, pins=()))
    return lines


def tracked_files() -> list[tuple[str, str]]:
    paths = [p for p in _git("ls-files", "-z").decode().split("\0") if p]
    assert len(paths) > 500, "git ls-files resolves to too few files"
    files = []
    for path in paths:
        file = ROOT / path
        if not outside_workshop(path) or file.is_symlink() or not file.is_file():
            continue
        try:
            files.append((path, file.read_bytes().decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return files


def _read(path: str) -> str | None:
    file = ROOT / path
    return file.read_text(encoding="utf-8") if file.is_file() else None


def test_BR_350_001_no_private_ref_from_the_tag_comes_back() -> None:
    baseline = tag_baseline()
    assert baseline, f"tag {TAG}'s tree carries no ticket-shaped line: wrong baseline"
    assert findings(tracked_files(), baseline) == []


def test_BR_350_001_positive_fixture() -> None:
    # A line of the v1.0.0 tree carrying a private `#N`, restored in a scratch file, fails.
    baseline = tag_baseline()
    line = sorted(baseline)[0]
    assert findings([("docs/scratch.md", f"intro\n{line}\n")], baseline) == ["docs/scratch.md:2"]
    # The same token on a new line (a GitHub issue after the switch) passes.
    assert findings([("docs/scratch.md", "Fixed in " + HASH + "12.\n")], baseline) == []
    # A workshop file is outside the population.
    assert findings([(".cairn/runs/acts/x.md", f"{line}\n")], baseline) == []


def test_BR_350_001_tag_absent_fails_naming_it() -> None:
    import pytest

    with pytest.raises(AssertionError, match=r"v0\.0\.0-absent"):
        tag_baseline("v0.0.0-absent")


def test_BR_350_001_shapes() -> None:
    for sample in ("see " + HASH + "12", "(" + HASH + "7)", "**" + HASH + "109.**", HASH + "45 ·"):
        assert ticket_spans("x.md", sample, pins=()), sample
    for sample in (
        "PKCS" + HASH + "8",
        "&" + HASH + "123;",
        HASH + "0009",
        HASH + "19366c",
        HASH + "12345",
        "(" + HASH + "3-functional-spec)",
        "file.md" + HASH + "12",
        HASH + HASH + "12",
    ):
        assert not ticket_spans("x.md", sample, pins=()), sample
    for name, pattern, sample in PATTERNS:
        assert pattern.search(sample), name
        assert TICKET_SHAPED.search(sample), name
        assert not ticket_spans("x.md", sample, pins=()), name
    # A ticket reference after a colon stays ticket-shaped: the colour pattern needs a terminator.
    assert ticket_spans("x.md", "Ticket: " + HASH + "4 (x)", pins=())
    assert ticket_spans("x.md", "rotation: " + HASH + "23)", pins=())
    # Review 1, N1: quoted, backticked and `: …;` tokens are citations, not colours; `item`,
    # `row` and `no.` are not line or step words.
    for sample in (
        "Refs: " + HASH + "106;",
        '"' + HASH + '106"',
        "`" + HASH + "106`",
        "'" + HASH + "1061'",
        "see item " + HASH + "106",
        "row " + HASH + "106",
        "PR no. " + HASH + "106",
    ):
        assert ticket_spans("x.md", sample, pins=()), sample
    kept = ("border-top-color: " + HASH + "333 }", "fill:" + HASH + "000;", "step " + HASH + "4")
    for sample in kept:
        assert not ticket_spans("x.md", sample, pins=()), sample


def test_BR_350_002_pinned_exclusions_are_live() -> None:
    assert stale_pins(PINS, _read) == []
    for path, literal, _reason in PINS:
        assert TICKET_SHAPED.search(literal), path
        assert not ticket_spans(path, literal), path


def test_BR_350_002_stale_pin_fails() -> None:
    pin = ("README.md", "not in the readme " + HASH + "1", "fixture")
    assert stale_pins([pin], _read) == ["README.md"]
    assert stale_pins([("absent.md", "x", "fixture")], _read) == ["absent.md"]
