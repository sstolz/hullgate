"""BR-232 (AC-19) — the four named flows by keyboard alone, with visible focus: sign in, place a
connection ("Place in…"), issue a token, delete a node. No click, no pointer: Tab, Shift+Tab,
arrows, Space, Enter, Delete. Then an axe audit of the console's screens, reported as a warning
and written to `$CAPTURES/axe-report.json` (BR-232: what it finds is a warning, not a failure)."""

from __future__ import annotations

import json
import warnings

from browser_support import CAPTURES, CONSOLE, EMAIL, Watched
from console_support import (
    DEV_OPS,
    SCRATCH,
    active,
    axe,
    focus_visible,
    paths,
    placements,
    seed,
    tab_to,
    tree_page,
)

KBD = "acme/kbd"


def test_br_232_sign_in_by_keyboard(operator: Watched) -> None:
    seed()
    page = operator.page
    operator.context.clear_cookies()
    page.goto(f"{CONSOLE}/admin/tree")
    page.get_by_role("button", name="Sign in with a passkey").wait_for()
    tab_to(operator, "e.textContent.trim() === 'Sign in with a passkey'")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.get_by_role("button", name=EMAIL).wait_for()
    assert page.url == f"{CONSOLE}/admin/tree"


def test_br_232_place_a_connection_by_keyboard(operator: Watched) -> None:
    page = operator.page
    tree_page(operator)
    page.get_by_role("button", name=f"Inspect connection {KBD}").wait_for()
    tab_to(
        operator,
        "e.textContent.trim() === 'Place in…' &&"
        f" e.closest('li')?.querySelector('.name')?.textContent === '{KBD}'",
    )
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.locator("[data-state='place']").wait_for()
    # the dialog focuses its first free role; move to DEV_OPS and tick it
    tab_to(operator, f"e.type === 'checkbox' && e.value === '{DEV_OPS}'")
    assert focus_visible(operator)
    page.keyboard.press("Space")
    assert active(operator, "e.checked")
    tab_to(operator, "e.textContent.trim() === 'Place in 1 role(s)'")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.get_by_text(f"“{KBD}” placed on {DEV_OPS}.").wait_for()
    assert DEV_OPS in placements(KBD)


def test_br_232_issue_a_token_by_keyboard(operator: Watched) -> None:
    page = operator.page
    tree_page(operator)
    tab_to(operator, "e.textContent.trim() === 'Tokens' && e.classList.contains('tab')")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.get_by_role("heading", name="Issue a token").wait_for()
    tab_to(operator, "e.tagName === 'SELECT' && !!e.closest('form.issue')")
    assert focus_visible(operator)
    for _ in range(60):  # arrows change a closed select's value in Chromium
        if active(operator, "e.value") == DEV_OPS:
            break
        page.keyboard.press("ArrowDown")
    assert active(operator, "e.value") == DEV_OPS
    page.keyboard.press("Tab")
    assert focus_visible(operator)
    page.keyboard.type("keyboard token")
    tab_to(operator, "e.textContent.trim() === 'Issue'")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    shown = page.locator("[data-state='SCR-09.value-shown']")
    shown.wait_for()
    value = shown.locator("code").inner_text()
    assert len(value) > 20
    tab_to(operator, "e.textContent.trim() === 'I copied it — hide'")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    shown.wait_for(state="detached")
    assert value not in page.content()  # shown once (BR-053 v3)
    page.get_by_role("cell", name="keyboard token").wait_for()


def test_br_232_delete_a_node_by_keyboard(operator: Watched) -> None:
    page = operator.page
    tree_page(operator)
    tab_to(operator, "e.getAttribute('role') === 'treeitem'")
    assert focus_visible(operator)
    for _ in range(40):
        if active(operator, "e.dataset.path") == SCRATCH:
            break
        page.keyboard.press("ArrowDown")
    assert active(operator, "e.dataset.path") == SCRATCH
    assert focus_visible(operator)
    page.keyboard.press("Delete")
    dialog = page.locator("[data-state='impact']")
    dialog.wait_for()
    dialog.get_by_text("Descendants deleted").wait_for()  # the server's numbers (BR-214)
    tab_to(
        operator,
        "e.textContent.trim() === 'Delete' && !e.disabled"
        " && !!e.closest(\"[data-state='impact']\")",
    )
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    dialog.wait_for(state="detached")
    page.locator(f"[role='treeitem'][data-path='{SCRATCH}']").wait_for(state="detached")
    assert SCRATCH not in paths()


def test_br_232_axe_audit_is_reported(operator: Watched) -> None:
    page = operator.page
    reports = []
    for screen, path, ready in [
        ("tree", "/admin/tree", "[role='treeitem'][data-path='/']"),
        ("tokens", "/admin/tokens", "form.issue"),
        ("audit", "/admin/audit", "form.filters"),
    ]:
        page.goto(f"{CONSOLE}{path}")
        page.locator(ready).wait_for()
        reports.append(axe(operator, screen))
    CAPTURES.mkdir(parents=True, exist_ok=True)
    report = CAPTURES / "axe-report.json"
    existing = json.loads(report.read_text()) if report.exists() else []
    report.write_text(json.dumps([*existing, *reports], indent=2) + "\n")
    found = [f"{r['screen']}: {v['id']} ({v['impact']})" for r in reports for v in r["violations"]]
    if found:
        warnings.warn("axe (BR-232, warning): " + "; ".join(found), stacklevel=1)
