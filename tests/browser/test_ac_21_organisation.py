"""Slice 4: real screen acts and dark/light captures. Docker/browser category, not local pytest."""

import subprocess
from datetime import UTC, datetime, timedelta

from browser_support import CONSOLE, GATEWAY, Watched, host
from console_support import both_themes, seed, seed_tools, tree_page


def test_ac_21_setup_guide(operator: Watched) -> None:
    # Played before seeding; the empty instance is the actual server state.
    tree_page(operator)
    guide = operator.page.locator("[data-state='SCR-14']")
    guide.wait_for()
    assert guide.get_by_role("link").count() == 5
    both_themes(operator, "SCR-14")
    guide.get_by_role("button", name="Dismiss", exact=True).click()
    guide.wait_for(state="detached")
    operator.page.goto(f"{CONSOLE}/admin/organisation")
    operator.page.get_by_role("button", name="Setup guide", exact=True).click()
    operator.page.locator("[data-state='SCR-14.done']").wait_for()
    both_themes(operator, "SCR-14.done")


def test_ac_21_organisation_and_fresh_allowlist(operator: Watched) -> None:
    page = operator.page
    page.goto(f"{CONSOLE}/admin/organisation")
    page.get_by_label("Organisation name", exact=True).wait_for()
    both_themes(operator, "SCR-13")
    # Force the existing session's freshness to expire without invalidating the session.
    subprocess.run(
        [
            "docker",
            "exec",
            GATEWAY,
            "python",
            "-c",
            "import sqlite3; c=sqlite3.connect('/data/hullgate.db'); "
            "c.execute('UPDATE admin_session SET fresh_until=0'); c.commit()",
        ],
        check=True,
    )
    page.get_by_label("IP allow-list (one CIDR per line)").fill("192.0.2.0/24")
    page.locator("form").filter(has=page.locator("textarea")).get_by_role(
        "button", name="Save"
    ).click()
    page.locator("[data-state='SCR-13.fresh-required']").wait_for()
    both_themes(operator, "SCR-13.fresh-required")
    page.get_by_role("button", name="Confirm with a passkey", exact=True).click()
    page.locator("[data-state='SCR-13.allowlist-self']").wait_for()
    both_themes(operator, "SCR-13.allowlist-self")
    assert page.locator("[role='alert']").inner_text()


def test_ac_21_expiring_threshold(operator: Watched) -> None:
    seed()
    end = (datetime.now(UTC) + timedelta(days=6)).isoformat()
    host(
        "connection",
        "add",
        "acme/expiring",
        "--service",
        "gitea",
        "--provided",
        "--file",
        "-",
        "--prefix",
        "exp",
        "--expires-at",
        end,
        stdin="browser-expiring-token\n",
    )
    tree_page(operator)
    operator.page.get_by_text("acme/expiring", exact=True).first.click()
    operator.page.locator("[data-state='SCR-07.expiring']").wait_for()
    both_themes(operator, "SCR-07.expiring")
    operator.page.goto(f"{CONSOLE}/admin/organisation")
    field = operator.page.get_by_label("Expiry warning threshold (whole days, 1–90)")
    field.fill("5")
    operator.page.locator("form").filter(has=field).get_by_role("button", name="Save").click()
    operator.page.get_by_role("status").filter(has_text="Done.").wait_for()
    tree_page(operator)
    operator.page.get_by_text("acme/expiring", exact=True).first.click()
    operator.page.locator(".sheet").filter(has_text="acme/expiring").wait_for()
    assert operator.page.locator("[data-state='SCR-07.expiring']").count() == 0
    host("settings", "set", "/", "connection_expiry_days", "7")


def test_br_211_catalogue_reclassify_and_clear(operator: Watched) -> None:
    seed_tools()
    host("tool", "classify", "tracker", "create_issue", "write")
    page = operator.page
    page.goto(f"{CONSOLE}/admin/tools")
    row = (
        page.get_by_role("region", name="Classified tools")
        .locator(".change")
        .filter(has_text="create_issue")
    )
    row.get_by_role("button", name="read", exact=True).click()
    row.get_by_text("(manual)", exact=False).wait_for()
    row.get_by_role("button", name="Clear", exact=True).click()
    page.get_by_role("status").filter(has_text="create_issue").wait_for()


def test_br_182_type_mark_is_visible(operator: Watched) -> None:
    """Transport values have the same spelling; the server's type mark must remain visible."""
    page = operator.page
    route = "**/admin/api/v1/tools/diff"
    page.route(
        route,
        lambda r: r.fulfill(
            json={
                "node": "/",
                "changes": [
                    {
                        "connection": "acme/tracker",
                        "role": "acme/web/dev/tools",
                        "tool": "create_issue",
                        "state": "changed",
                        "baseline_sha256": "a",
                        "current_sha256": "b",
                        "fields": [
                            {
                                "field": "inputSchema",
                                "old": {"default": "NaN"},
                                "new": {"default": "NaN"},
                                "type_marks": [
                                    {"path": "/default", "old": "string", "new": "number"}
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
    )
    try:
        page.goto(f"{CONSOLE}/admin/tools")
        page.get_by_role("button", name="Read the changes", exact=True).click()
        page.get_by_text("/default: string → number", exact=True).wait_for()
        both_themes(operator, "SCR-10.diff-types")
    finally:
        page.unroute(route)
