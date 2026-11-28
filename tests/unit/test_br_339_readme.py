"""BR-339: the README's reading path for a reader who has never seen hullgate.

Its `##` headings are exactly the ten of F-025 §3.3, in order; `## Historical references`
carries BR-332's note (F-025 §3.8 step 6); the page links the licence, the contribution rules,
the security policy and the five generic guides; every relative link resolves to a tracked file.
That the page reads for an outsider is read by review.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEADINGS = [
    "## What hullgate is",
    "## Status",
    "## Quick start",
    "## Deployment",
    "## Configuration",
    "## Development",
    "## Contributing",
    "## Security",
    "## License",
    "## Historical references",
]
REQUIRED_LINKS = {
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    *(f"docs/guides/{g}.md" for g in ("install", "update", "backup-restore", "admin")),
    "docs/guides/keys-and-upstreams.md",
}
# BR-332's note as F-025 §3.8 step 6 rewrites it: one fragment per clause.
NOTE = [
    re.compile(r"`HULL-NN` names a ticket or pull request of the project's private tracker"),
    re.compile(r"`#N` names a GitHub issue or pull request of\s+this repository"),
    re.compile(r"except in the tree of tag `v1\.0\.0`"),
    re.compile(r"`GH-N`, in a branch name or in a field cairn composes"),
    re.compile(r"names GitHub issue #N"),
    re.compile(r"Review identifiers"),
    re.compile(r"workshop reports"),
]
LINK = re.compile(r"\]\(([^)\s]+)\)")


def tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return {p for p in out.stdout.decode().split("\0") if p}


def problems(text: str, files: set[str]) -> list[str]:
    errors = []
    if re.findall(r"^## .*$", text, re.MULTILINE) != HEADINGS:
        errors.append("## headings differ from the ten of F-025 §3.3, in order")
    note = text.partition("## Historical references")[2]
    errors.extend(f"note clause missing: {p.pattern}" for p in NOTE if not p.search(note))
    relative = set()
    for target in LINK.findall(text):
        if re.match(r"[a-z]+:", target) or target.startswith("#"):
            continue
        path = target.split("#", 1)[0]
        relative.add(path)
        if path not in files:
            errors.append(f"relative link to an untracked path: {target}")
    errors.extend(f"link missing: {p}" for p in sorted(REQUIRED_LINKS - relative))
    return errors


def test_BR_339_001_readme() -> None:
    files = tracked()
    assert problems((ROOT / "README.md").read_text(encoding="utf-8"), files) == []


def test_BR_339_001_negative_controls() -> None:
    files = tracked()
    good = (ROOT / "README.md").read_text(encoding="utf-8")
    assert problems(good.replace("## Status", "## Project status"), files)
    assert problems(good + "\n## Roadmap\n", files)
    assert problems(good + "\nSee [the plan](docs/plan.md).\n", files)
    assert problems(good.replace("(SECURITY.md)", "(https://example.org/security)"), files)
    assert problems(good.replace("except in the tree of tag `v1.0.0`", "always"), files)
