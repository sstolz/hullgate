"""AC-26: opt-in deletion, path identity, absent setting means inheritance."""

from tests.admin_support import Console
from tests.integration.test_br_222_import_preview import apply, document, encoded, preview
from tests.relay_bed import Bed
from tests.support import Gateway


def test_br_224_deletions_disabled_and_segment_is_create_delete(gw: Gateway) -> None:
    Bed(gw)
    doc = document(gw)
    client = doc["organisation"]["clients"][0]
    client["segment"] = "new-client"
    text = encoded(doc)
    console = Console(gw)
    with console.client() as browser:
        console.admin(browser)
        plan = preview(console, browser, text)
        assert any(c["action"] == "delete" and c["path"] == "mpx" for c in plan["changes"])
        assert any(c["action"] == "create" and c["path"] == "new-client" for c in plan["changes"])
        assert apply(console, browser, text, plan).status_code == 200
        assert gw.cli("tree", "show", "mpx").code == 0
        assert gw.cli("tree", "show", "new-client").code == 0
        plan = preview(console, browser, text)
        response = apply(console, browser, text, plan, apply_deletions=True)
        assert response.status_code == 200, response.text
        assert gw.cli("tree", "show", "mpx").code == 2


def test_br_224_omitted_override_is_change_without_deletions(gw: Gateway) -> None:
    Bed(gw)
    doc = document(gw)
    gw.ok("settings", "set", "mpx", "token_duration", "7d")
    text = encoded(doc)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        plan = preview(console, client, text)
        assert plan["changes"][0]["action"] == "change"
        assert apply(console, client, text, plan).status_code == 200
        assert document(gw) == doc
