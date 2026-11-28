"""BR-214 (AC-13) — an irreversible write quotes the impact it was shown.

For each act of the population, a stale or missing digest gets 409 `impact_changed` with the
current impact and changes nothing (row count on every table); the digest of the matching read
lets the act through. The sweep classifies every write route by what its operation does, not by
its name: in the population (the table), exempted with its reason, or not destructive with what
it changes; a route added later fails the sweep until it is classified. For the acts that wait
for a renewal lease (connection revoke and remove), an impact that changes while the act waits
is refused too: the digest is compared in the transaction that erases.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from hullgate import lifecycle
from hullgate.admin.console import IMPACT_ACTS
from tests.admin_support import Console, counts
from tests.support import Gateway, standard_tree

# act -> (write route, impact query, write body)
TABLE: dict[str, tuple[str, dict[str, str], dict[str, Any]]] = {
    "tree.delete": ("nodes/delete", {"path": "mpx/cairn/staging"}, {"path": "mpx/cairn/staging"}),
    "tree.rename": (
        "nodes/rename",
        {"path": "mpx/cairn/staging", "segment": "stage"},
        {"path": "mpx/cairn/staging", "name": "Stage", "segment": "stage"},
    ),
    "tree.move": (
        "nodes/move",
        {"path": "mpx/cairn/staging", "parent": "mpx/spare"},
        {"path": "mpx/cairn/staging", "parent": "mpx/spare"},
    ),
    "tree.sensitive": (
        "nodes/sensitive",
        {"path": "mpx/cairn/staging", "sensitive": "true"},
        {"path": "mpx/cairn/staging", "sensitive": True},
    ),
    "connection.revoke": (
        "connections/revoke",
        {"name": "mpx/gitea-dev"},
        {"name": "mpx/gitea-dev"},
    ),
    "connection.remove": (
        "connections/remove",
        {"name": "mpx/gitea-dev"},
        {"name": "mpx/gitea-dev"},
    ),
    "client.remove": ("clients/remove", {"client_id": "claude"}, {"client_id": "claude"}),
    "application.remove": ("applications/remove", {"id": "spare"}, {"id": "spare"}),
    "service.remove": ("services/remove", {"id": "spare-svc"}, {"id": "spare-svc"}),
    "application.reregister": (
        "applications/reregister",
        {"id": "dyn"},
        {"id": "dyn", "confirm": "dyn"},
    ),
}
# Writes that delete or revoke but are not BR-214's population, with the reason (F-016 §9).
EXEMPT = {
    "POST tokens/revoke": "one token, revoked in one click (SCR-09); its impact is itself",
    "POST placements/remove": "undoable (BR-220), placed again by one act",
    "POST settings/unset": "undoable (BR-220), back to the inherited value",
    "POST services/upstream": "its removal asks the CLI's --confirm, carried as `confirm`",
    "DELETE passkeys/{credential_id}": "slice 1: one's own passkey, a security act under a fresh "
    "passkey (BR-210), the last one refused",
    "POST placements/lists/remove": "slice 3: takes tools out of a placement's list, a policy "
    "change undoable (BR-220), no token, placement or path",
    "POST placements/arguments/remove": "slice 3: removes an argument rule, a policy change "
    "undoable (BR-220)",
    "POST placements/tool-pins/remove": "slice 3: lifts a tool pin, baseline and suspension kept "
    "(BR-181), undoable (BR-220)",
    "POST tools/validate": "slice 3: accepts the current definition as baseline, and drops the "
    "baseline of a tool gone upstream (BR-182); no token, placement or path",
    "POST connections/tools": "`purge_names` releases the name reservations of tools the "
    "upstream no longer lists (BR-110), no token, placement or path; it asks the CLI's --confirm",
}
# Composite acts compare their own server impact inside the writing transaction. Their
# missing/stale digest gates are exercised in test_br_221_undo_stale and test_br_222_import_preview.
COMPOSITE = {
    "POST changes/undo": "BR-219: sensitivity undo compares the current BR-214 impact digest",
    "POST import/apply": "BR-222: the digest binds the document, state and every item impact",
}
# Writes that neither delete, revoke nor change a path or a sensitivity, with what they change.
NOT_DESTRUCTIVE = {
    "POST import/preview": "BR-222: a read snapshot, no domain or audit writes",
    "POST auth/options": "slice 1: opens a sign-in ceremony",
    "POST auth/assert": "slice 1: signs in",
    "POST enrol/options": "slice 1: opens an enrolment ceremony",
    "POST enrol/verify": "slice 1: enrols a passkey",
    "POST session/signout": "slice 1: ends one's own session",
    "POST session/fresh/options": "slice 1: opens a fresh-authentication ceremony",
    "POST session/fresh": "slice 1: refreshes the session (BR-210)",
    "POST passkeys/options": "slice 1: opens a passkey addition",
    "POST passkeys": "slice 1: adds a passkey",
    "PUT allowlist": "slice 1: sets the address allow-list, undoable, under a fresh passkey",
    "PUT settings": "sets a value, undoable (BR-220)",
    "POST nodes": "adds nodes",
    "POST nodes/lock": "locks a node, undone by unlock",
    "POST nodes/unlock": "unlocks a node",
    "POST tokens": "issues a token",
    "POST clients": "registers a client",
    "PUT clients/dcr": "switches dynamic registration, undoable",
    "POST services": "declares a service",
    "POST services/rediscover": "replaces the discovered endpoints, connections kept (BR-097)",
    "POST applications": "declares an application",
    "PUT applications/web-return": "records whether the web return URI is registered",
    "POST connections": "adds a connection",
    "POST connections/reauthorize": "opens an authorisation, or replaces a provided token",
    "POST connections/authorize": "completes an authorisation (paste path)",
    "POST connections/callback": "completes an authorisation (web return, BR-216)",
    "POST connections/renew": "renews the tokens",
    "PUT connections/prefix": "changes the prefix, undoable (BR-076)",
    "POST placements": "places a connection, undone by placements/remove",
    "POST audit/export": "exports the audit, audited (BR-218)",
    "POST rights": "slice 3: the rights preview; its fresh listing records the catalogue (BR-217)",
    "PUT placements/mode": "slice 3: sets a placement's mode, undoable (BR-220)",
    "POST placements/lists": "slice 3: adds tools to a placement's list, undoable (BR-220)",
    "PUT placements/arguments": "slice 3: sets an argument rule, undoable (BR-220)",
    "PUT placements/pin": "slice 3: sets a placement's pin, undoable (BR-220)",
    "POST placements/tool-pins": "slice 3: pins one tool, undoable (BR-220)",
    "POST tools/classify": "slice 3: sets a tool's manual class (a lowering under a lock is "
    "refused, F-015)",
    "POST tools/accept": "slice 3: takes a tool out of the queue with its class",
    "POST setup/dismiss": "BR-229 guide dismissal only",
    "POST tools/diff": "slice 3: reads pinned changes; its fresh listing records the catalogue",
}


def _seed(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static()
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok(
        "client",
        "add",
        "claude",
        "--path",
        "mpx",
        "--redirect-uri",
        "https://claude.example.test/cb",
    )
    gw.service("spare-svc")
    gw.application("spare")
    gw.ok("tree", "add", "mpx", "project", "Spare")
    gw.provider()
    gw.ok("application", "declare", "dyn", "--service", "gitea-dev", "--mode", "dynamic")


def test_br_214_population_is_the_table() -> None:
    assert set(TABLE) == set(IMPACT_ACTS)


def test_br_214_every_write_is_classified(gw: Gateway) -> None:
    """The prescribed sweep: each write route is in the population, exempted or not destructive,
    once; an unclassified route (a later slice's import, a write named otherwise) fails here."""
    console = Console(gw)
    writes = {f"{s.method} {s.path}" for s in console.app.api_routes if s.method != "GET"}
    covered = {f"POST {route}" for route, _, _ in TABLE.values()}
    exempt = set(EXEMPT)
    benign = set(NOT_DESTRUCTIVE)
    assert not covered & exempt
    assert not covered & benign
    assert not exempt & benign
    composite = set(COMPOSITE)
    assert not composite & (covered | exempt | benign)
    assert writes == covered | exempt | benign | composite, sorted(
        writes ^ (covered | exempt | benign | composite)
    )


@pytest.mark.parametrize("act", sorted(TABLE))
def test_br_214_stale_digest_is_refused_and_fresh_one_acts(gw: Gateway, act: str) -> None:
    _seed(gw)
    console = Console(gw)
    route, query, body = TABLE[act]
    with console.client() as client:
        console.admin(client)
        read = client.get("/admin/api/v1/impacts", params={"act": act, **query},
                          headers=console.headers())  # fmt: skip
        assert read.status_code == 200, read.text
        shown = read.json()
        assert shown["act"] == act
        assert len(shown["digest"]) == 64
        for quoted in (None, "0" * 64, shown["digest"][::-1]):
            before = counts(gw)
            sent = dict(body) if quoted is None else {**body, "impact": quoted}
            refused = console.call(client, "POST", route, sent)
            assert refused.status_code == 409, (act, refused.text)
            assert refused.json()["code"] == "impact_changed"
            assert refused.json()["impact"]["digest"] == shown["digest"]
            assert counts(gw) == before, act
        done = console.call(client, "POST", route, {**body, "impact": shown["digest"]})
        assert done.status_code == 200, (act, done.text)


def test_br_214_a_change_between_read_and_write_is_refused(gw: Gateway) -> None:
    """The impact moves (a token is issued below the node): the digest shown is stale."""
    _seed(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        params = {"act": "tree.delete", "path": "mpx/cairn/staging"}
        shown = client.get("/admin/api/v1/impacts", params=params, headers=console.headers()).json()
        assert shown["impact"]["tokens_revoked"] == 0
        gw.issue("mpx/cairn/staging/dev")
        refused = console.call(client, "POST", "nodes/delete",
                               {"path": "mpx/cairn/staging",
                                "impact": shown["digest"]})  # fmt: skip
        assert refused.status_code == 409
        current = refused.json()["impact"]
        assert current["impact"]["tokens_revoked"] == 1
        assert current["digest"] != shown["digest"]
        assert "mpx/cairn/staging" in [
            n["path"] for n in console.call(client, "GET", "nodes").json()["nodes"]
        ]
        done = console.call(client, "POST", "nodes/delete",
                            {"path": "mpx/cairn/staging", "impact": current["digest"]})  # fmt: skip
        assert done.status_code == 200, done.text


@pytest.mark.parametrize("act", ["connection.revoke", "connection.remove"])
def test_br_214_a_placement_landing_while_the_act_waits_is_refused(gw: Gateway, act: str) -> None:
    """B2 (round 1): the act waits for a renewal lease; a placement made by the CLI meanwhile
    changes the impact, so the act refuses with the current impact and erases nothing."""
    _seed(gw)
    gw.build(("mpx/cairn/staging", "role", "Review"))
    console = Console(gw)
    route, query, body = TABLE[act]
    db = gw.connect()
    try:
        with console.client() as client:
            console.admin(client)
            shown = client.get("/admin/api/v1/impacts", params={"act": act, **query},
                               headers=console.headers()).json()  # fmt: skip
            assert shown["impact"]["roles"] == ["mpx/cairn/staging/dev"]
            # a renewal holds the connection for 3 s of the injected clock
            db.execute(
                "UPDATE connection SET renewing_until = ? WHERE id = ?",
                (int(gw.clock()) + 3, gw.connection_id("mpx/gitea-dev")),
            )
            db.commit()
            answer: list[Any] = []
            waiting = threading.Thread(
                target=lambda: answer.append(
                    console.call(client, "POST", route, {**body, "impact": shown["digest"]})
                )
            )
            waiting.start()
            waiting.join(0.8)
            assert waiting.is_alive(), "the act did not wait for the lease"
            gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/review")
            db.execute("UPDATE connection SET renewing_until = NULL")
            db.commit()
            waiting.join(10)
            assert not waiting.is_alive()
            refused = answer[0]
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "impact_changed"
            assert refused.json()["impact"]["impact"]["roles"] == [
                "mpx/cairn/staging/dev",
                "mpx/cairn/staging/review",
            ]
            views = console.call(client, "GET", "connections").json()["connections"]
            (view,) = [v for v in views if v["name"] == "mpx/gitea-dev"]
            assert view["state"] == "active"
            assert len(view["placements"]) == 2
    finally:
        db.close()


def test_br_214_reregister_quotes_the_connections_it_moves(gw: Gateway) -> None:
    """B3 (round 1): the impact of a reregistration lists the connections moved to
    reauthentication required, as the CLI's refusal does; a connection added since is refused."""
    _seed(gw)
    console = Console(gw)
    refused_cli = gw.cli("application", "reregister", "dyn")
    assert "connections moved to reauthentication required: none" in refused_cli.err
    with console.client() as client:
        console.admin(client)
        params = {"act": "application.reregister", "id": "dyn"}
        shown = client.get("/admin/api/v1/impacts", params=params, headers=console.headers())
        assert shown.json()["impact"] == {"reauth_required": []}
        added = console.call(client, "POST", "connections", {"name": "mpx/dyn-c",
                             "service": "gitea-dev", "application": "dyn",
                             "prefix": "dc"})  # fmt: skip
        assert added.status_code == 201, added.text
        stale = console.call(client, "POST", "applications/reregister",
                             {"id": "dyn", "confirm": "dyn",
                              "impact": shown.json()["digest"]})  # fmt: skip
        assert stale.status_code == 409, stale.text
        current = stale.json()["impact"]
        assert current["impact"] == {"reauth_required": ["mpx/dyn-c"]}
        done = console.call(client, "POST", "applications/reregister",
                            {"id": "dyn", "confirm": "dyn",
                             "impact": current["digest"]})  # fmt: skip
        assert done.status_code == 200, done.text
        assert done.json()["tools_notice"] is True


def test_br_214_reregister_rechecks_in_its_writing_transaction(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 of review 577: the impact changes *during* the provider call (a connection of the
    application is added by the CLI while the new client is registered); the digest, which held
    before the call, is compared again in the writing transaction and refuses: nothing changes
    here, the connection added keeps its state."""
    _seed(gw)
    console = Console(gw)
    original = lifecycle._register

    def register_then_change(*args: Any, **kwargs: Any) -> Any:
        registered = original(*args, **kwargs)
        gw.ok("connection", "add", "mpx/dyn-late", "--service", "gitea-dev", "--application",
              "dyn", "--prefix", "dl")  # fmt: skip
        return registered

    with console.client() as client:
        console.admin(client)
        params = {"act": "application.reregister", "id": "dyn"}
        shown = client.get("/admin/api/v1/impacts", params=params, headers=console.headers())
        assert shown.json()["impact"] == {"reauth_required": []}
        before = gw.audit()
        monkeypatch.setattr(lifecycle, "_register", register_then_change)
        refused = console.call(client, "POST", "applications/reregister",
                               {"id": "dyn", "confirm": "dyn",
                                "impact": shown.json()["digest"]})  # fmt: skip
        monkeypatch.undo()
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "impact_changed"
        assert refused.json()["impact"]["impact"] == {"reauth_required": ["mpx/dyn-late"]}
        late = [v for v in console.call(client, "GET", "connections").json()["connections"]
                if v["name"] == "mpx/dyn-late"]  # fmt: skip
    assert [v["state"] for v in late] == ["pending"]
    assert not [e for e in gw.audit()[len(before) :] if e["action"] == "application.reregister"]


def test_br_214_the_dialog_numbers_are_the_servers(gw: Gateway) -> None:
    """The impact read gives the numbers the CLI prints before the same act."""
    _seed(gw)
    gw.issue("mpx/cairn/staging/dev")
    cli = gw.cli("tree", "delete", "mpx/cairn/staging")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        params = {"act": "tree.delete", "path": "mpx/cairn/staging"}
        numbers = client.get("/admin/api/v1/impacts", params=params,
                             headers=console.headers()).json()["impact"]  # fmt: skip
    assert f"descendants: {numbers['descendants']}" in cli.out
    assert f"active tokens revoked: {numbers['tokens_revoked']}" in cli.out
    assert f"URLs that stop answering: {numbers['urls']}" in cli.out
    assert f"connection placements removed: {numbers['placements_removed']}" in cli.out


def test_br_214_move_signals_the_shares_it_creates(gw: Gateway) -> None:
    """BR-079: a move making a placed connection shared lists it before acting."""
    _seed(gw)
    gw.build(("/", "client", "Other"))
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        params = {"act": "tree.move", "path": "mpx/cairn", "parent": "other"}
        shown = client.get("/admin/api/v1/impacts", params=params, headers=console.headers())
    assert shown.status_code == 200, shown.text
    shares = shown.json()["shares"]
    assert [s["connection"] for s in shares] == ["mpx/gitea-dev"]
    assert shares[0]["reasons"] == ["other_client"]
