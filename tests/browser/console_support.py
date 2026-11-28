"""Helpers of the slice 2 browser tests (F-016 §3.5 to §3.11): the seeded tree, library and
connections, written through the host CLI inside the gateway container as an operator would, and
keyboard helpers for BR-232.

Seeded once per run (`seed()`), after test_br_202 has enrolled the operator and made the vault
ready:

    acme (client)
      web (project)
        dev (environment)     principal, ops, idle (roles)
        prod (environment, sensitive)   principal
        scratch (environment)  principal          deleted by keyboard in test_br_232
    beta (client)
      app (project) / dev (environment) / principal

Service `gitea` on the fake provider (an internal host of the run's network, BR-084), with the
declared applications `app` (no web return: the paste path) and `web` (its web return is
registered from the console in test_br_216). Provided-token connections `acme/shared` (placed on
`acme/web/dev/ops`) and `acme/kbd` (placed nowhere).

Slice 3 adds (`seed_tools()`, test_ac_21_policy): roles `acme/web/dev/tools` and
`acme/web/dev/flaky`; services `tracker` and `outage` whose MCP upstream is the run's fake
(tests/browser/upstream.py: `/mcp` lists four tools, `/down` answers 503); provided-token
connections `acme/tracker` (on both roles) and `acme/outage` (on `flaky`, its tools unknown); an
operator token on `beta/app/dev/principal`, which does not reach `acme/web/dev/tools`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import warnings
from typing import Any

from browser_support import AXE_JS, CAPTURES, CONSOLE, GATEWAY, PROVIDER, Watched, host

SEEDED: list[bool] = []

DEV_OPS = "acme/web/dev/ops"
DEV_IDLE = "acme/web/dev/idle"
PROD_ROLE = "acme/web/prod/principal"
BETA_ROLE = "beta/app/dev/principal"
SCRATCH = "acme/web/scratch"
DEV_TOOLS = "acme/web/dev/tools"
DEV_FLAKY = "acme/web/dev/flaky"
TRACKER = "acme/tracker"
OUTAGE = "acme/outage"
MCP = os.environ.get("MCP", "http://mcp.example.test:8080")
TOOLS_SEEDED: list[str] = []


def seed() -> None:
    if SEEDED:
        return
    made = subprocess.run(
        ["docker", "exec", GATEWAY, "python", "-m", "hullgate.keys", "connections-key",
         "/data/connections_key"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert made.returncode == 0 or "already exists" in made.stderr, made.stderr
    for parent, level, name in [
        ("/", "client", "acme"),
        ("acme", "project", "web"),
        ("acme/web", "environment", "dev"),
        ("acme/web/dev", "role", "ops"),
        ("acme/web/dev", "role", "idle"),
        ("acme/web", "environment", "prod"),
        ("acme/web", "environment", "scratch"),
        ("/", "client", "beta"),
        ("beta", "project", "app"),
        ("beta/app", "environment", "dev"),
    ]:
        host("tree", "add", parent, level, name)
    host("tree", "sensitive", "acme/web/prod", "on")
    host("service", "declare", "gitea", "--server", PROVIDER, "--model", "gitea", "--internal-host")
    for app in ("app", "web"):
        host("application", "declare", app, "--service", "gitea", "--mode", "declared",
             "--client-id", f"hullgate-{app}")  # fmt: skip
    for name, prefix in (("acme/shared", "sh"), ("acme/kbd", "kb")):
        host("connection", "add", name, "--service", "gitea", "--provided", "--file", "-",
             "--prefix", prefix, stdin=f"provided-{prefix}-value\n")  # fmt: skip
    host("connection", "place", "acme/shared", DEV_OPS)
    SEEDED.append(True)


def seed_tools() -> str:
    """The slice 3 seed; returns the identifier of the token that does not reach DEV_TOOLS."""
    if TOOLS_SEEDED:
        return TOOLS_SEEDED[0]
    seed()
    for name in ("tools", "flaky"):
        host("tree", "add", "acme/web/dev", "role", name)
    for service, path in (("tracker", "/mcp"), ("outage", "/down")):
        host("service", "declare", service, "--server", PROVIDER, "--model", "gitea",
             "--internal-host")  # fmt: skip
        host("service", "upstream", service, "--type", "mcp-internal", "--mcp-url", MCP + path,
             "--mcp-internal-host")  # fmt: skip
    for name, service, prefix in ((TRACKER, "tracker", "tr"), (OUTAGE, "outage", "ou")):
        host("connection", "add", name, "--service", service, "--provided", "--file", "-",
             "--prefix", prefix, stdin=f"provided-{prefix}-value\n")  # fmt: skip
    host("connection", "place", TRACKER, DEV_TOOLS)
    host("connection", "place", TRACKER, DEV_FLAKY)
    host("connection", "place", OUTAGE, DEV_FLAKY)
    # a first listing enters the tools in the catalogue and its queue (F-015 BR-173)
    host("policy", "preview", DEV_TOOLS)
    issued = json.loads(host("token", "create", "--path", BETA_ROLE, "--label", "beta reader",
                             "--json"))  # fmt: skip
    TOOLS_SEEDED.append(str(issued["id"]))
    return TOOLS_SEEDED[0]


def change_upstream_definition(control: str = "change") -> None:
    """The fake upstream changes `create_issue`'s definition from then on (SCR-10.diff), or puts
    it back (`control="reset"`)."""
    script = (
        "import urllib.request as u;"
        f"u.urlopen(u.Request('{MCP}/control/{control}', data=b'{{}}', method='POST'), timeout=5)"
    )
    subprocess.run(["docker", "exec", GATEWAY, "python", "-c", script], check=True,
                   capture_output=True)  # fmt: skip


def status(name: str) -> dict[str, Any]:
    """`connection status <name> --json`: the server's view (BR-080)."""
    doc = json.loads(host("connection", "status", name, "--json"))
    view: dict[str, Any] = doc[0] if isinstance(doc, list) else doc
    return view


def placements(name: str) -> list[str]:
    return [p["role"] for p in status(name)["placements"]]


def audit_actions(n: int = 200) -> list[dict[str, Any]]:
    doc = json.loads(host("audit", "tail", "-n", str(n), "--json"))
    return doc if isinstance(doc, list) else doc.get("entries", [])


def paths() -> list[str]:
    doc = json.loads(host("tree", "show", "--json"))
    nodes = doc if isinstance(doc, list) else doc.get("nodes", [doc])
    out: list[str] = []

    def walk(items: list[Any]) -> None:
        for item in items:
            if isinstance(item, dict):
                if "path" in item:
                    out.append(str(item["path"]))
                for value in item.values():
                    if isinstance(value, list):
                        walk(value)

    walk(nodes)
    return out


def tree_page(w: Watched) -> None:
    """The operator on the Tree tab, the tree loaded."""
    w.page.goto(f"{CONSOLE}/admin/tree")
    w.page.locator("[role='treeitem'][data-path='/']").wait_for()


def role_row(w: Watched, path: str) -> Any:
    return w.page.locator(f"[role='treeitem'][data-path='{path}']")


def both_themes(w: Watched, state: str, *, capture_text: dict[str, str] | None = None) -> None:
    """Captures `state` dark then light (AC-21), the theme set on the document only, so that an
    open dialog stays open; the console's own switch is used in test_br_202. BR-333 example
    text replaces the selected DOM text only during each capture, after real-value assertions.
    Application state is untouched and the original DOM text is restored even on failure."""
    try:
        for theme in ("dark", "light"):
            w.page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
            w.page.wait_for_timeout(100)
            originals: list[tuple[Any, str]] = []
            try:
                for selector, example in (capture_text or {}).items():
                    element = w.page.locator(selector)
                    originals.append((element, element.inner_text()))
                    element.evaluate("(element, text) => { element.textContent = text; }", example)
                w.capture(state)
            finally:
                for element, original in originals:
                    element.evaluate("(element, text) => { element.textContent = text; }", original)
    finally:
        w.page.evaluate("document.documentElement.dataset.theme = 'dark'")


# -- keyboard (BR-232) ---------------------------------------------------------------------------

ACTIVE = "(() => { const e = document.activeElement; return e ? %s : false; })()"


def active(w: Watched, expression: str) -> Any:
    """`expression` evaluated on `e`, the focused element."""
    return w.page.evaluate(ACTIVE % expression)


def tab_to(w: Watched, expression: str, limit: int = 200, back: bool = False) -> None:
    """Presses Tab (Shift+Tab with `back`) until the focused element `e` satisfies `expression`."""
    for _ in range(limit):
        if active(w, expression):
            return
        w.page.keyboard.press("Shift+Tab" if back else "Tab")
    raise AssertionError(f"focus never reached: {expression}; on {describe(w)}")


def describe(w: Watched) -> str:
    return str(active(w, "e.outerHTML.slice(0, 160)"))


def focus_visible(w: Watched) -> bool:
    """The focused element shows its focus: `:focus-visible` matches and draws an outline."""
    return bool(
        active(
            w,
            "e.matches(':focus-visible') && getComputedStyle(e).outlineStyle !== 'none'"
            " && parseFloat(getComputedStyle(e).outlineWidth) > 0",
        )
    )


def wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("condition not reached in time")


def axe(w: Watched, screen: str) -> dict[str, Any]:
    """axe-core run through CDP (the console's CSP forbids injected and evaluated scripts)."""
    if w.cdp is None:
        w.cdp = w.context.new_cdp_session(w.page)
    if not w.page.evaluate("typeof window.axe === 'object'"):
        w.cdp.send("Runtime.evaluate", {"expression": AXE_JS.read_text()})
    result = w.cdp.send(
        "Runtime.evaluate",
        {
            "expression": "axe.run(document).then(r => JSON.stringify({violations: r.violations"
            ".map(v => ({id: v.id, impact: v.impact, help: v.help,"
            " nodes: v.nodes.map(n => n.target.join(' '))}))}))",
            "awaitPromise": True,
            "returnByValue": True,
        },
    )
    doc: dict[str, Any] = json.loads(result["result"]["value"])
    doc["screen"] = screen
    return doc


def accessibility(operator: Watched, screen: str) -> None:
    result = axe(operator, screen)
    report = CAPTURES / "axe-report.json"
    existing = json.loads(report.read_text()) if report.exists() else []
    report.write_text(json.dumps([*existing, result], indent=2))
    if result["violations"]:
        warnings.warn(f"{screen}: axe found {len(result['violations'])} violation(s)", stacklevel=2)
