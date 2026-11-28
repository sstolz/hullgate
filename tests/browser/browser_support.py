"""Fixtures of the `browser` category (F-016, BR-035 v2): Chromium with a CDP virtual authenticator
against the real image (tests/browser/run.sh), sharing the gateway's network namespace so that the
console is `http://localhost:4791`, a secure context for WebAuthn.

Every page is watched: the URLs it requests (BR-212: no external origin) and its console messages
(BR-212: no CSP violation). Captures of the visual states (AC-21) land in `$CAPTURES`, named
`<state>@<theme>.png`.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import sync_playwright

CONSOLE = os.environ.get("CONSOLE", "http://localhost:4791")
GATEWAY = os.environ.get("GATEWAY", "")
CAPTURES = Path(os.environ.get("CAPTURES", "captures"))
PROVIDER = os.environ.get("PROVIDER", "https://gitea.example.test:8443")
AXE_JS = Path(os.environ.get("AXE_JS", "/opt/axe.min.js"))
LINK = re.compile(r"^ENROLMENT-LINK: (\S+)$", re.MULTILINE)
EMAIL = "ops@browser.test"


def host(*args: str, stdin: str | None = None) -> str:
    """`hullgate <args>` on the gateway container: the host side of BR-201. A secret goes on
    standard input (BR-086)."""
    command = ["docker", "exec", *(["-i"] if stdin is not None else []), GATEWAY, "hullgate"]
    result = subprocess.run(
        [*command, *args], input=stdin, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout


def bootstrap(email: str = EMAIL) -> str:
    links = LINK.findall(host("admin", "bootstrap", email))
    assert len(links) == 1
    return str(links[0])


def generate_key() -> None:
    """A disposable signing key inside the container's own volume (the vault becomes ready)."""
    subprocess.run(
        ["docker", "exec", GATEWAY, "python", "-m", "hullgate.keys", "signing-key",
         "/data/signing_key"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip


@dataclass
class Watched:
    context: Any
    page: Any
    requests: list[str] = field(default_factory=list)
    console: list[str] = field(default_factory=list)
    authenticator: str = ""
    cdp: Any = None

    def external(self) -> list[str]:
        """Requests to another origin than the console's. The fake provider's pages are the
        browser leaving for the provider and coming back (BR-216), not the console's requests."""
        return [
            u
            for u in self.requests
            if not u.startswith((CONSOLE + "/", PROVIDER + "/")) and u != CONSOLE
        ]

    def csp_violations(self) -> list[str]:
        return [m for m in self.console if "Content Security Policy" in m or "Refused to" in m]

    def capture(self, state: str) -> None:
        theme = self.page.evaluate("document.documentElement.dataset.theme") or "dark"
        CAPTURES.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(CAPTURES / f"{state}@{theme}.png"), full_page=True)

    def close(self) -> None:
        self.context.close()


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    with sync_playwright() as pw:
        chromium = pw.chromium.launch()
        yield chromium
        chromium.close()


def open_page(browser: Any, *, authenticator: bool = True, webauthn: bool = True) -> Watched:
    # the fake provider's certificate comes from the run's throwaway CA (tests/browser/run.sh)
    context = browser.new_context(viewport={"width": 1280, "height": 800}, ignore_https_errors=True)
    if not webauthn:
        context.add_init_script("delete window.PublicKeyCredential;")
    page = context.new_page()
    watched = Watched(context, page)
    page.on("request", lambda request: watched.requests.append(request.url))
    page.on("console", lambda message: watched.console.append(f"{message.type}: {message.text}"))
    if authenticator:
        watched.cdp = context.new_cdp_session(page)
        watched.cdp.send("WebAuthn.enable")
        new_authenticator(watched)
    return watched


def new_authenticator(watched: Watched) -> None:
    """Replaces the page's virtual authenticator by an empty one: another device of the operator
    (an authenticator holding an excluded credential refuses to create one more, as it should)."""
    if watched.authenticator:
        watched.cdp.send(
            "WebAuthn.removeVirtualAuthenticator", {"authenticatorId": watched.authenticator}
        )
    added = watched.cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "internal",
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )
    watched.authenticator = str(added["authenticatorId"])


@pytest.fixture(scope="session")
def operator(browser: Any, watches: list[Watched]) -> Watched:
    """The operator's browser: one context, one virtual authenticator, kept for the whole run.
    test_br_202 enrols it first (conftest orders the files); the later files reuse its session."""
    watched = open_page(browser)
    watches.append(watched)
    return watched


@pytest.fixture(scope="session")
def watches() -> Iterator[list[Watched]]:
    """Every page the session opened, for the BR-212 checks at the end."""
    opened: list[Watched] = []
    yield opened
    for w in opened:
        w.close()
