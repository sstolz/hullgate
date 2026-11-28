"""Review 605 regressions: renewals survive refusals, preparation never holds the store."""

from __future__ import annotations

import copy
import sqlite3
import threading
import time
from typing import Any

import pytest

from tests.admin_support import Console
from tests.fake_upstream import tool
from tests.integration.test_br_222_import_preview import apply, document, encoded, preview
from tests.policy_support import catalogue, gitea, gitea_tools, method_schema, rows
from tests.relay_bed import A, Bed
from tests.support import Gateway


def latest(gw: Gateway, action: str) -> dict[str, Any]:
    return next(e for e in reversed(gw.audit()) if e["action"] == action)


def oauth_bed(gw: Gateway) -> Any:
    bed = Bed(gw)
    up = bed.connection("ab", A, oauth=True)
    up.tools = gitea_tools()
    catalogue(gw, "mpx/ab", A)
    return up


def without_create() -> list[dict[str, Any]]:
    return [
        t
        if t["name"] != "issue_write"
        else tool("issue_write", method_schema("update"), {"readOnlyHint": False})
        for t in gitea_tools()
    ]


def next_access(gw: Gateway) -> str:
    gw.expire("mpx/ab", 10)
    try:
        return type(gw.access("mpx/ab", A)).__name__
    except Exception as exc:
        return type(exc).__name__


def test_b1_control_direct_act_refused_after_renewal(gw: Gateway) -> None:
    up = oauth_bed(gw)
    up.tools = without_create()
    gw.expire("mpx/ab", 10)
    assert (
        gw.cli("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create").code == 2
    )
    assert next_access(gw) == "Access"
    assert rows(gw, "SELECT state FROM connection") == [("active",)]


@pytest.mark.parametrize("unplace", [False, True])
def test_b1_undo_refused_after_renewal_keeps_the_connection_active(
    gw: Gateway,
    unplace: bool,
) -> None:
    up = oauth_bed(gw)
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "update")
    seq = latest(gw, "policy.argument_set")["seq"]
    if unplace:
        gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
        gw.ok("connection", "unplace", "mpx/ab", A)
        seq = latest(gw, "connection.unplace")["seq"]
    up.tools = without_create()
    gw.expire("mpx/ab", 10)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        assert console.call(client, "POST", "changes/undo", {"seq": seq}).status_code == 409
    if unplace:
        gw.ok("connection", "place", "mpx/ab", A)
    assert next_access(gw) == "Access"
    assert rows(gw, "SELECT state FROM connection") == [("active",)]


def test_b1_import_rolled_back_after_renewal_keeps_the_connection_active(gw: Gateway) -> None:
    oauth_bed(gw)
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
    doc = document(gw)
    doc["connections"][0]["placements"][0]["arguments"][0]["values"] = ["update"]
    doc["organisation"]["settings"]["lock"] = "invalid"  # refused last (order 30)
    text = encoded(doc)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        gw.expire("mpx/ab", 10)
        plan = preview(console, client, text)
        refused = apply(console, client, text, plan)
        assert refused.status_code == 409
        assert refused.json()["location"] == "/:lock"
    assert next_access(gw) == "Access"
    assert rows(gw, "SELECT state FROM connection") == [("active",)]


def test_b1_lease_wait_allows_a_concurrent_writer(gw: Gateway) -> None:
    oauth_bed(gw)
    doc = document(gw)
    doc["connections"] = []
    text = encoded(doc)
    with gw.connect() as c:
        path = c.execute("PRAGMA database_list").fetchone()[2]
    seen: list[str] = []

    def writer() -> None:
        time.sleep(0.8)
        c = sqlite3.connect(path, timeout=1.0, isolation_level=None)
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("ROLLBACK")
            seen.append("ok")
        except sqlite3.OperationalError as exc:
            seen.append(str(exc))
        finally:
            c.close()

    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        with gw.connect() as c:
            c.execute(
                "UPDATE connection SET renewing_until = ?, renewal_owner = 'other'",
                (int(gw.clock()) + 3,),
            )
            c.commit()
        plan = preview(console, client, text)
        thread = threading.Thread(target=writer)
        thread.start()
        started = time.monotonic()
        assert apply(console, client, text, plan, apply_deletions=True).status_code == 200
        elapsed = time.monotonic() - started
        thread.join()
    assert elapsed > 3.0  # the frozen test clock exercises the dead-owner deadline
    assert seen == ["ok"]


def test_b2_service_change_preserves_catalogue_and_upstream_and_converges(gw: Gateway) -> None:
    bed = Bed(gw)
    gitea(bed, "ab", A)
    catalogue(gw, "mpx/ab", A)
    gw.ok("tool", "classify", "sab", "list_issues", "destructive")
    gw.ok("connection", "remove", "mpx/ab", "--confirm", "mpx/ab")
    assert rows(gw, "SELECT count(*), count(manual_class) FROM tool_catalog") == [(6, 1)]
    upstream = rows(gw, "SELECT upstream_type, mcp_url, mcp_internal FROM service")
    catalogue_before = rows(gw, "SELECT * FROM tool_catalog ORDER BY upstream_name")
    doc = document(gw)
    doc["services"][0]["scopes"] = ["read:issue"]
    text = encoded(doc)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        plan = preview(console, client, text)
        assert [(c["kind"], c["action"], c["impact"]) for c in plan["changes"]] == [
            ("service", "change", {})
        ]
        assert apply(console, client, text, plan).status_code == 200  # apply_deletions off
        assert rows(gw, "SELECT * FROM tool_catalog ORDER BY upstream_name") == catalogue_before
        assert rows(gw, "SELECT upstream_type, mcp_url, mcp_internal FROM service") == upstream
        assert preview(console, client, text)["changes"] == []


@pytest.mark.parametrize("name", ["a\ufffeb", "a\uffffb", "Orbit \U0001f680", "Caf\u00e9"])
def test_n1_accepted_name_round_trips_in_preview(gw: Gateway, name: str) -> None:
    gw.ok("tree", "add", "/", "client", name, "--slug", "odd")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        result = console.call(client, "POST", "import/preview", {"document": gw.ok("export")})
        assert result.status_code == 200
        assert result.json()["changes"] == []


def test_n3_imported_environment_without_principal_converges(gw: Gateway) -> None:
    Bed(gw)
    doc = document(gw)
    env = copy.deepcopy(doc["organisation"]["clients"][0]["projects"][0]["environments"][0])
    env.update(segment="imported", name="Imported", roles=[])
    doc["organisation"]["clients"][0]["projects"][0]["environments"].append(env)
    text = encoded(doc)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        plan = preview(console, client, text)
        assert apply(console, client, text, plan).status_code == 200
        assert preview(console, client, text)["changes"] == []


def assert_store_available(gw: Gateway) -> None:
    """A process database call can acquire SQLite's write lock during external preparation."""
    result: list[str] = []

    def writer() -> None:
        def check(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")

        try:
            gw.db.call(check)
            result.append("ok")
        except Exception as exc:
            result.append(type(exc).__name__)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert result == ["ok"]


@pytest.mark.parametrize("route", ["preview", "apply"])
def test_n2_parse_once_outside_database_locks(
    gw: Gateway,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    from hullgate import importing

    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        text = gw.ok("export")
        plan = preview(console, client, text)
        original = importing.parse
        seen: list[bool] = []

        def parse(value: str) -> dict[str, Any]:
            assert_store_available(gw)
            seen.append(True)
            return original(value)

        monkeypatch.setattr(importing, "parse", parse)
        response = console.call(
            client, "POST", f"import/{route}", {"document": text, "digest": plan["digest"]}
        )
        assert response.status_code == 200, response.text
        assert seen == [True]


@pytest.mark.parametrize("operation", ["undo", "import"])
def test_b1_provider_preparation_releases_database_locks(
    gw: Gateway,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from hullgate import upstream
    from hullgate.upstream_mcp import UpstreamClient

    oauth_bed(gw)
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "update")
    seq = latest(gw, "policy.argument_set")["seq"]
    doc = document(gw)
    doc["connections"][0]["placements"][0]["arguments"][0]["values"] = ["create"]
    text = encoded(doc)
    original_refresh, original_list = upstream.refresh, UpstreamClient.list_tools
    calls: list[str] = []

    def refresh(*args: Any, **kwargs: Any) -> Any:
        assert_store_available(gw)
        calls.append("refresh")
        return original_refresh(*args, **kwargs)

    def listing(*args: Any, **kwargs: Any) -> Any:
        assert_store_available(gw)
        calls.append("list")
        return original_list(*args, **kwargs)

    monkeypatch.setattr(upstream, "refresh", refresh)
    monkeypatch.setattr(UpstreamClient, "list_tools", listing)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        gw.expire("mpx/ab", 10)
        if operation == "undo":
            response = console.call(client, "POST", "changes/undo", {"seq": seq})
        else:
            response = apply(console, client, text, preview(console, client, text))
        assert response.status_code == 200, response.text
    assert calls == ["refresh", "list"]


def test_b1_deferred_removal_checkpoints_only_after_commit(
    gw: Gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hullgate.admin import history
    from hullgate.db import connect as original

    oauth_bed(gw)
    doc = document(gw)
    doc["connections"] = []
    checkpoints: list[bool] = []

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = original(*args, **kwargs)

        def trace(sql: str) -> None:
            if sql == "PRAGMA wal_checkpoint(TRUNCATE)":
                checkpoints.append(not conn.in_transaction)

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(history, "connect", connect)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        doc["organisation"]["settings"]["lock"] = "invalid"
        text = encoded(doc)
        response = apply(
            console, client, text, preview(console, client, text), apply_deletions=True
        )
        assert response.status_code == 409
        assert checkpoints == []
        doc["organisation"]["settings"]["lock"] = "false"
        text = encoded(doc)
        response = apply(
            console, client, text, preview(console, client, text), apply_deletions=True
        )
        assert response.status_code == 200
        assert checkpoints == [True]


@pytest.mark.parametrize("operation", ["undo", "import"])
@pytest.mark.parametrize("phase", ["refresh", "listing"])
def test_b1_racing_writer_refuses_composite_but_keeps_renewal(
    gw: Gateway,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    phase: str,
) -> None:
    from hullgate import upstream
    from hullgate.upstream_mcp import UpstreamClient

    oauth_bed(gw)
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "update")
    seq = latest(gw, "policy.argument_set")["seq"]
    doc = document(gw)
    doc["connections"][0]["placements"][0]["arguments"][0]["values"] = ["create"]
    text = encoded(doc)
    original = upstream.refresh if phase == "refresh" else UpstreamClient.list_tools

    def race(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        gw.ok("settings", "set", A, "token_duration", "7d")
        return result

    if phase == "refresh":
        monkeypatch.setattr(upstream, "refresh", race)
    else:
        monkeypatch.setattr(UpstreamClient, "list_tools", race)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        gw.expire("mpx/ab", 10)
        before = rows(gw, "SELECT allowed_values FROM argument_rule")
        if operation == "undo":
            response = console.call(client, "POST", "changes/undo", {"seq": seq})
        else:
            response = apply(console, client, text, preview(console, client, text))
        assert response.status_code == 409, response.text
        assert response.json()["code"] == ("undo_stale" if operation == "undo" else "import_stale")
        assert rows(gw, "SELECT allowed_values FROM argument_rule") == before
        if operation == "undo":
            assert "current" in response.json()
            assert "expected" in response.json()
    monkeypatch.undo()
    assert next_access(gw) == "Access"
    assert rows(gw, "SELECT state FROM connection") == [("active",)]


def test_b2_discovery_runs_without_store_lock(gw: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    from hullgate import connections

    bed = Bed(gw)
    gitea(bed, "ab", A)
    gw.ok("connection", "remove", "mpx/ab", "--confirm", "mpx/ab")
    doc = document(gw)
    doc["services"][0]["scopes"] = ["read:issue"]
    text = encoded(doc)
    original = connections._discover
    calls: list[bool] = []

    def discover(*args: Any, **kwargs: Any) -> Any:
        assert_store_available(gw)
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(connections, "_discover", discover)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        response = apply(console, client, text, preview(console, client, text))
        assert response.status_code == 200, response.text
    assert calls == [True]


@pytest.mark.parametrize("operation", ["undo", "import"])
def test_b1_prepared_listing_keeps_ordinary_pinning_checks(gw: Gateway, operation: str) -> None:
    up = oauth_bed(gw)
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "update")
    seq = latest(gw, "policy.argument_set")["seq"]
    doc = document(gw)
    doc["connections"][0]["placements"][0]["arguments"][0]["values"] = ["create"]
    text = encoded(doc)
    up.tools = [
        {**t, "description": "Changed upstream definition"} if t["name"] == "issue_write" else t
        for t in gitea_tools()
    ]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        if operation == "undo":
            response = console.call(client, "POST", "changes/undo", {"seq": seq})
        else:
            response = apply(console, client, text, preview(console, client, text))
        assert response.status_code == 200, response.text
    assert rows(
        gw,
        "SELECT suspended_at IS NOT NULL FROM pinned_definition"
        " WHERE upstream_name = 'issue_write'",
    ) == [(1,)]


def test_b1_preparation_allows_a_planned_unlock_before_argument_change(gw: Gateway) -> None:
    oauth_bed(gw)
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
    gw.ok("tree", "lock", A)
    doc = document(gw)
    doc["connections"][0]["placements"][0]["arguments"][0]["values"] = ["update"]
    project = doc["organisation"]["clients"][0]["projects"][0]
    role = next(
        r
        for e in project["environments"]
        for r in e["roles"]
        if r["segment"] == "dev" and r["settings"].get("lock") == "true"
    )
    del role["settings"]["lock"]
    text = encoded(doc)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        response = apply(console, client, text, preview(console, client, text))
        assert response.status_code == 200, response.text
        assert preview(console, client, text)["changes"] == []
