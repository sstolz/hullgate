"""Composite import/undo decisions recheck sessions after preparation, preserving outcomes."""

from __future__ import annotations

import copy
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from hullgate import connections, lifecycle, upstream
from hullgate.upstream_mcp import UpstreamClient
from tests.admin_support import Console
from tests.integration.test_br_219_undo import latest
from tests.integration.test_br_222_import_preview import apply, document, encoded, preview
from tests.integration.test_hull_85_decision_recheck import _end, _refused, _snapshot
from tests.integration.test_hull_85_provider_outcomes import _after_answer, _audit, _row
from tests.policy_support import catalogue, gitea_tools, policy_rows
from tests.relay_bed import A, Bed
from tests.support import Gateway

NAME = "mpx/gd"


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
def test_import_deletion_rechecks_after_lease_wait(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    bed = Bed(gw)
    bed.connection("gd", A, oauth=True)
    provider = gw.provider("sgd")
    doc = document(gw)
    doc["connections"] = []
    text = encoded(doc)
    waits: list[float] = []

    def sleep(_s: float) -> None:
        if not waits:
            _end(gw, event)
        waits.append(gw.clock())
        gw.clock.advance(1)

    defaults = lifecycle.prepare_removal.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "sleep", sleep)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        gw.db.call(
            lambda c: c.execute(
                "UPDATE connection SET renewing_until = ?, renewal_owner = 'other'",
                (int(gw.clock() + lifecycle.LEASE_S),),
            )
        )
        plan = preview(console, client, text)
        before = _snapshot(gw)
        calls = len(provider.journal)
        response = apply(console, client, text, plan, apply_deletions=True)
    assert waits
    if event == "live":
        assert response.status_code == 200, response.text
        assert _row(gw)["removed"] == 1
        assert not policy_rows(gw)[2]
        assert _audit(gw, "import.apply")[-1]["result"] == "ok"
    else:
        _refused(response)
        assert _snapshot(gw) == before  # Includes connection, placement and audit.
        assert len(provider.journal) == calls
        assert not _audit(gw, "import.apply")


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
def test_import_rechecks_after_discovery(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    bed = Bed(gw)
    bed.connection("gd", A)
    doc = document(gw)
    doc["services"].append(
        {**copy.deepcopy(doc["services"][0]), "id": "new", "scopes": ["new_scope"]}
    )
    text = encoded(doc)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        plan = preview(console, client, text)
        before = _snapshot(gw)
        answered = _after_answer(gw, monkeypatch, connections, "prepare_service", event)
        response = apply(console, client, text, plan)
    assert answered == [True]
    if event == "live":
        assert response.status_code == 200, response.text
        assert any(
            s["id"] == "new" and "new_scope" in s["scopes"] for s in document(gw)["services"]
        )
    else:
        _refused(response)
        assert _snapshot(gw) == before


def _argument_request(gw: Gateway, route: str) -> dict[str, Any]:
    """Prepare a real argument-rule reversal or import with a distinct desired value."""
    gw.ok("policy", "arg", NAME, "--role", A, "issue_write", "method", "create")
    if route == "changes/undo":
        gw.ok("policy", "arg", NAME, "--role", A, "issue_write", "method", "update")
        return {"seq": latest(gw)["seq"]}
    doc = document(gw)
    doc["connections"][0]["placements"][0]["arguments"][0]["values"] = ["update"]
    return {"document": encoded(doc)}


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
@pytest.mark.parametrize("route", ["changes/undo", "import/apply"])
def test_argument_decision_rechecks_after_listing(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str, route: str
) -> None:
    bed = Bed(gw)
    up = bed.connection("gd", A)
    up.tools = gitea_tools()
    catalogue(gw, NAME, A)
    body = _argument_request(gw, route)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        if route == "import/apply":
            body["digest"] = preview(console, client, body["document"])["digest"]
        before = _snapshot(gw)
        answered = _after_answer(gw, monkeypatch, UpstreamClient, "list_tools", event)
        response = console.call(client, "POST", route, body)
    assert answered == [True]
    if event == "live":
        assert response.status_code == 200, response.text
        wanted = "create" if route == "changes/undo" else "update"
        assert document(gw)["connections"][0]["placements"][0]["arguments"][0]["values"] == [wanted]
    else:
        _refused(response)
        assert _snapshot(gw) == before  # Rule and every other domain table stay unchanged.


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
@pytest.mark.parametrize("route", ["changes/undo", "import/apply"])
def test_preparation_records_rotation_before_refusing_mutation(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str, route: str
) -> None:
    bed = Bed(gw)
    up = bed.connection("gd", A, oauth=True)
    up.tools = gitea_tools()
    provider = gw.provider("sgd")
    provider.rotate = True
    catalogue(gw, NAME, A)
    body = _argument_request(gw, route)
    gw.clock.advance(3600)
    before = _row(gw)
    policies = policy_rows(gw)[:-1]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        if route == "import/apply":
            body["digest"] = preview(console, client, body["document"])["digest"]
        n0 = len(gw.audit())
        answered = _after_answer(gw, monkeypatch, upstream, "refresh", event)
        response = console.call(client, "POST", route, body)
    assert answered == [True]
    assert _row(gw) == {**before, "generation": before["generation"] + 1}
    assert [e["result"] for e in _audit(gw, "connection.renew")] == ["ok"]
    assert sum(e["live"] for e in provider.refresh_tokens.values()) == 1
    if event == "live":
        assert response.status_code == 200, response.text
        assert policy_rows(gw)[:-1] != policies
    else:
        _refused(response)
        assert policy_rows(gw)[:-1] == policies
        assert [e["action"] for e in gw.audit()[n0:]] == ["connection.renew"]
    monkeypatch.undo()
    # The saved rotated refresh token really works on the next exchange.
    assert gw.cli("connection", "renew", NAME).code == 0
    assert _row(gw)["generation"] == before["generation"] + 2


def test_uncertain_outcome_is_recorded_after_session_ends(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    bed = Bed(gw)
    up = bed.connection("gd", A, oauth=True)
    up.tools = gitea_tools()
    catalogue(gw, NAME, A)
    body = _argument_request(gw, "changes/undo")
    gw.clock.advance(3600)
    monotonic = [0.0]
    monkeypatch.setattr(lifecycle, "time", SimpleNamespace(monotonic=lambda: monotonic[0]))
    original: Callable[..., Any] = upstream.refresh

    def answer(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        _end(gw, "signout")
        # No write was possible during the lease. Exercise _record_uncertain, not _finish.
        monotonic[0] += lifecycle.LEASE_S + 1
        return result

    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = _row(gw)
        policies = policy_rows(gw)[:-1]
        monkeypatch.setattr(upstream, "refresh", answer)
        response = console.call(client, "POST", "changes/undo", body)
    _refused(response)
    assert _row(gw) == {**before, "fail_count": before["fail_count"] + 1}
    assert policy_rows(gw)[:-1] == policies
    assert [e["result"] for e in _audit(gw, "connection.renew")] == ["uncertain"]
    assert _audit(gw, "connection.upstream_revoke")[-1]["result"] == "ok"
    row = gw.row(NAME)
    assert row["last_outcome"] == "uncertain"
    assert row["last_reason"] == "lease_write_failed"
    assert row["not_before"] > gw.clock()
    assert not _audit(gw, "change.undo")
