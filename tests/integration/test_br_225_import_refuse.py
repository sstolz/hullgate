"""AC-27: bounded safe YAML, precise locations and credentialless restore."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.admin_support import Console, counts
from tests.integration.test_br_222_import_preview import apply, document, encoded, preview
from tests.policy_support import catalogue, gitea, rows
from tests.relay_bed import A, Bed
from tests.support import Gateway


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("x" * (1024 * 1024 + 1), 413),
        ("!!python/object:os.system {}", 422),
        ("x: &a [1]\ny: *a", 422),
        ("format: 4", 422),
        ("[1, 2", 422),
        ("format: 5\nformat: 5", 422),
        ("[" * 100 + "]" * 100, 422),
        ('"\\ud800": value', 422),
    ],
)
def test_br_225_malformed_is_refused_with_location(gw: Gateway, text: str, status: int) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = counts(gw)
        result = console.call(client, "POST", "import/preview", {"document": text})
        assert result.status_code == status, result.text
        assert result.json()["location"]
        assert counts(gw) == before


@pytest.mark.parametrize(
    "where",
    ["root", "node", "settings", "service", "application", "connection", "placement", "argument"],
)
def test_br_225_unknown_keys_at_every_level(gw: Gateway, where: str) -> None:
    bed = Bed(gw)
    gitea(bed, "ab", A)
    catalogue(gw, "mpx/ab", A)
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
    doc = document(gw)
    doc["applications"] = [
        {"id": "app", "service": "sab", "mode": "declared", "client_id": "public"}
    ]
    targets = {
        "root": doc,
        "node": doc["organisation"],
        "settings": doc["organisation"]["settings"],
        "service": doc["services"][0],
        "application": doc["applications"][0],
        "connection": doc["connections"][0],
        "placement": doc["connections"][0]["placements"][0],
        "argument": doc["connections"][0]["placements"][0]["arguments"][0],
    }
    targets[where]["unknown"] = "value"
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        result = console.call(client, "POST", "import/preview", {"document": encoded(doc)})
        assert result.status_code == 422, result.text
        assert "unknown" in result.json()["location"]


def test_br_236_restored_library_has_no_credentials_and_adopts_id(
    gw: Gateway, tmp_path: Path
) -> None:
    bed = Bed(gw)
    gitea(bed, "ab", A)
    catalogue(gw, "mpx/ab", A)
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
    doc = document(gw)
    doc["applications"] = [
        {"id": "restored", "service": "sab", "mode": "declared", "client_id": "public-id"}
    ]
    (tmp_path / "restore").mkdir()
    restored = Gateway(tmp_path / "restore")
    try:
        console = Console(restored)
        with console.client() as client:
            console.admin(client)
            text = encoded(doc)
            plan = preview(console, client, text)
            assert plan["reauthorize"] == 1
            response = apply(console, client, text, plan)
            assert response.status_code == 200, response.text
            assert document(restored)["organisation"]["id"] == doc["organisation"]["id"]
            assert rows(restored, "SELECT tokens_enc, state FROM connection") == [
                (None, "reauth_required")
            ]
            assert rows(restored, "SELECT secret_enc FROM application") == [(None,)]
            assert rows(restored, "SELECT count(*) FROM argument_rule") == [(1,)]
            assert rows(restored, "SELECT count(*) FROM pinned_definition") == [(0,)]
            # The general store guard is still present after the narrowly scoped adoption.
            with restored.connect() as conn:
                import sqlite3

                with pytest.raises(sqlite3.IntegrityError, match="permanent"):
                    conn.execute("UPDATE node SET public_id = 'aaaaaaaaaaaa' WHERE level = 'org'")

    finally:
        restored.close()


def test_br_225_foreign_id_populated_refused(gw: Gateway) -> None:
    Bed(gw)
    doc = document(gw)
    doc["organisation"]["id"] = "aaaaaaaaaaaa"
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        result = console.call(client, "POST", "import/preview", {"document": encoded(doc)})
        assert result.status_code == 422
        assert result.json()["code"] == "import_foreign_organisation"


@pytest.mark.parametrize(
    "value",
    ['"\\ud800"', "9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59", "[]", "{}", "null"],
)
def test_br_225_unstorable_text_and_calendar_edges_are_refusals(gw: Gateway, value: str) -> None:
    text = gw.ok("export").replace('name: "Organisation"', "name: " + value, 1)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = counts(gw)
        result = console.call(client, "POST", "import/preview", {"document": text})
        assert result.status_code == 422, result.text
        assert result.json()["location"]
        assert counts(gw) == before
        result = console.call(client, "POST", "import/apply", {"document": text, "digest": "none"})
        assert result.status_code == 422, result.text
        assert gw.audit()[-1]["action"] == "import.apply"
        assert gw.audit()[-1]["result"] == "refused"


def test_br_225_exact_byte_limit_and_large_json_transport(gw: Gateway) -> None:
    from hullgate.importing import MAX_BYTES

    text = gw.ok("export")
    text += "#" + " " * (MAX_BYTES - len(text.encode()) - 1)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        assert preview(console, client, text)["changes"] == []
        result = console.call(client, "POST", "import/preview", {"document": text + " "})
        assert result.status_code == 413


def test_br_225_adoption_rolls_back_with_late_refusal(gw: Gateway) -> None:
    import sqlite3

    original = document(gw)
    desired = document(gw)
    desired["organisation"]["id"] = "aaaaaaaaaaaa"
    desired["organisation"]["settings"]["lock"] = "invalid"
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        text = encoded(desired)
        plan = preview(console, client, text)
        refused = apply(console, client, text, plan)
        assert refused.status_code == 409
        assert document(gw) == original
        with gw.connect() as c, pytest.raises(sqlite3.IntegrityError, match="permanent"):
            c.execute("UPDATE node SET public_id = 'aaaaaaaaaaaa' WHERE level = 'org'")
