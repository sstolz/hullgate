"""BR-211, BR-086 (round 1, B1 and N1) — the API refuses what the CLI refuses, as a refusal.

A provided token or an application secret goes through the CLI reader's checks on every route
that takes one (`secret_value`: empty, blank, several lines, too long; one final newline
removed); the CLI refuses the same values with the same text. Odd inputs that used to raise
(a lone surrogate, an audit cursor beyond SQLite's integers, an unreadable URL) are refusals
that write nothing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hullgate.connections import SECRET_MAX
from tests.admin_support import Console, counts
from tests.support import Gateway, standard_tree

REFUSED = {
    "": "empty value",
    "   ": "empty value",
    "abc\ndef": "value on several lines",
    "abc\r\ndef": "value on several lines",
    "x" * (SECRET_MAX + 1): "value too long",
}


def _seed(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()


def _generation(gw: Gateway, name: str = "mpx/gitea-dev") -> int:
    db = gw.connect()
    try:
        row = db.execute(
            "SELECT generation FROM connection WHERE id = ?", (gw.connection_id(name),)
        ).fetchone()
        return int(row[0])
    finally:
        db.close()


@pytest.mark.parametrize("value", sorted(REFUSED))
def test_br_211_cli_and_api_refuse_the_same_provided_values(gw: Gateway, value: str) -> None:
    _seed(gw)
    gw.static()
    cli = gw.cli(
        "connection",
        "add",
        "mpx/other",
        "--service",
        "gitea-dev",
        "--provided",
        "--file",
        "-",
        "--prefix",
        "ot",
        stdin=value,
    )
    assert cli.code == 2
    assert REFUSED[value] in cli.err
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before, generation = counts(gw), _generation(gw)
        bodies: list[tuple[str, dict[str, Any]]] = [
            ("connections/reauthorize", {"name": "mpx/gitea-dev", "provided": {"value": value}}),
            (
                "connections",
                {
                    "name": "mpx/other",
                    "service": "gitea-dev",
                    "prefix": "ot",
                    "provided": {"value": value},
                },
            ),
            (
                "applications",
                {
                    "id": "hand",
                    "service": "gitea-dev",
                    "mode": "declared",
                    "client_id": "client-hand",
                    "secret": value,
                },
            ),
        ]
        for route, body in bodies:
            refused = console.call(client, "POST", route, body)
            if len(value) > SECRET_MAX:  # over the body's limit before the value's (BR-205)
                assert refused.status_code == 413, (route, refused.text)
                continue
            assert refused.status_code == 409, (route, refused.text)
            assert refused.json()["code"] == "refused"
            assert REFUSED[value] in refused.json()["message"]
            assert value.strip() == "" or value[:20] not in refused.text
        assert counts(gw) == before
        views = console.call(client, "GET", "connections").json()["connections"]
        assert [v["name"] for v in views] == ["mpx/gitea-dev"]
        assert views[0]["state"] == "active"
        assert _generation(gw) == generation


def test_br_211_a_final_newline_is_removed_as_the_cli_does(gw: Gateway) -> None:
    _seed(gw)
    gw.static()
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        generation = _generation(gw)
        done = console.call(
            client,
            "POST",
            "connections/reauthorize",
            {"name": "mpx/gitea-dev", "provided": {"value": "tok-new\n"}},
        )
        assert done.status_code == 200, done.text
        assert _generation(gw) == generation + 1
    # what is stored, and what the relay delivers upstream, is the value without its newline
    assert gw.tokens("mpx/gitea-dev") == {"access_token": "tok-new", "token_type": "Bearer"}
    assert gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev").authorization == "Bearer tok-new"


def test_br_211_an_empty_application_secret_is_refused_as_the_cli_does(gw: Gateway) -> None:
    """Round 2 of review 577: a secret given empty is not an absent one; the CLI refuses an
    empty `--secret-file`, the API refuses `"secret": ""` with the same text."""
    _seed(gw)
    cli = gw.cli("application", "declare", "hand", "--service", "gitea-dev", "--mode",
                 "declared", "--client-id", "client-hand", "--secret-file", "-",
                 stdin="")  # fmt: skip
    assert cli.code == 2
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = counts(gw)
        refused = console.call(client, "POST", "applications", {
            "id": "hand", "service": "gitea-dev", "mode": "declared",
            "client_id": "client-hand", "secret": "",
        })  # fmt: skip
        absent = console.call(client, "POST", "applications", {
            "id": "hand", "service": "gitea-dev", "mode": "declared",
            "client_id": "client-hand",
        })  # fmt: skip
    assert refused.status_code == 409, refused.text
    assert cli.err.strip() == f"refused: {refused.json()['message']}"
    assert absent.status_code == 201, absent.text  # no secret at all stays allowed
    assert counts(gw)["application"] == before["application"] + 1


@pytest.mark.parametrize(
    ("method", "route", "body"),
    [
        ("POST", "nodes", {"parent": "mpx", "level": "project", "name": "x\ud800y"}),
        (
            "POST",
            "connections/reauthorize",
            {"name": "mpx/gitea-dev", "provided": {"value": "tok\udfffx"}},
        ),
    ],
)
def test_br_211_a_lone_surrogate_is_a_400(
    gw: Gateway, method: str, route: str, body: dict[str, Any]
) -> None:
    _seed(gw)
    gw.static()
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = counts(gw)
        raw = json.dumps(body).encode("ascii")  # "\\ud800" escapes: valid JSON, invalid text
        refused = client.request(
            method,
            f"/admin/api/v1/{route}",
            content=raw,
            headers={**console.headers(), "content-type": "application/json"},
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "invalid_request"
        assert counts(gw) == before


@pytest.mark.parametrize(
    "query",
    [
        {"before": "9" * 23},
        {"before": str(2**63)},
        {"limit": "9" * 5000},
        {"before": "9" * 5000},
    ],
)
def test_br_211_audit_cursors_out_of_range_are_422(gw: Gateway, query: dict[str, str]) -> None:
    _seed(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        refused = client.get("/admin/api/v1/audit", params=query, headers=console.headers())
        assert refused.status_code == 422, refused.text


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("clients", {"client_id": "claude", "path": "mpx", "redirect_uris": ["https://["]}),
        (
            "clients",
            {
                "client_id": "https://[",
                "path": "mpx",
                "redirect_uris": ["https://claude.example.test/cb"],
            },
        ),
        ("connections/authorize", {"name": "mpx/gitea-dev", "callback": "https://["}),
    ],
)
def test_br_211_an_unreadable_url_is_refused(gw: Gateway, route: str, body: dict[str, Any]) -> None:
    _seed(gw)
    gw.application()
    gw.provider()
    gw.open_oauth()
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = counts(gw)
        refused = console.call(client, "POST", route, body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "refused"
        if route == "clients":
            assert counts(gw) == before


def test_br_211_cli_refuses_an_unreadable_redirect_uri(gw: Gateway) -> None:
    _seed(gw)
    refused = gw.cli("client", "add", "claude", "--path", "mpx", "--redirect-uri", "https://[")
    assert refused.code == 2, refused
    assert "redirect_uri refused" in refused.err
