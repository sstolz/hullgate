"""F-023 AC-02..06: real anchors, ordinary controls, no document reload or domain writes."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from urllib.parse import urlsplit

from browser_support import CONSOLE, GATEWAY, Watched
from console_support import seed, tree_page
from playwright.sync_api import expect


def counts() -> dict[str, int]:
    """Read only counts through the host, independent of the console API under test."""
    result = subprocess.run(
        [
            "docker",
            "exec",
            GATEWAY,
            "python",
            "-c",
            "import sqlite3,json; c=sqlite3.connect('/data/hullgate.db'); "
            "print(json.dumps({t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] "
            "for t in ('audit','node','connection','placement','operator_token')}))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value: dict[str, int] = json.loads(result.stdout)
    return value


def targets(page: Any) -> list[dict[str, str]]:
    value: list[dict[str, str]] = page.evaluate(
        "async () => (await (await fetch('/admin/api/v1/setup')).json()).steps"
    )
    assert len(value) == 5
    assert len({step["url"] for step in value}) == 5
    return value


def landed(page: Any, step: str, *, seeded: bool) -> None:
    if step in {"service", "connection"}:
        state = "SCR-07.declare" if step == "service" else "SCR-07.connect"
        control = page.locator(f'[role="dialog"][data-state="{state}"]')
        expect(control).to_be_visible()
        expect(control).to_be_in_viewport()
        expect(control.locator(":focus")).to_have_count(1)
        return
    if step == "client":
        control = page.locator(".tree .adding input" if seeded else "#first-client")
        if seeded:
            # The inline addition is immediately after the organisation row.
            expect(page.locator('[data-level="org"] + .adding input')).to_have_count(1)
    elif step == "placement":
        library = page.get_by_role("region", name="Library", exact=True)
        control = (
            library.get_by_role("button", name="Place in…", exact=True).first
            if seeded
            else (library.get_by_role("button", name="Connect…", exact=True))
        )
    else:
        control = page.locator("form.issue").get_by_role("combobox", name="Node", exact=True)
    expect(control).to_be_focused()
    expect(control).to_be_in_viewport()


def close_dialog(page: Any) -> None:
    if page.get_by_role("dialog").count():
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)


def follow_all(operator: Watched, *, seeded: bool, reopened: bool) -> None:
    page = operator.page
    steps = targets(page)
    before = counts()
    page.evaluate("window.hull109Marker = 'still-here'")
    for step in steps:
        if reopened:
            page.get_by_role("button", name="Organisation", exact=True).click()
            page.get_by_role("button", name="Setup guide", exact=True).click()
            guide = page.locator('[data-state="SCR-14.done"]')
        else:
            page.get_by_role("button", name="Tree", exact=True).click()
            guide = page.locator('[data-state="SCR-14"]')
        guide.locator(f'a[href="{step["url"]}"]').click()
        landed(page, step["id"], seeded=seeded)
        assert page.evaluate("window.hull109Marker") == "still-here"
        assert urlsplit(page.url).path == urlsplit(step["url"]).path
        close_dialog(page)
    assert counts() == before


def direct_all(operator: Watched, *, seeded: bool) -> None:
    page = operator.page
    steps = targets(page)
    before = counts()
    for step in steps:
        page.goto(CONSOLE + step["url"])
        landed(page, step["id"], seeded=seeded)
        assert urlsplit(page.url).path == urlsplit(step["url"]).path
        close_dialog(page)
    assert counts() == before


def test_br_229_empty_links_and_direct_targets(operator: Watched) -> None:
    # conftest orders this after enrolment, before dismissal or any domain seeding.
    tree_page(operator)
    follow_all(operator, seeded=False, reopened=False)
    direct_all(operator, seeded=False)


def test_br_229_seeded_reopened_links_and_direct_targets(operator: Watched) -> None:
    # The existing AC-21 test dismissed the guide; Organisation must reopen it.
    seed()
    tree_page(operator)
    follow_all(operator, seeded=True, reopened=True)
    direct_all(operator, seeded=True)
