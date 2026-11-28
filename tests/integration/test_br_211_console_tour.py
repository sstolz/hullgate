"""BR-211 (AC-10) — the console's tour: every route of slice 2, played once on the fake provider
and the fake upstream, answers as the CLI verb it maps; the tour asserts that each route it
plays is a declared route and that every declared console route is played.
"""

from __future__ import annotations

from typing import Any

from tests import fake_upstream
from tests.admin_support import Console
from tests.policy_support import gitea_tools
from tests.support import Gateway, org, standard_tree

ROLE = "mpx/cairn/staging/dev"


def test_br_211_every_console_route_is_played(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.provider()
    gw.upnet.upstream("gd.mcp.test", "modern").tools = gitea_tools()
    console = Console(gw)
    played: set[str] = set()

    def call(method: str, route: str, body: dict[str, Any] | None = None, **query: str) -> Any:
        played.add(f"{method} {route}")
        if method == "GET":
            response = client.get(f"/admin/api/v1/{route}", params=query, headers=console.headers())
        else:
            response = console.call(client, method, route, body or {})
        assert response.status_code in (200, 201), (method, route, response.text)
        return (
            response.json()
            if response.headers["content-type"].startswith("application/json")
            else response.text
        )

    def impact(act: str, **query: str) -> str:
        return str(call("GET", "impacts", None, act=act, **query)["digest"])

    with console.client() as client:
        console.admin(client)
        start = max(e["seq"] for e in call("GET", "audit", limit="500")["entries"])
        assert call("GET", "organisation")["connection_expiry_days"] == 7
        assert len(call("GET", "setup")["steps"]) == 5
        # tree and settings
        created = call(
            "POST", "nodes", {"parent": "mpx/cairn", "level": "environment", "name": "QA"}
        )
        assert [n["path"] for n in created["created"]] == ["mpx/cairn/qa", "mpx/cairn/qa/principal"]
        assert created["created"][1]["url"].endswith(f"/o/{org()}/p/mpx/cairn/qa/principal/mcp")
        nodes = call("GET", "nodes")["nodes"]
        assert nodes[0]["path"] == "/"
        assert {n["path"] for n in nodes} >= {ROLE, "mpx/cairn/qa"}
        call("POST", "nodes/rename", {"path": "mpx/cairn/qa", "name": "Quality",
             "impact": impact("tree.rename", path="mpx/cairn/qa")})  # fmt: skip
        call("POST", "nodes/sensitive", {"path": "mpx/cairn/qa", "sensitive": True,
             "impact": impact("tree.sensitive", path="mpx/cairn/qa",
                              sensitive="true")})  # fmt: skip
        locked = call("POST", "nodes/lock", {"path": "mpx/cairn/qa"})
        assert locked == {"path": "mpx/cairn/qa", "locked_by": "mpx/cairn/qa"}
        assert call("POST", "nodes/unlock", {"path": "mpx/cairn/qa"})["locked_by"] is None
        gw.ok("tree", "add", "mpx", "project", "Spare")
        call("POST", "nodes/move", {"path": "mpx/cairn/qa", "parent": "mpx/spare",
             "impact": impact("tree.move", path="mpx/cairn/qa", parent="mpx/spare")})  # fmt: skip
        call("POST", "nodes/delete", {"path": "mpx/spare/qa",
             "impact": impact("tree.delete", path="mpx/spare/qa")})  # fmt: skip
        assert call("PUT", "settings", {"path": "mpx", "key": "token_duration", "value": "7d"}) == {
            "key": "token_duration",
            "value": "7d",
            "origin": "mpx",
            "tools_notice": True,
        }
        shown = call("GET", "settings", path=ROLE)["settings"]
        assert {"key": "token_duration", "value": "7d", "origin": "mpx", "here": False} in shown
        unset = call("POST", "settings/unset", {"path": "mpx", "key": "token_duration"})
        assert unset["origin"] == "/"
        # tokens and clients
        issued = call("POST", "tokens", {"path": ROLE, "label": "agent", "duration": "2d"})
        tokens = call("GET", "tokens", path="mpx")["tokens"]
        assert [t["id"] for t in tokens] == [issued["id"]]
        assert call("POST", "tokens/revoke", {"id": issued["id"]})["already"] is False
        call("POST", "clients", {"client_id": "claude", "path": "mpx",
                                 "redirect_uris": ["https://claude.example.test/cb"]})  # fmt: skip
        assert call("PUT", "clients/dcr", {"on": True}) == {"dcr": True}
        assert call("GET", "clients")["dcr"] is True
        call("POST", "clients/remove", {"client_id": "claude",
             "impact": impact("client.remove", client_id="claude")})  # fmt: skip
        # services, applications
        declared = call("POST", "services", {"id": "gitea-dev", "server":
                        "https://gitea-dev.example.test", "model": "gitea",
                        "scopes": "write:issue"})  # fmt: skip
        assert declared["service"]["id"] == "gitea-dev"
        call("POST", "services/rediscover", {"id": "gitea-dev", "confirm": "gitea-dev"})
        call("POST", "services/upstream", {"id": "gitea-dev", "type": "mcp-public",
                                           "mcp_url": "https://gd.mcp.test/mcp"})  # fmt: skip
        assert [s["id"] for s in call("GET", "services")["services"]] == ["gitea-dev"]
        call("POST", "applications", {"id": "dyn", "service": "gitea-dev", "mode": "dynamic"})
        call("POST", "applications/reregister", {"id": "dyn", "confirm": "dyn",
             "impact": impact("application.reregister", id="dyn")})  # fmt: skip
        call("POST", "applications", {"id": "app", "service": "gitea-dev", "mode": "declared",
                                      "client_id": "client-app"})  # fmt: skip
        assert call("PUT", "applications/web-return", {"id": "app", "registered": True})[
            "web_return"
        ]
        apps = call("GET", "applications")
        assert {a["id"] for a in apps["applications"]} == {"app", "dyn"}
        call("POST", "applications/remove", {"id": "dyn",
             "impact": impact("application.remove", id="dyn")})  # fmt: skip
        # connections and placements
        added = call("POST", "connections", {"name": "mpx/gitea-dev", "service": "gitea-dev",
                                             "application": "app", "prefix": "gd"})  # fmt: skip
        from urllib.parse import parse_qs, urlsplit

        back = parse_qs(urlsplit(provider.approve(added["authorization_url"])).query)
        call("POST", "connections/callback", {"state": back["state"][0], "code": back["code"][0]})
        call("PUT", "connections/prefix", {"name": "mpx/gitea-dev", "prefix": "gt"})
        call("PUT", "connections/prefix", {"name": "mpx/gitea-dev", "prefix": "gd"})
        call("POST", "placements", {"connection": "mpx/gitea-dev", "role": ROLE})
        listed = call("POST", "connections/tools", {"name": "mpx/gitea-dev", "role": ROLE})
        assert listed["connection"] == "mpx/gitea-dev"
        assert listed["tools"]
        # policy surfaces (slice 3): preview, placement policy, catalogue
        here = {"connection": "mpx/gitea-dev", "role": ROLE}
        assert "gd__list_issues" in call("POST", "rights", {"role": ROLE})["exposed"]
        assert call("GET", "placements/policy", None, path=ROLE)["placements"][0]["role"] == ROLE
        call("PUT", "placements/mode", {**here, "mode": "write"})
        call("POST", "placements/lists", {**here, "list": "deny", "tools": ["delete_file"]})
        call("POST", "placements/lists/remove", {**here, "list": "deny", "tools": ["delete_file"]})
        call("PUT", "placements/arguments", {**here, "tool": "issue_write", "argument": "method",
                                             "values": ["create"]})  # fmt: skip
        call("POST", "placements/arguments/remove", {**here, "tool": "issue_write"})
        call("POST", "placements/tool-pins", {**here, "tool": "list_issues"})
        call("POST", "placements/tool-pins/remove", {**here, "tool": "list_issues"})
        call("PUT", "placements/pin", {**here, "pin": "on"})
        queued = call("GET", "tools/queue", None, service="gitea-dev")["queue"]
        assert {q["name"] for q in queued} >= {"issue_write", "pull_request_write"}
        call("POST", "tools/accept", {"service": "gitea-dev", "tool": "issue_write"})
        call("POST", "tools/classify", {"service": "gitea-dev", "tool": "pull_request_write",
                                        "class": "write"})  # fmt: skip
        assert any(
            tool["name"] == "pull_request_write"
            for tool in call("GET", "tools/catalogue", None, service="gitea-dev")["catalogue"]
        )
        call("POST", "connections/tools", {"name": "mpx/gitea-dev", "role": ROLE})
        waiting = call("POST", "tools/diff", {"path": ROLE})["changes"]
        call("POST", "tools/validate", {"path": ROLE, "connection": "mpx/gitea-dev"})
        assert len(waiting) > len(call("POST", "tools/diff", {"path": ROLE})["changes"])
        renewed = call("POST", "connections/renew", {"name": "mpx/gitea-dev"})
        assert renewed["generation"] == 2
        reopened = call("POST", "connections/reauthorize", {"name": "mpx/gitea-dev"})
        assert reopened["redirect"] == "web"
        pasted = provider.approve(reopened["authorization_url"])
        call("POST", "connections/authorize", {"name": "mpx/gitea-dev", "callback": pasted})
        views = call("GET", "connections")["connections"]
        assert [(v["name"], v["placements"]) for v in views] == [
            ("mpx/gitea-dev", [{"role": ROLE, "sensitive": False}])
        ]
        call("POST", "placements/remove", {"connection": "mpx/gitea-dev", "role": ROLE})
        call("POST", "connections/revoke", {"name": "mpx/gitea-dev",
             "impact": impact("connection.revoke", name="mpx/gitea-dev")})  # fmt: skip
        call("POST", "connections/remove", {"name": "mpx/gitea-dev",
             "impact": impact("connection.remove", name="mpx/gitea-dev")})  # fmt: skip
        call("POST", "applications/remove", {"id": "app",
             "impact": impact("application.remove", id="app")})  # fmt: skip
        call(
            "POST", "services/upstream", {"id": "gitea-dev", "remove": True, "confirm": "gitea-dev"}
        )
        call("POST", "services/remove", {"id": "gitea-dev",
             "impact": impact("service.remove", id="gitea-dev")})  # fmt: skip
        # Slice 5: shared history and import operations.
        call("PUT", "settings", {"path": ROLE, "key": "token_duration", "value": "5d"})
        history = call("GET", "audit", action="settings.set")["entries"][0]
        call("GET", "changes", seq=str(history["seq"]))
        call("POST", "changes/undo", {"seq": history["seq"]})
        document = call("GET", "export")
        plan = call("POST", "import/preview", {"document": document})
        assert plan["changes"] == []
        call("POST", "import/apply", {"document": document, "digest": plan["digest"]})
        # audit: every entry written since sign-in is the administrator's (AC-10), but the one
        # CLI call of the tour and the renewal, the gateway's act whoever forces it (BR-089)
        page = call("GET", "audit", limit="500")
        assert page["next"] is None
        entries = [e for e in page["entries"] if e["seq"] > start]
        mine = [e for e in entries if e["actor"].startswith("admin:")]
        others = sorted((e["action"], e["actor"]) for e in entries if e not in mine)
        assert others == [("connection.renew", "gateway"), ("tree.add", "operator-cli")]
        assert len({e["actor"] for e in mine}) == 1
        assert len(mine) >= len([p for p in played if not p.startswith("GET ")])
        assert call("POST", "audit/export", {"action": "tree."}).count("\n") >= 5
        # Guide dismissal is the explicitly permitted presentation-state-only write.
        assert call("POST", "setup/dismiss") == {"dismissed": True}
    lot_a = ("auth/", "enrol/", "session", "passkeys", "allowlist", "meta")
    declared_routes = {
        f"{s.method} {s.path}"
        for s in console.app.api_routes
        if not any(s.path.startswith(p) for p in lot_a)
    }
    assert played == declared_routes, declared_routes ^ played
    assert fake_upstream  # the upstream of `connections/tools` is the shared fake
