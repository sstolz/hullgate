"""BR-338: `SECURITY.md` names one private reporting channel and no e-mail address."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEADINGS = ["## Supported versions", "## Reporting a vulnerability", "## What to expect"]
ADVISORY = "https://github.com/sstolz/hullgate/security/advisories/new"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|mailto:", re.IGNORECASE)
URL = re.compile(r"https?://[^\s)>\]]+")


def problems(text: str) -> list[str]:
    errors = []
    if re.findall(r"^## .*$", text, re.MULTILINE) != HEADINGS:
        errors.append("headings differ from the three of F-025 §3.3, in order")
    for literal in ("private vulnerability reporting", ADVISORY, "do not open a public issue"):
        if literal not in text:
            errors.append(f"missing literal: {literal}")
    if EMAIL.search(text):
        errors.append("an e-mail address")
    if set(URL.findall(text)) != {ADVISORY}:
        errors.append("a link other than the advisory form (a second channel)")
    return errors


def test_BR_338_001_security_policy() -> None:
    assert problems((ROOT / "SECURITY.md").read_text(encoding="utf-8")) == []


def test_BR_338_001_negative_controls() -> None:
    good = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert problems(good + "\nOr write to security" + "@" + "example.org.\n")
    assert problems(good + "\nOr use https://example.org/report.\n")
    assert problems(good.replace("do not open a public issue", "avoid public issues"))
    assert problems(good.replace("## What to expect", "## Afterwards"))
    assert problems(good.replace(ADVISORY, ADVISORY.replace("/new", "")))
