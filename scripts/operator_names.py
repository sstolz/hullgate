"""BR-325: private infrastructure ratchet over the Git index's working-tree files.

Diagnostics contain only paths, line numbers and entry ids. Private patterns, samples and
exclusions live in DENYLIST; BASELINE holds counts and is removed when the sweep is complete.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

DENYLIST = ".cairn/runs/private/operator-denylist.json"
BASELINE = ".cairn/runs/private/operator-baseline.json"
CONTEXT = ".cairn/project-context.yaml"
GUARD = "scripts/operator_names.py"
WORKSHOP = (".cairn/runs/", ".cairn/learnings/", ".cairn/auth/")


@dataclass(frozen=True)
class Entry:
    index: str
    pattern: re.Pattern[bytes]
    exclusions: tuple[tuple[str, bytes, str], ...]


@dataclass
class Scan:
    counts: Counter[tuple[str, str]]
    diagnostics: list[str]
    binary: list[str]
    files: int


def git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


def tracked(root: Path) -> list[str]:
    try:
        result = git(root, "ls-files", "-z")
    except OSError:
        raise ValueError("BR-325: git ls-files failed") from None
    if result.returncode:
        raise ValueError("BR-325: git ls-files failed")
    paths = [p.decode() for p in result.stdout.split(b"\0") if p]
    if not paths:
        raise ValueError("BR-325: git ls-files resolved an empty population")
    return sorted(set(paths))


def outside_workshop(path: str) -> bool:
    return not path.startswith(WORKSHOP) and path != CONTEXT


def require(condition: object) -> None:
    if not condition:
        raise ValueError("invalid private input")


def entries(root: Path) -> list[Entry]:
    try:
        data = json.loads((root / DENYLIST).read_bytes())
        require(isinstance(data, list) and len(data) == 8)
        result = []
        for number, item in enumerate(data, 1):
            index = f"E{number}"
            require(item["index"] == index)
            require(item["case"] in ("sensitive", "insensitive"))
            require(item["bounds"] in ("none", "both", "login"))
            flags = re.IGNORECASE if item["case"] == "insensitive" else 0
            raw = item["pattern"]
            if item["bounds"] == "both":
                raw = r"(?<![A-Za-z0-9])(?:" + raw + r")(?![A-Za-z0-9])"
            elif item["bounds"] == "login":
                raw = r"(?<![A-Za-z0-9])(?:" + raw + r")(?=[A-Za-z0-9])"
            pattern = re.compile(raw.encode("ascii"), flags)
            require(pattern.search(item["sample"].encode("ascii")))
            exclusions = []
            for pin in item.get("exclusions", []):
                require(pin["reason"] and pin["literal"] and outside_workshop(pin["path"]))
                exclusions.append((pin["path"], pin["literal"].encode(), pin["reason"]))
            result.append(Entry(index, pattern, tuple(exclusions)))
        return result
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, OSError, re.error):
        raise ValueError("BR-325: invalid denylist schema, sample or pattern") from None


def scan(root: Path, paths: list[str], rules: list[Entry]) -> Scan:
    counts: Counter[tuple[str, str]] = Counter()
    diagnostics, binary = [], []
    population = [p for p in paths if outside_workshop(p)]
    for rule in rules:
        for path, literal, _reason in rule.exclusions:
            if path not in population or not (root / path).is_file():
                raise ValueError(f"{path}:0 {rule.index}: exclusion file absent")
            if literal not in (root / path).read_bytes():
                raise ValueError(f"{path}:0 {rule.index}: stale exclusion")
    for path in population:
        file = root / path
        # An unstaged deletion must be staged before acceptance, not silently skipped.
        if file.is_symlink():
            # Git tracks the link text, not the target; never follow it into local secrets.
            raw = os.fsencode(os.readlink(file))
        elif file.is_file():
            raw = file.read_bytes()
        else:
            raise ValueError(f"{path}:0: tracked file unreadable")
        try:
            raw.decode("utf-8")
            is_binary = b"\0" in raw
        except UnicodeError:
            is_binary = True
        if is_binary:
            binary.append(path)
        for rule in rules:
            path_count = len(rule.pattern.findall(path.encode()))
            if path_count:
                counts[path, rule.index] += path_count
                diagnostics.append(f"{path}:0 {rule.index}")
            content = raw
            for pinned_path, literal, _reason in rule.exclusions:
                if path == pinned_path:
                    # Keep byte/line offsets and token boundaries while masking a pinned literal.
                    content = content.replace(
                        literal, b"\n".join(b" " * len(x) for x in literal.split(b"\n"))
                    )
            for line_no, line in enumerate(content.split(b"\n"), 1):
                found = len(rule.pattern.findall(line))
                if found:
                    counts[path, rule.index] += found
                    diagnostics.append(f"{path}:{line_no} {rule.index}")
    return Scan(counts, diagnostics, binary, len(population))


def baseline(raw: bytes) -> Counter[tuple[str, str]]:
    try:
        rows = json.loads(raw)
        require(isinstance(rows, list))
        counts: Counter[tuple[str, str]] = Counter()
        for row in rows:
            path, index, count = row["file"], row["entry"], row["count"]
            require(isinstance(path, str) and outside_workshop(path))
            require(index in {f"E{i}" for i in range(1, 9)})
            require(type(count) is int and count > 0)
            require((path, index) not in counts)
            counts[path, index] = count
        return counts
    except (ValueError, TypeError, KeyError):
        raise ValueError("BR-325: invalid baseline") from None


def check(root: Path) -> tuple[str, list[str]]:
    if not (root / DENYLIST).is_file():
        if (root / CONTEXT).exists():
            raise ValueError("BR-325: private context present but denylist absent")
        return "skip", ["BR-325: denylist absent and no local context (public/export checkout)"]
    paths = tracked(root)
    if DENYLIST not in paths:
        raise ValueError("BR-325: denylist must be tracked for checkouts and linked worktrees")
    rules = entries(root)
    found = scan(root, paths, rules)
    current = baseline((root / BASELINE).read_bytes()) if (root / BASELINE).exists() else Counter()
    if current and BASELINE not in paths:
        raise ValueError("BR-325: baseline must be tracked")
    # Current tracked files are always compared with the private baseline itself below.
    # Also walk the baseline's committed history: HEAD alone cannot detect an allowance
    # enlarged in the same commit as the offending file. Once introduced, it only shrinks.
    history = git(root, "rev-list", "--first-parent", "--reverse", "HEAD", "--", BASELINE, GUARD)
    prior: Counter[tuple[str, str]] | None = None
    for revision in history.stdout.decode().splitlines():
        snapshot = git(root, "show", f"{revision}:{BASELINE}")
        allowance = baseline(snapshot.stdout) if snapshot.returncode == 0 else Counter()
        if prior is not None and allowance - prior:
            raise ValueError("BR-325: committed baseline grew; baseline only shrinks")
        prior = allowance
    if prior is not None and current - prior:
        raise ValueError("BR-325: baseline grew; strict guard cannot regain a baseline")
    if found.counts != current:
        bad = set((found.counts - current) | (current - found.counts))
        lines = [
            line
            for line in found.diagnostics
            if (line.rsplit(":", 1)[0], line.rsplit(" ", 1)[1]) in bad
        ]
        lines.extend(f"{p}:0 {e}: baseline stale; shrink it" for p, e in current - found.counts)
        raise ValueError("BR-325: matches differ from baseline\n" + "\n".join(lines))
    return "pass", [
        *(f"BR-325 binary: {path}" for path in found.binary),
        f"BR-325: files={found.files} entries={len(rules)} binary={len(found.binary)} "
        f"baseline_rows={len(current)} matches={sum(found.counts.values())}",
    ]


def main() -> int:
    try:
        _status, lines = check(Path.cwd())
    except (ValueError, OSError) as exc:
        print(str(exc))
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
