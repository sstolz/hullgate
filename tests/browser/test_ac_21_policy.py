"""AC-21 for slice 3's screens (F-016 §3.6, §3.8, §3.10), dark and light, with the acts that reach
them: the rights preview of a role and of a token on a role (SCR-08, BR-217), the placement
policy editor (SCR-05.policy, BR-215's notice), the queue to classify and the pinned changes
(SCR-10). The tools come from the run's fake MCP upstream (tests/browser/upstream.py); the states
are the server's, the console shows them. Then BR-232 at warning level: the new acts by keyboard
with visible focus, and axe on the new screens, appended to `$CAPTURES/axe-report.json`."""

from __future__ import annotations

import json
import warnings
from typing import Any

from browser_support import CAPTURES, CONSOLE, Watched, host
from console_support import (
    DEV_FLAKY,
    DEV_IDLE,
    DEV_TOOLS,
    TRACKER,
    active,
    axe,
    both_themes,
    change_upstream_definition,
    focus_visible,
    role_row,
    seed_tools,
    tab_to,
    tree_page,
    wait_until,
)

PREVIEW = "[role='dialog'][data-state='SCR-08.dialog']"


def _state(w: Watched, state: str) -> None:
    w.page.locator(f"[data-state='{state}']").first.wait_for()
    both_themes(w, state)


def _read_summary(w: Watched) -> None:
    """The SCR-05 summary is read on the operator's click only: selecting a role sends no
    `POST rights` (an audited fresh listing)."""
    w.page.get_by_role("button", name="Read the rights preview").click()
    w.page.locator("[data-state='SCR-05.rights']").get_by_text("exposed ·").wait_for()


def _open_preview(w: Watched, role: str) -> Any:
    page = w.page
    tree_page(w)
    role_row(w, role).click()
    page.locator("[data-state='SCR-05.rights'] p").first.wait_for()
    _read_summary(w)
    page.get_by_role("button", name="Open the rights preview").click()
    dialog = page.locator(PREVIEW)
    dialog.wait_for()
    return dialog


def _close(w: Watched) -> None:
    """Closes the preview by its button (Escape is played with the focus inside, in the keyboard
    test below)."""
    w.page.locator(PREVIEW).get_by_role("button", name="Close").click()
    w.page.locator(PREVIEW).wait_for(state="detached")


def _policy(role: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(host("policy", "show", TRACKER, "--role", role, "--json"))
    return doc


def _queue() -> list[dict[str, Any]]:
    doc = json.loads(host("tool", "queue", "--json"))
    return doc if isinstance(doc, list) else doc.get("queue", [])


def test_ac_21_tools_queue(operator: Watched) -> None:
    seed_tools()
    page = operator.page
    assert len(_queue()) == 4
    page.goto(f"{CONSOLE}/admin/tools")
    page.locator("table tbody tr").first.wait_for()
    # the badge is the server's count of the queue (§3.4)
    page.get_by_role("button", name="Tools, 4 to classify").wait_for()
    _state(operator, "SCR-10")
    text = page.locator("[data-state='SCR-10']").inner_text()
    assert "delete_repo" in text
    assert "list_issues" in text


def test_ac_21_rights_preview(operator: Watched) -> None:
    token = seed_tools()
    dialog = _open_preview(operator, DEV_TOOLS)
    dialog.locator("[data-state='SCR-08'] table").first.wait_for()
    preview = json.loads(host("policy", "preview", DEV_TOOLS, "--json"))
    rows = preview if isinstance(preview, list) else preview.get("tools", [])
    shown = dialog.inner_text()
    for row in rows:
        assert row["public_name"] in shown
    assert "above_mode" in shown  # the server's reason code, beside its words
    _state(operator, "SCR-08")
    # the token variant: a token picked from the list, out of this role's scope
    picker = dialog.locator("form.token select")
    picker.locator(f"option[value='{token}']").wait_for(state="attached")
    picker.select_option(token)
    dialog.get_by_role("button", name="Test the token").click()
    dialog.locator("[data-state='SCR-08.out-of-scope'] .refused").wait_for()
    assert "does not reach this role" in dialog.inner_text()
    _state(operator, "SCR-08.out-of-scope")
    _close(operator)
    dialog.wait_for(state="detached")

    dialog = _open_preview(operator, DEV_FLAKY)
    dialog.locator("[data-state='SCR-08.unknown'] .unknown").wait_for()
    assert "acme/outage" in dialog.inner_text()
    _state(operator, "SCR-08.unknown")
    _close(operator)

    dialog = _open_preview(operator, DEV_IDLE)
    dialog.locator("[data-state='SCR-08.empty'] .empty").wait_for()
    _state(operator, "SCR-08.empty")
    _close(operator)


def test_ac_21_placement_policy(operator: Watched) -> None:
    seed_tools()
    page = operator.page
    tree_page(operator)
    role_row(operator, DEV_TOOLS).click()
    page.locator("[data-state='SCR-05.rights'] p").first.wait_for()
    page.get_by_role("button", name="Edit the policy").click()
    editor = page.locator("[data-state='SCR-05.policy']")
    editor.wait_for()
    editor.get_by_role("button", name="Allowlist every read tool").click()
    editor.get_by_text("Policy saved.").wait_for()
    page.locator("[data-state='SCR-03.tools-notice']").wait_for()  # BR-215
    deny = editor.get_by_label("Tool (upstream name) · deny")
    deny.fill("label_issue")
    deny.press("Enter")
    editor.locator("fieldset", has_text="Deny").get_by_text("label_issue").wait_for()
    # a refusal is the server's text (BR-211): a name the catalogue does not hold
    opened = editor.get_by_label("Tool (upstream name) · open")
    opened.fill("no_such_tool")
    opened.press("Enter")
    editor.locator("[role='alert']").wait_for()
    policy = _policy(DEV_TOOLS)
    assert policy["allowlist"] == ["list_issues"]
    assert policy["deny"] == ["label_issue"]
    assert policy["open"] == []
    _state(operator, "SCR-05.policy")
    # put the placement back as it was for the next tests
    host("policy", "remove", TRACKER, "--role", DEV_TOOLS, "allowlist", "list_issues")
    host("policy", "remove", TRACKER, "--role", DEV_TOOLS, "deny", "label_issue")


def test_ac_21_pinned_changes(operator: Watched) -> None:
    seed_tools()
    page = operator.page
    host("policy", "pin", TRACKER, "--role", DEV_TOOLS, "on")
    host("policy", "preview", DEV_TOOLS)  # the baselines of the pinned definitions
    change_upstream_definition()
    page.goto(f"{CONSOLE}/admin/tools")
    form = page.locator("form.pick")
    form.locator("select").select_option(DEV_TOOLS)
    form.get_by_role("button", name="Read the changes").click()
    diff = page.locator("[data-state='SCR-10.diff']")
    diff.wait_for()
    text = diff.inner_text()
    assert "create_issue" in text
    assert "assign it to every member" in text  # after, as text
    _state(operator, "SCR-10.diff")
    diff.get_by_role("button", name=f"Validate every change of {TRACKER}").click()
    page.get_by_text("definition(s) validated.").wait_for()
    page.get_by_text(f"No change on the pinned definitions under {DEV_TOOLS}.").wait_for()
    host("policy", "pin", TRACKER, "--role", DEV_TOOLS, "inherit")


def test_br_182_a_validation_names_what_the_operator_saw(operator: Watched) -> None:
    """Review 579 B1. `acme/tracker` is pinned on two roles under `acme/web/dev` and its
    `create_issue` changes upstream on both: a row's Validate lifts that row's suspension only,
    and a node chosen while a read is on its way does not become the scope of the changes
    shown, nor of the connection's Validate."""
    seed_tools()
    page = operator.page
    dev = "acme/web/dev"
    change_upstream_definition("reset")
    for role in (DEV_TOOLS, DEV_FLAKY):
        host("policy", "pin", TRACKER, "--role", role, "on")
        host("policy", "preview", role)
    host("tool", "validate", dev, TRACKER)  # both roles' baselines: the original definition
    change_upstream_definition()
    page.goto(f"{CONSOLE}/admin/tools")
    form = page.locator("form.pick")
    form.locator(f"select option[value='{dev}']").wait_for(state="attached")
    form.locator("select").select_option(dev)
    form.get_by_role("button", name="Read the changes").click()
    diff = page.locator("[data-state='SCR-10.diff']")
    diff.get_by_role("button", name=f"Validate create_issue ({DEV_TOOLS})").click()
    page.get_by_text("definition(s) validated.").wait_for()
    left = json.loads(host("tool", "diff", dev, "--json"))["changes"]
    assert [(c["role"], c["tool"]) for c in left] == [(DEV_FLAKY, "create_issue")]

    # the read of `dev` is held while the operator picks DEV_TOOLS in the select
    held: list[Any] = []

    def hold(route: Any) -> None:
        held.append(route)

    page.route("**/admin/api/v1/tools/diff", hold)
    form.get_by_role("button", name="Read the changes").click()
    while not held:
        page.wait_for_timeout(50)
    form.locator("select").select_option(DEV_TOOLS)
    # synchronised on the released read itself: the row below was already on screen before it
    # (review 581 N6); `continue_` comes before `unroute`, which would release it on its own
    assert form.get_attribute("aria-busy") == "true"  # the read is the held one
    with page.expect_response(lambda r: r.url.endswith("/admin/api/v1/tools/diff")) as released:
        held[0].continue_()
    assert released.value.ok
    page.unroute("**/admin/api/v1/tools/diff")
    page.locator("form.pick[aria-busy='false']").wait_for()  # the read's state is applied
    diff.get_by_role("button", name=f"Validate create_issue ({DEV_FLAKY})").wait_for()
    sent: list[Any] = []

    def seen(request: Any) -> None:
        if request.url.endswith("/admin/api/v1/tools/validate"):
            sent.append(json.loads(request.post_data or "{}"))

    page.on("request", seen)
    diff.get_by_role("button", name=f"Validate every change of {TRACKER}").click()
    page.get_by_text("definition(s) validated.").wait_for()
    page.remove_listener("request", seen)
    assert sent == [{"path": dev, "connection": TRACKER}]
    assert json.loads(host("tool", "diff", dev, "--json"))["changes"] == []
    for role in (DEV_TOOLS, DEV_FLAKY):
        host("policy", "pin", TRACKER, "--role", role, "inherit")


def test_br_232_classify_by_keyboard(operator: Watched) -> None:
    """The queue's acts by keyboard alone, with visible focus; the queue is emptied, the last
    tools through the pointer (SCR-10.empty)."""
    seed_tools()
    page = operator.page
    page.goto(f"{CONSOLE}/admin/tools")
    page.locator("table tbody tr").first.wait_for()
    before = len(_queue())
    tab_to(operator, "e.textContent.trim() === 'write' && !!e.closest('[role=group]')")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    wait_until(lambda: len(_queue()) == before - 1)
    page.get_by_text("(manual).").wait_for()
    while page.locator("table tbody tr").count() > 0:
        count = page.locator("table tbody tr").count()
        page.locator("table tbody tr").first.get_by_role("button", name="Accept").click()
        wait_until(lambda n=count: page.locator("table tbody tr").count() < n)
    assert _queue() == []
    page.get_by_role("button", name="Tools", exact=True).wait_for()  # no badge left
    _state(operator, "SCR-10.empty")


def test_br_232_preview_and_policy_by_keyboard(operator: Watched) -> None:
    seed_tools()
    page = operator.page
    tree_page(operator)
    rights: list[str] = []

    def seen(request: Any) -> None:
        if request.url.endswith("/admin/api/v1/rights"):
            rights.append(request.method)

    page.on("request", seen)
    role_row(operator, DEV_TOOLS).click()
    page.locator("[data-state='SCR-05.rights'] p").first.wait_for()
    page.get_by_role("button", name="Edit the policy").wait_for()  # the role's reads are done
    # selecting a role reads no preview: that listing is audited, it runs on the operator's ask
    assert rights == []
    assert page.get_by_role("button", name="Open the rights preview").count() == 0
    tab_to(operator, "e.textContent.trim() === 'Read the rights preview'")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.locator("[data-state='SCR-05.rights']").get_by_text("exposed ·").wait_for()
    page.remove_listener("request", seen)
    assert rights == ["POST"]
    tab_to(operator, "e.textContent.trim() === 'Open the rights preview'", back=True)
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.locator(PREVIEW).wait_for()
    page.keyboard.press("Escape")
    page.locator(PREVIEW).wait_for(state="detached")
    assert active(operator, "e.textContent.trim()") == "Open the rights preview"
    tab_to(operator, "e.textContent.trim() === 'Edit the policy'")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.locator("[data-state='SCR-05.policy']").wait_for()
    tab_to(operator, "e.textContent.trim() === 'Allowlist every read tool'")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.locator("[data-state='SCR-05.policy']").get_by_text("Policy saved.").wait_for()
    assert _policy(DEV_TOOLS)["allowlist"] == ["list_issues"]
    host("policy", "remove", TRACKER, "--role", DEV_TOOLS, "allowlist", "list_issues")


def test_br_232_axe_on_slice_3_screens(operator: Watched) -> None:
    seed_tools()
    page = operator.page
    reports = []
    page.goto(f"{CONSOLE}/admin/tools")
    page.locator("[data-state^='SCR-10']").first.wait_for()
    reports.append(axe(operator, "tools"))
    tree_page(operator)
    role_row(operator, DEV_TOOLS).click()
    page.locator("[data-state='SCR-05.rights'] p").first.wait_for()
    _read_summary(operator)
    page.get_by_role("button", name="Edit the policy").click()
    page.locator("[data-state='SCR-05.policy']").wait_for()
    reports.append(axe(operator, "role inspector with the policy editor"))
    page.get_by_role("button", name="Open the rights preview").click()
    page.locator(f"{PREVIEW} table").first.wait_for()
    reports.append(axe(operator, "rights preview"))
    _close(operator)
    target = CAPTURES / "axe-report.json"
    earlier = json.loads(target.read_text()) if target.exists() else []
    target.write_text(json.dumps([*earlier, *reports], indent=2) + "\n")
    found = [f"{r['screen']}: {v['id']} ({v['impact']})" for r in reports for v in r["violations"]]
    if found:
        warnings.warn("axe (BR-232, warning): " + "; ".join(found), stacklevel=1)
