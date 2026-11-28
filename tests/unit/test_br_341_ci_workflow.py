"""BR-341: every workflow plays the gate on every change, without a secret, on hosted runners.

Read on every file under `.github/workflows/`: triggers `push` and `pull_request`, never
`pull_request_target`; no `self-hosted` runner; `permissions` read-only on contents and nothing
else; no `secrets.` reference; every `uses:` pinned by a 40-hex commit SHA; steps `make gate`
then `make qa`. That the runs are green on GitHub is read on the forge (AC-OP-04).
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def workflows() -> list[Path]:
    return sorted((ROOT / ".github/workflows").glob("*.y*ml"))


def problems(text: str) -> list[str]:
    errors = []
    data: dict[Any, Any] = yaml.safe_load(text)
    # YAML 1.1 reads the bare key `on` as the boolean true.
    triggers = data.get("on", data.get(True))
    names = set(triggers) if isinstance(triggers, dict) else set()
    if not {"push", "pull_request"} <= names:
        errors.append("triggers lack push or pull_request")
    if "pull_request_target" in names or "pull_request_target" in text:
        errors.append("pull_request_target")
    if data.get("permissions") != {"contents": "read"}:
        errors.append("top-level permissions are not exactly contents: read")
    if "secrets." in text or re.search(r"\bsecrets\b\s*:", text):
        errors.append("a secret reference")
    jobs = data.get("jobs") or {}
    if not jobs:
        errors.append("no job")
    commands: list[str] = []
    for name, job in jobs.items():
        runner = job.get("runs-on")
        labels = runner if isinstance(runner, list) else [runner]
        if any(not isinstance(x, str) or "self-hosted" in x for x in labels):
            errors.append(f"{name}: runner is not GitHub-hosted")
        if "permissions" in job and job["permissions"] != {"contents": "read"}:
            errors.append(f"{name}: job permissions widened")
        if "environment" in job:
            errors.append(f"{name}: an environment")
        for step in job.get("steps", []):
            if "uses" in step and not PINNED.match(step["uses"]):
                errors.append(f"{name}: action not pinned by commit SHA: {step['uses']}")
            commands.extend(line.strip() for line in str(step.get("run", "")).splitlines())
    makes = [c for c in commands if c.startswith("make ")]
    if makes != ["make gate", "make qa"]:
        errors.append(f"make steps are {makes}, not make gate then make qa")
    return errors


def test_BR_341_001_workflows() -> None:
    files = workflows()
    assert files, "no workflow under .github/workflows/"
    for path in files:
        assert problems(path.read_text(encoding="utf-8")) == [], path.name


def test_BR_341_001_checkout_fetches_the_tag() -> None:
    # BR-350's guard reads the tree of tag v1.0.0: the checkout fetches tags and history.
    for path in workflows():
        text = path.read_text(encoding="utf-8")
        assert "fetch-depth: 0" in text
        assert "fetch-tags: true" in text


def test_BR_341_001_negative_controls() -> None:
    good = workflows()[0].read_text(encoding="utf-8")
    assert problems(good.replace("pull_request:", "pull_request_target:"))
    assert problems(good.replace("ubuntu-24.04", "[self-hosted, linux]"))
    assert problems(good.replace("contents: read", "contents: write"))
    injected = "        env:\n          T: ${{ secrets.TOKEN }}\n        run: make gate"
    assert problems(good.replace("        run: make gate", injected))
    assert problems(re.sub(r"@[0-9a-f]{40}", "@v6", good))
    assert problems(good.replace("run: make qa", "run: make lint"))
