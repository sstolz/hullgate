"""AC-24: deterministic round trip, all-table read-only preview, stale document/state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from tests.admin_support import Console, counts
from tests.policy_support import catalogue, gitea
from tests.relay_bed import A, Bed
from tests.support import Gateway


def document(gw: Gateway) -> dict[str, Any]:
    return dict(yaml.safe_load(gw.ok("export")))


def encoded(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def preview(console: Console, client: Any, text: str) -> dict[str, Any]:
    response = console.call(client, "POST", "import/preview", {"document": text})
    assert response.status_code == 200, response.text
    return dict(response.json())


def apply(console: Console, client: Any, text: str, plan: dict[str, Any], **opts: Any) -> Any:
    return console.call(
        client, "POST", "import/apply", {"document": text, "digest": plan["digest"], **opts}
    )


def test_br_222_round_trip_all_tables_read_only_and_cli(gw: Gateway, tmp_path: Path) -> None:
    bed = Bed(gw)
    gitea(bed, "ab", A)
    catalogue(gw, "mpx/ab", A)
    gw.ok("policy", "mode", "mpx/ab", "--role", A, "write")
    gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")
    text = gw.ok("export")
    path = tmp_path / "state.yaml"
    path.write_text(text)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = counts(gw)
        with gw.connect() as conn:
            snapshot = list(conn.iterdump())
        plan = preview(console, client, text)
        assert plan["changes"] == []
        assert counts(gw) == before
        with gw.connect() as conn:
            assert list(conn.iterdump()) == snapshot
        assert gw.ok("export") == text
        assert console.call(client, "GET", "export").text == text
        assert json.loads(gw.ok("import", "--preview", str(path))) == plan
        assert json.loads(gw.ok("import", "--apply", plan["digest"], str(path)))["changes"] == 0


def test_br_222_changed_state_and_document_refuse(gw: Gateway) -> None:
    Bed(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        text = gw.ok("export")
        plan = preview(console, client, text)
        for changed in (text + "# changed bytes\n",):
            response = apply(console, client, changed, plan)
            assert response.status_code == 409
            assert response.json()["code"] == "import_stale"
        gw.ok("settings", "set", A, "token_duration", "7d")
        response = apply(console, client, text, plan)
        assert response.status_code == 409
        assert response.json()["code"] == "import_stale"
