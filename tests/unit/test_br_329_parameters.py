"""BR-329: every deployment key and documentation placeholder has one parameter row."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def missing_parameters(table: str, guides: str, env: str) -> set[str]:
    rows = re.findall(r"^\| `([^`]+)` \| [^|]+ \| ([^|]+) \|$", table, re.M)
    names = [name for name, default in rows if default.strip()]
    assert len(names) == len(set(names)), "duplicate parameter rows"
    used = set(re.findall(r"^([A-Z_]+)=", env, re.M))
    used.update(re.findall(r"<[A-Za-z][A-Za-z0-9_-]*>", guides))
    # A deployment key may also be written as a command placeholder.
    documented = set(names) | {f"<{n}>" for n in names if not n.startswith("<")}
    return used - documented


def test_br_329_parameters() -> None:
    table = (
        (ROOT / "docs/guides/install.md")
        .read_text()
        .split("## Parameters", 1)[1]
        .split("## Step O1", 1)[0]
    )
    guides = "\n".join(p.read_text() for p in (ROOT / "docs/guides").glob("*.md"))
    env = (ROOT / "deploy/env.example").read_text()
    assert missing_parameters(table, guides, env) == set()
    assert len(re.findall(r"^[A-Z_]+_PATH=/opt/hullgate/secrets/", env, re.M)) == 3


def test_br_329_new_undocumented_values_fail() -> None:
    assert missing_parameters("", "use <new-path>", "NEW_KEY=value") == {"<new-path>", "NEW_KEY"}
