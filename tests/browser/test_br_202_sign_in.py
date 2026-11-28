"""BR-202, BR-203, BR-204 (AC-02, AC-03, AC-21 for lot A's screens) — enrolment and sign-in in
Chromium with a virtual authenticator, against the real image. The tests run in file order: one
administrator is bootstrapped, enrols, signs out and in again, and adds a second passkey."""

from __future__ import annotations

import subprocess
from typing import Any

from browser_support import (
    CONSOLE,
    EMAIL,
    GATEWAY,
    Watched,
    bootstrap,
    generate_key,
    host,
    new_authenticator,
    open_page,
)
from console_support import accessibility, both_themes

STATE: dict[str, Any] = {}


def capture(w: Watched, state: str) -> None:
    both_themes(w, state)
    accessibility(w, state)


def test_br_202_fresh_instance_shows_sign_in(browser: Any, watches: list[Watched]) -> None:
    w = open_page(browser, authenticator=False)
    watches.append(w)
    w.page.goto(f"{CONSOLE}/admin")
    w.page.get_by_role("button", name="Sign in with a passkey").wait_for()
    assert "@" not in w.page.content()  # no account revealed
    capture(w, "SCR-01")


def test_br_202_browser_without_webauthn(browser: Any, watches: list[Watched]) -> None:
    w = open_page(browser, authenticator=False, webauthn=False)
    watches.append(w)
    w.page.goto(f"{CONSOLE}/admin")
    w.page.locator("[data-state='SCR-01.unsupported']").wait_for()
    capture(w, "SCR-01.unsupported")
    w.page.goto(f"{CONSOLE}/admin/enrol/whatever")
    w.page.locator("[data-state='SCR-02.unsupported']").wait_for()
    capture(w, "SCR-02.unsupported")


def test_br_203_enrol_from_the_link(operator: Watched) -> None:
    link = bootstrap()
    STATE["link"] = link
    page = operator.page
    page.goto(link)
    page.get_by_label("Passkey label").wait_for()
    assert "/enrol/" not in page.url.removeprefix(f"{CONSOLE}/admin")[len("/enrol") :]
    assert page.url == f"{CONSOLE}/admin/enrol"  # the token left the address bar
    assert EMAIL in page.content()
    capture(operator, "SCR-02")
    page.get_by_label("Passkey label").fill("chromium")
    page.get_by_role("button", name="Create a passkey").click()
    page.get_by_role("button", name=EMAIL).wait_for()
    assert page.url == f"{CONSOLE}/admin/tree"


def test_br_204_enrolment_session_expiry_returns_to_sign_in(operator: Watched) -> None:
    """SCR-03.session-expired in the session the enrolment page opened, before any reload
    (review 560, N1): a 401 takes the console back to SCR-01, then to the same screen."""
    page = operator.page
    operator.context.clear_cookies()
    page.get_by_role("button", name=EMAIL).click()
    page.get_by_role("menuitem", name="Passkeys").click()
    page.get_by_role("button", name="Sign in with a passkey").wait_for()
    page.get_by_role("button", name="Sign in with a passkey").click()
    page.get_by_role("button", name=EMAIL).wait_for()
    assert page.url == f"{CONSOLE}/admin/tree"


def test_br_204_shell_vault_down_then_ready(operator: Watched) -> None:
    page = operator.page
    page.locator("[data-state='SCR-03.vault-down']").wait_for()
    capture(operator, "SCR-03.vault-down")
    generate_key()
    page.reload()
    page.locator("[data-state='SCR-04.empty']").wait_for()
    page.wait_for_timeout(500)
    assert page.locator("[data-state='SCR-03.vault-down']").count() == 0
    capture(operator, "SCR-03")
    capture(operator, "SCR-04.empty")
    page.get_by_role("button", name="Light theme").click()
    operator.capture("SCR-03")
    operator.capture("SCR-04.empty")
    page.get_by_role("button", name="Dark theme").click()


def test_br_204_cookie_attributes(operator: Watched) -> None:
    cookies = [c for c in operator.context.cookies() if c["name"] == "hullgate_admin"]
    assert len(cookies) == 1
    cookie = cookies[0]
    assert cookie["httpOnly"] is True
    assert cookie["sameSite"] == "Strict"
    assert cookie["path"] == "/admin"
    assert cookie["secure"] is False  # loopback ADMIN_URL, the one exemption
    assert operator.page.evaluate("document.cookie") == ""  # HttpOnly: not readable by script


def test_br_202_sign_out_then_sign_in(operator: Watched) -> None:
    page = operator.page
    page.get_by_role("button", name=EMAIL).click()
    page.get_by_role("menuitem", name="Sign out").click()
    page.get_by_role("button", name="Sign in with a passkey").wait_for()
    assert not [c for c in operator.context.cookies() if c["name"] == "hullgate_admin"]
    page.get_by_role("button", name="Sign in with a passkey").click()
    page.get_by_role("button", name=EMAIL).wait_for()


def test_br_202_unknown_passkey_is_refused(browser: Any, watches: list[Watched]) -> None:
    w = open_page(browser)  # an authenticator holding no credential of this console
    watches.append(w)
    w.page.goto(f"{CONSOLE}/admin")
    w.page.get_by_role("button", name="Sign in with a passkey").click()
    w.page.locator("[data-state='SCR-01.refused']").wait_for()
    capture(w, "SCR-01.refused")


def test_br_210_second_passkey_with_fresh_confirmation(operator: Watched) -> None:
    """Adding asks for the passkey again (BR-210); the same authenticator then refuses to create
    a second credential for the account (excluded), so the operator uses another device."""
    page = operator.page
    page.get_by_role("button", name=EMAIL).click()
    page.get_by_role("menuitem", name="Passkeys").click()
    page.get_by_role("dialog").wait_for()
    page.locator("#new-label").fill("second")
    page.get_by_role("button", name="Add a passkey").click()
    page.get_by_role("dialog").get_by_role("alert").wait_for()  # excluded on this device
    new_authenticator(operator)  # the second device; the confirmation is still fresh
    page.get_by_role("button", name="Add a passkey").click()
    page.get_by_role("cell", name="second").wait_for()
    assert page.get_by_role("row").count() == 3  # header and two passkeys
    capture(operator, "SCR-03.passkeys")
    page.get_by_role("button", name="Close").click()


def test_br_203_used_link_shows_the_expired_page(browser: Any, watches: list[Watched]) -> None:
    w = open_page(browser, authenticator=False)
    watches.append(w)
    w.page.goto(STATE["link"])
    w.page.locator("[data-state='SCR-02.expired']").wait_for()
    assert w.page.url == f"{CONSOLE}/admin/enrol"
    capture(w, "SCR-02.expired")


def test_br_204_session_expired_returns_to_sign_in(operator: Watched) -> None:
    """SCR-03.session-expired: a 401 takes the console back to SCR-01."""
    page = operator.page
    page.goto(f"{CONSOLE}/admin/audit")
    page.get_by_label("Action prefix", exact=True).wait_for()
    operator.context.clear_cookies()
    page.get_by_role("button", name=EMAIL).click()
    page.get_by_role("menuitem", name="Passkeys").click()
    page.get_by_role("button", name="Sign in with a passkey").wait_for()
    capture(operator, "SCR-03.session-expired")
    page.get_by_role("button", name="Sign in with a passkey").click()
    page.get_by_role("button", name=EMAIL).wait_for()
    assert page.url == f"{CONSOLE}/admin/audit"


def test_br_202_limited_sign_in(browser: Any, watches: list[Watched]) -> None:
    """A real server 429 after five failures; clear it before the remaining suite signs in."""
    w = open_page(browser)
    watches.append(w)
    host("admin", "attempts", "clear")
    try:
        for _ in range(5):
            response = w.context.request.post(
                f"{CONSOLE}/admin/api/v1/enrol/options",
                data={"link": "invalid-browser-fixture-link"},
                headers={"Origin": CONSOLE},
            )
            assert response.status == 404
        w.page.goto(f"{CONSOLE}/admin")
        w.page.get_by_role("button", name="Sign in with a passkey").click()
        w.page.locator("[data-state='SCR-01.limited']").wait_for()
        capture(w, "SCR-01.limited")
    finally:
        host("admin", "attempts", "clear")


def test_br_202_blocked_sign_in(browser: Any, watches: list[Watched]) -> None:
    """Fixture models an operator whose address is outside the configured allow-list.

    The list is configured from a different allowed address inside the disposable test
    container; the host recovery command restores access even if an assertion fails.
    """
    w = open_page(browser)
    watches.append(w)
    w.page.goto(f"{CONSOLE}/admin")
    w.page.get_by_role("button", name="Sign in with a passkey").wait_for()
    script = (
        "import time; from hullgate.db import connect, transaction; "
        "from hullgate.admin.accounts import set_allowlist; "
        "c = connect('/data/hullgate.db'); "
        'exec("with transaction(c):\\n '
        "set_allowlist(c, ['192.0.2.0/24'], '192.0.2.1', 'operator-cli', int(time.time()))\")"
    )
    try:
        subprocess.run(
            ["docker", "exec", GATEWAY, "python", "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        w.page.get_by_role("button", name="Sign in with a passkey").click()
        w.page.locator("[data-state='SCR-01.blocked']").wait_for()
        assert w.page.get_by_role("button", name="Sign in with a passkey").is_disabled()
        capture(w, "SCR-01.blocked")
    finally:
        host("admin", "allowlist", "clear")
