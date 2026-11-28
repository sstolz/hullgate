"""Cleanliness of the working tree in BR-104's sense, and writing of `coverage.sha` (#45).

A single definition of "clean tree": no tracked modification, no untracked file under
`src/` and `tests/`. `qa_sonar.py` uses it as its send guard; the gate uses it to write
`coverage.sha` (the SHA of the head it tested) only if the tested state is the head's.
Usage: qa_tree.py <coverage.sha file>. Never returns a failure: without a write, the file stays
absent and `qa-sonar` returns SKIP.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DIRTY_REASON = "dirty working tree (tracked modification or untracked file under src/ or tests/)"


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("git failed")
    return proc.stdout.strip()


def dirty_reason() -> str | None:
    """Reason why the tree is not clean in BR-104's sense, otherwise None.

    Raises RuntimeError or OSError if git is unreadable: the caller decides.
    """
    dirty = git("status", "--porcelain", "--untracked-files=no")
    untracked = git("ls-files", "--others", "--exclude-standard", "--", "src", "tests")
    return DIRTY_REASON if dirty or untracked else None


def stamp(target: Path) -> str:
    """Write the head's SHA to `target` if the tree is clean; return the printed message."""
    target.unlink(missing_ok=True)
    try:
        reason = dirty_reason()
        if reason:
            return f"{target.name} not written: {reason}"
        target.write_text(git("rev-parse", "HEAD") + "\n", encoding="utf-8")
    except (RuntimeError, OSError):
        return f"{target.name} not written: repository state unreadable (git unavailable)"
    return f"{target.name} written: head tested on a clean tree"


def main(argv: list[str]) -> int:
    print(stamp(Path(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
