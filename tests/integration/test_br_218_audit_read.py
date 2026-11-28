"""BR-218, BR-066 v5 (AC-17) — the audit screen reads and exports; it writes nothing.

Filters on time, actor, path prefix, action prefix and result hold; paging by `seq` covers every
entry once; an export returns JSON Lines, at most 100 000 entries, and is itself audited with its
filter. No route writes the audit other than as the trace of an act.
"""

from __future__ import annotations

import json

from hullgate.admin.console import EXPORT_MAX
from tests.admin_support import Console, counts
from tests.support import Gateway, standard_tree


def _entries(client, console: Console, **query: str) -> list[dict]:  # type: ignore[no-untyped-def,type-arg]
    response = client.get("/admin/api/v1/audit", params=query, headers=console.headers())
    assert response.status_code == 200, response.text
    return list(response.json()["entries"])


def test_br_218_filters_hold(gw: Gateway) -> None:
    standard_tree(gw)
    gw.clock.advance(3600)
    gw.ok("settings", "set", "mpx/cairn", "token_duration", "7d")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        everything = _entries(client, console, limit="500")
        assert [e["seq"] for e in everything] == sorted(
            (e["seq"] for e in everything), reverse=True
        )
        by_action = _entries(client, console, action="tree.")
        assert by_action
        assert all(e["action"].startswith("tree.") for e in by_action)
        by_path = _entries(client, console, path="mpx/cairn/prod")
        assert by_path
        assert all(e["path"].startswith("mpx/cairn/prod") for e in by_path)
        by_actor = _entries(client, console, actor="operator-cli")
        assert by_actor
        assert all(e["actor"] == "operator-cli" for e in by_actor)
        admins = _entries(client, console, action="admin.sign_in")
        assert admins
        assert all(e["actor"].startswith("admin:") for e in admins)
        ok = _entries(client, console, result="ok")
        assert ok
        assert all(e["result"] == "ok" for e in ok)
        later = everything[0]["at"]
        recent = _entries(client, console, since=later)
        assert recent
        assert all(e["at"] >= later for e in recent)
        early = _entries(client, console, until=everything[-1]["at"])
        assert early
        assert all(e["at"] <= everything[-1]["at"] for e in early)
        literal = _entries(client, console, action="tree_")  # `_` is not a wildcard
        assert literal == []
        bad = client.get("/admin/api/v1/audit", params={"since": "yesterday"},
                         headers=console.headers())  # fmt: skip
        assert bad.status_code == 422


def test_br_218_paging_by_seq_covers_every_entry_once(gw: Gateway) -> None:
    standard_tree(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        everything = _entries(client, console, limit="500")
        seen: list[int] = []
        before = ""
        while True:
            query = {"limit": "3", **({"before": before} if before else {})}
            page = client.get("/admin/api/v1/audit", params=query, headers=console.headers()).json()
            seen += [e["seq"] for e in page["entries"]]
            if page["next"] is None:
                break
            before = str(page["next"])
    assert seen == [e["seq"] for e in everything]


def test_br_218_export_is_json_lines_and_audited(gw: Gateway) -> None:
    standard_tree(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        response = console.call(client, "POST", "audit/export", {"action": "tree."})
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert response.headers["cache-control"] == "no-store"
        lines = [json.loads(line) for line in response.text.splitlines()]
        assert lines
        assert all(e["action"].startswith("tree.") for e in lines)
    [trace] = [e for e in gw.audit() if e["action"] == "audit.export"]
    assert trace["actor"].startswith("admin:")
    detail = json.loads(trace["detail"])
    assert detail == {"filter": {"action": "tree."}, "entries": len(lines)}
    assert EXPORT_MAX == 100_000


def test_br_218_reading_writes_nothing(gw: Gateway) -> None:
    standard_tree(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = counts(gw)
        _entries(client, console, action="tree.")
        _entries(client, console, limit="1")
        assert counts(gw) == before
