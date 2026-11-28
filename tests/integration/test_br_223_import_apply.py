"""AC-25: rollback late refusals, domain parity, one import identifier, no partial undo."""

import copy
import json

from tests.admin_support import Console
from tests.integration.test_br_222_import_preview import apply, document, encoded, preview
from tests.policy_support import rows
from tests.relay_bed import A, Bed
from tests.support import Gateway


def test_br_223_last_item_rolls_back_and_names_path(gw: Gateway) -> None:
    Bed(gw)
    doc = document(gw)
    doc["organisation"]["name"] = "Changed then rolled back"
    doc["organisation"]["clients"][0]["settings"] = {"lock": "not-a-bool"}
    text = encoded(doc)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        plan = preview(console, client, text)
        before = gw.ok("export")
        audit = gw.audit()
        response = apply(console, client, text, plan)
        assert response.status_code == 409, response.text
        assert response.json()["location"] == "mpx:lock"
        assert gw.ok("export") == before
        assert gw.audit()[: len(audit)] == audit
        assert len(gw.audit()) == len(audit) + 1
        assert gw.audit()[-1]["result"] == "refused"


def test_br_223_all_entries_share_id_and_cannot_be_undone(gw: Gateway) -> None:
    Bed(gw)
    doc = document(gw)
    doc["organisation"]["name"] = "Imported name"
    doc["organisation"]["clients"][0]["settings"] = {"token_duration": "7d"}
    text = encoded(doc)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        plan = preview(console, client, text)
        n = len(gw.audit())
        response = apply(console, client, text, plan)
        assert response.status_code == 200, response.text
        new = gw.audit()[n:]
        assert len(new) >= 3
        for e in new:
            assert json.loads(e["detail"])["import_id"] == response.json()["import_id"]
            read = console.call(client, "GET", f"changes?seq={e['seq']}")
            assert read.json()["undo_reason"] == "part_of_import"
            refused = console.call(client, "POST", "changes/undo", {"seq": e["seq"]})
            assert refused.status_code == 409
            assert refused.json()["code"] == "part_of_import"


def test_br_223_share_preview_and_apply_confirmation(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ab", A)
    doc = document(gw)
    p = copy.deepcopy(doc["connections"][0]["placements"][0])
    p["path"] = "mpx/cairn/prod/dev"
    doc["connections"][0]["placements"].append(p)
    text = encoded(doc)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        plan = preview(console, client, text)
        assert plan["changes"][0]["impact"]["shares"] == ["sensitivity"]
        before = rows(gw, "SELECT * FROM placement")
        response = apply(console, client, text, plan)
        assert response.status_code == 409
        assert response.json()["code"] == "share_confirmation_required"
        assert rows(gw, "SELECT * FROM placement") == before
        response = apply(console, client, text, plan, confirm_share=True)
        assert response.status_code == 200, response.text


def test_br_223_removal_rolls_back_before_any_provider_revocation(gw: Gateway, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from hullgate import lifecycle

    bed = Bed(gw)
    bed.connection("ab", A)
    doc = document(gw)
    doc["connections"] = []
    doc["organisation"]["settings"]["lock"] = "invalid"
    calls = []

    def revoked(*args):  # type: ignore[no-untyped-def]
        # This hook is the existing best-effort remote revocation boundary.
        with gw.connect() as c:
            assert c.execute("SELECT removed FROM connection").fetchone()[0] == 1
        calls.append(True)
        return "revoked"

    monkeypatch.setattr(lifecycle, "_finish_upstream", revoked)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        text = encoded(doc)
        plan = preview(console, client, text)
        before = rows(gw, "SELECT * FROM connection")
        placements = rows(gw, "SELECT * FROM placement")
        refused = apply(console, client, text, plan, apply_deletions=True)
        assert refused.status_code == 409, refused.text
        assert refused.json()["location"] == "/:lock"
        assert rows(gw, "SELECT * FROM connection") == before
        assert rows(gw, "SELECT * FROM placement") == placements
        assert calls == []
        doc["organisation"]["settings"]["lock"] = "false"
        text = encoded(doc)
        plan = preview(console, client, text)
        retained = apply(console, client, text, plan)
        assert retained.status_code == 200, retained.text
        assert rows(gw, "SELECT * FROM connection") == before
        assert calls == []
        plan = preview(console, client, text)
        removed = apply(console, client, text, plan, apply_deletions=True)
        assert removed.status_code == 200, removed.text
        assert calls == [True]
