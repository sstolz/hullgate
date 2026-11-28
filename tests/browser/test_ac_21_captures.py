"""AC-21 — the visual states of slice 2's screens (§3.5 to §3.11), dark and light, written to
`$CAPTURES` as `<state>@<theme>.png` for the reading by hand at review. SCR-07.empty is taken
before seeding (test_ac_21_empty), SCR-07.share, SCR-07.connect and SCR-07.connect-paste in the
flows that open them (test_br_078, test_br_216). SCR-07.expiring is not drawn in this slice: the
server sends no such state."""

from __future__ import annotations

from browser_support import CONSOLE, Watched, host
from console_support import (
    BETA_ROLE,
    DEV_IDLE,
    DEV_OPS,
    PROD_ROLE,
    both_themes,
    role_row,
    seed,
    status,
    tree_page,
)


def _state(w: Watched, state: str, *, capture_text: dict[str, str] | None = None) -> None:
    w.page.locator(f"[data-state='{state}']").first.wait_for()
    both_themes(w, state, capture_text=capture_text)


def _node(w: Watched, path: str, state: str) -> None:
    role_row(w, path).click()
    _state(w, state)


def test_ac_21_tree_and_inspector(operator: Watched) -> None:
    seed()
    host("settings", "set", "acme", "token_duration", "30d")
    tree_page(operator)
    _state(operator, "SCR-04")
    _node(operator, DEV_OPS, "SCR-05")
    _node(operator, DEV_IDLE, "SCR-05.empty")
    _node(operator, PROD_ROLE, "SCR-05.sensitive")
    _node(operator, "acme/web", "SCR-06")
    _node(operator, "acme", "SCR-06.overridden")


def test_ac_21_map(operator: Watched) -> None:
    page = operator.page
    tree_page(operator)
    page.get_by_role("button", name="Map").click()
    page.get_by_role("button", name="Inspect connection acme/shared").hover()
    page.get_by_text("“acme/shared”: 2 placement(s) highlighted").wait_for()
    _state(operator, "SCR-04.map")
    page.get_by_role("button", name="List").click()


def test_ac_21_locked(operator: Watched) -> None:
    host("tree", "lock", "acme/web/prod")
    try:
        tree_page(operator)
        _node(operator, PROD_ROLE, "SCR-04.locked")
        assert "Locked by acme/web/prod" in operator.page.locator("section.insp").inner_text()
    finally:
        host("tree", "unlock", "acme/web/prod")


def test_ac_21_narrow(operator: Watched) -> None:
    page = operator.page
    page.set_viewport_size({"width": 800, "height": 900})
    try:
        tree_page(operator)
        _state(operator, "SCR-04.narrow")
    finally:
        page.set_viewport_size({"width": 1280, "height": 800})


def test_ac_21_connection_sheets(operator: Watched) -> None:
    page = operator.page
    tree_page(operator)
    page.get_by_role("button", name="Inspect connection acme/shared").click()
    _state(operator, "SCR-07")
    host("connection", "add", "acme/pending", "--service", "gitea", "--application", "app",
         "--prefix", "pe")  # fmt: skip
    tree_page(operator)
    page.get_by_role("button", name="Inspect connection acme/pending").click()
    _state(operator, "SCR-07.pending")


def test_ac_21_revoke_through_the_impact_dialog(operator: Watched) -> None:
    """Revoking keeps the connection, its prefix and placements (BR-095); the sheet then asks
    for a new authorisation."""
    page = operator.page
    tree_page(operator)
    page.get_by_role("button", name="Inspect connection acme/webflow").click()
    page.locator("section.sheet").get_by_role("button", name="Revoke").click()
    dialog = page.locator("[data-state='impact']")
    dialog.get_by_text("Placements whose tools are hidden").wait_for()
    both_themes(operator, "SCR-07.revoke-impact")
    dialog.get_by_role("button", name="Revoke").click()
    dialog.wait_for(state="detached")
    view = status("acme/webflow")
    assert view["prefix"] == "wf"
    assert view["stored_state"] != "active"
    tree_page(operator)
    page.get_by_role("button", name="Inspect connection acme/webflow").click()
    page.locator("section.sheet").wait_for()
    if view["stored_state"] == "reauth_required":
        _state(operator, "SCR-07.reauth-required")


def test_ac_21_tokens(operator: Watched) -> None:
    page = operator.page
    page.goto(f"{CONSOLE}/admin/tokens")
    page.locator("form.issue").wait_for()
    _state(operator, "SCR-09")
    page.locator(".filter select").select_option(BETA_ROLE)
    _state(operator, "SCR-09.empty")
    form = page.locator("form.issue")
    form.locator("select").select_option(PROD_ROLE)
    form.get_by_placeholder("e.g. Claude Code, laptop").fill("prod reader")
    form.get_by_placeholder("30d").fill("90d")
    form.get_by_role("button", name="Issue").click()
    token_selector = "[data-state='SCR-09.value-shown'] code.secret"  # noqa: S105 — a CSS selector
    assert page.locator(token_selector).inner_text().startswith("hg_")
    capture_text = {token_selector: "hg_EXAMPLE-not-a-real-token-0000000000"}
    _state(operator, "SCR-09.sensitive-ceiling", capture_text=capture_text)
    _state(operator, "SCR-09.value-shown", capture_text=capture_text)
    page.get_by_role("button", name="I copied it — hide").click()


def test_ac_21_audit(operator: Watched) -> None:
    page = operator.page
    page.goto(f"{CONSOLE}/admin/audit")
    page.locator("table").wait_for()
    _state(operator, "SCR-11")
    page.get_by_label("Actor").fill("nobody:at-all")
    page.get_by_role("button", name="Filter").click()
    _state(operator, "SCR-11.empty")
