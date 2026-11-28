"""BR-216, BR-082 v3 (AC-15) — a connection is authorised from the console against the run's fake
provider (tests/browser/provider.py). With the web return registered, the browser goes to the
provider and comes back to /admin/oauth/callback, which posts `state` and `code` under the
session and leaves the address bar clean; the connection ends active. An application without the
web return shows the paste path (SCR-07.connect-paste)."""

from __future__ import annotations

import subprocess
from urllib.parse import parse_qs, urlencode, urlsplit

from browser_support import CONSOLE, EMAIL, GATEWAY, Watched
from console_support import both_themes, seed, status, tree_page

WEB_CALLBACK = f"{CONSOLE}/admin/oauth/callback"


def _connect(w: Watched, application: str, name: str, prefix: str) -> None:
    page = w.page
    tree_page(w)
    page.get_by_role("button", name="Connect…").click()
    dialog = page.locator("[data-state='SCR-07.connect']")
    dialog.wait_for()
    dialog.locator("label:has-text('Service') select").first.select_option("gitea")
    dialog.locator("label:has-text('Application') select").first.select_option(application)
    dialog.locator("label:has-text('Client') select").first.select_option("acme")
    dialog.get_by_label("Name", exact=True).fill(name)
    dialog.get_by_label("Tool prefix").fill(prefix)
    dialog.get_by_role("button", name="Connect", exact=True).click()


def test_br_216_without_web_return_the_paste_path(operator: Watched) -> None:
    seed()
    page = operator.page
    _connect(operator, "app", "paste", "pa")
    paste = page.locator("[data-state='SCR-07.connect-paste']")
    paste.wait_for()
    url = paste.locator("code").inner_text()
    assert parse_qs(urlsplit(url).query)["redirect_uri"][0] != WEB_CALLBACK
    assert status("acme/paste")["stored_state"] == "pending"
    capture_query = parse_qs(urlsplit(url).query)
    capture_query["state"] = ["EXAMPLE-state"]
    capture_query["code_challenge"] = ["EXAMPLE-code-challenge"]
    capture_url = urlsplit(url)._replace(query=urlencode(capture_query, doseq=True)).geturl()
    both_themes(
        operator,
        "SCR-07.connect-paste",
        capture_text={"[data-state='SCR-07.connect-paste'] code": capture_url},
    )
    # the operator authorises at the provider, which sends them to the application's URI
    answer = operator.context.request.get(url, max_redirects=0, ignore_https_errors=True)
    assert answer.status == 302
    back = answer.headers["location"]
    paste.get_by_label("Return URL").fill(back)
    paste.get_by_role("button", name="Finish").click()
    done = page.get_by_role("dialog", name="Connection authorised")
    done.wait_for()
    assert "browser-bot" in done.inner_text()  # the account the provider names
    done.get_by_role("button", name="Close").first.click()
    assert status("acme/paste")["stored_state"] == "active"


def test_br_216_web_return_flow_ends_active_with_a_clean_address_bar(operator: Watched) -> None:
    page = operator.page
    tree_page(operator)
    app = page.locator("button.svc:has(span.mono:text-is('web'))")
    app.click()
    sheet = page.locator("section.sheet").filter(has_text="outbound application")
    sheet.get_by_text(WEB_CALLBACK).wait_for()  # the URI to add at the provider, from the server
    sheet.get_by_role("checkbox").check()
    app.locator(".badge").wait_for()  # the server's answer: web return registered
    seen: list[str] = []
    page.on("framenavigated", lambda frame: seen.append(frame.url))
    _connect(operator, "web", "webflow", "wf")
    page.wait_for_url(f"{WEB_CALLBACK}*")
    if page.get_by_role("button", name="Sign in with a passkey").count():
        page.get_by_role("button", name="Sign in with a passkey").click()  # SameSite=Strict
    page.get_by_text("“acme/webflow” is authorised.").wait_for()
    assert page.url == WEB_CALLBACK  # code and state left the address bar
    returns = [u for u in seen if u.startswith(f"{WEB_CALLBACK}?") and "code=" in u]
    assert returns
    # AC-15: the code the browser carried is in no log of the gateway's run (stdout and stderr)
    code = parse_qs(urlsplit(returns[0]).query)["code"][0]
    logs = subprocess.run(["docker", "logs", GATEWAY], capture_output=True, text=True, check=True)
    assert code not in logs.stdout + logs.stderr
    assert status("acme/webflow")["stored_state"] == "active"
    assert status("acme/webflow")["account"] == "browser-bot"
    page.get_by_role("button", name="Continue to the tree").click()
    page.wait_for_url(f"{CONSOLE}/admin/tree")
    page.get_by_role("button", name=EMAIL).wait_for()
