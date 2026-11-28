"""BR-078 v3, BR-079 (AC-18) — a placement that would share a connection across clients opens
SCR-07.share: the server's warning, the recommendation of a dedicated connection whose button
opens the connect dialog, and a checkbox unticked by default without which nothing is placed.
With the box ticked, the placement keeps its confirmation and the audit records it."""

from __future__ import annotations

from browser_support import Watched
from console_support import BETA_ROLE, audit_actions, both_themes, placements, seed, tree_page

SHARED = "acme/shared"


def _ask_share(w: Watched) -> None:
    page = w.page
    tree_page(w)
    page.get_by_role("button", name=f"Inspect connection {SHARED}").wait_for()
    item = page.locator("li.conn").filter(has_text=SHARED)
    item.get_by_role("button", name="Place in…").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("checkbox", name=BETA_ROLE).check()
    dialog.get_by_role("button", name="Place in 1 role(s)").click()
    page.locator("[data-state='SCR-07.share']").wait_for()


def test_br_078_share_needs_the_ticked_box(operator: Watched) -> None:
    seed()
    _ask_share(operator)
    page = operator.page
    share = page.locator("[data-state='SCR-07.share']")
    box = share.get_by_role("checkbox")
    assert not box.is_checked()  # unticked by default
    assert share.get_by_role("button", name="Place anyway").is_disabled()
    assert "call limit" in share.inner_text()  # BR-079: neighbours share the limit
    assert share.get_by_role("button", name="Connect a dedicated one").is_visible()
    both_themes(operator, "SCR-07.share")
    page.keyboard.press("Escape")
    share.wait_for(state="detached")
    assert BETA_ROLE not in placements(SHARED)


def test_br_078_dedicated_one_opens_the_connect_dialog(operator: Watched) -> None:
    _ask_share(operator)
    page = operator.page
    page.get_by_role("button", name="Connect a dedicated one").click()
    connect = page.locator("[data-state='SCR-07.connect']")
    connect.wait_for()
    assert (
        connect.locator("label:has-text('Service') select").first.input_value() == "gitea"
    )  # the shared one's service
    both_themes(operator, "SCR-07.connect")
    page.keyboard.press("Escape")
    connect.wait_for(state="detached")
    assert BETA_ROLE not in placements(SHARED)


def test_br_078_ticked_box_places_and_is_audited(operator: Watched) -> None:
    _ask_share(operator)
    page = operator.page
    share = page.locator("[data-state='SCR-07.share']")
    share.get_by_role("checkbox").check()
    share.get_by_role("button", name="Place anyway").click()
    share.wait_for(state="detached")
    page.get_by_text(f"“{SHARED}” placed on {BETA_ROLE}.").wait_for()
    assert BETA_ROLE in placements(SHARED)
    confirmed = [e for e in audit_actions() if e.get("action") == "connection.share_confirmed"]
    assert confirmed, "the confirmation is written to the audit"
    assert str(confirmed[0].get("actor", "")).startswith("admin:")
