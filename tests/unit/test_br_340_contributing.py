"""BR-340: `CONTRIBUTING.md` says how a change reaches `main`.

The test categories and their trigger paths are read against the public context example, so a
category added there must be named here. The accuracy of the workflow description against the
forge settings is read by review.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / ".cairn/project-context.example.yaml"
# Trigger paths of the categories outside `make gate`, as the example's comments state them.
TRIGGERS = {
    "container": (
        "make test-container",
        ["`Dockerfile`", "`compose*.yaml`", "`deploy/`", "`Makefile`"],
    ),
    "browser": ("make test-browser", ["`web/`", "`src/hullgate/admin/`"]),
}
# The same trigger paths as the example's comments spell them.
EXAMPLE_TRIGGERS = {
    "container": ["Dockerfile", "Compose files", "deploy/", "Makefile"],
    "browser": ["web/", "src/hullgate/admin/"],
}
REQUIRED = [
    "make gate",
    "make qa",
    "`qa-audit`",
    "`qa-secrets`",
    "pull request",
    "`main`",
    "comment review",
    "GitHub requires no approval",
    "[SECURITY.md](SECURITY.md)",
    "GitHub issue",
    "MIT licence",
    "inbound = outbound",
    "no contributor licence agreement",
    "no sign-off",
]
# The identifier note (F-025 §3.3, BR-340): `#N` with its `v1.0.0` exception, `HULL-NN`, `GH-N`.
NOTE = [
    re.compile(r"`#N` names a GitHub issue or pull request of this repository, except in the tree"),
    re.compile(r"of tag\s+`v1\.0\.0`"),
    re.compile(r"`HULL-NN` names a ticket or pull request of the project's private tracker"),
    re.compile(r"historical reference"),
    re.compile(r"`GH-N`, in a branch name or in a field cairn composes"),
    re.compile(r"names\s+GitHub issue #N"),
]


def categories() -> list[str]:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    return list(data["test-categories"])


def problems(text: str) -> list[str]:
    flat = " ".join(text.split())
    errors = [f"missing: {item!r}" for item in REQUIRED if item not in flat]
    for category in categories():
        if f"`{category}`" not in text:
            errors.append(f"test category not named: {category}")
        command, paths = TRIGGERS.get(category, ("make gate", []))
        if command not in text:
            errors.append(f"{category}: command {command} not named")
        section = next((p for p in text.split("\n- ") if command in p and "when" in p), "")
        errors.extend(f"{category}: trigger {p} not named" for p in paths if p not in section)
    errors.extend(f"identifier note: {p.pattern}" for p in NOTE if not p.search(flat))
    return errors


def test_BR_340_001_contributing() -> None:
    assert problems((ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")) == []


def test_BR_340_001_triggers_match_the_example() -> None:
    comments = " ".join(
        line.lstrip("# ")
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    )
    for category, (command, _paths) in TRIGGERS.items():
        heading = f"{category.capitalize()} tests use"
        start = comments.index(heading)
        end = comments.find(" tests use", start + len(heading))
        text = comments[start : end if end > 0 else None]
        assert command in text, category
        for path in EXAMPLE_TRIGGERS[category]:
            assert path in text, (category, path)
    assert set(TRIGGERS) <= set(categories())


def test_BR_340_001_negative_controls() -> None:
    good = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert problems(good.replace("`qa-secrets`", "the scanner"))
    assert problems(good.replace(", except in the tree", ". In the tree"))
    assert problems(good.replace("`GH-N`", "`GH-number`"))
    assert problems(good.replace("`src/hullgate/admin/`", "the admin code"))
    assert problems(good.replace("[SECURITY.md](SECURITY.md)", "the security policy"))
    assert problems(good.replace("comment review", "approval"))
