"""AC-23 and the whole-placement comparison of BR-221."""

from tests.admin_support import Console
from tests.integration.test_br_219_undo import latest, reverse
from tests.relay_bed import A, Bed
from tests.support import Gateway


def test_br_221_setting_example(gw: Gateway) -> None:
    Bed(gw)
    gw.ok("settings", "set", A, "token_duration", "30d")
    gw.ok("settings", "set", A, "token_duration", "7d")
    old = latest(gw)["seq"]
    gw.ok("settings", "set", A, "token_duration", "14d")
    new = latest(gw)["seq"]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        response = reverse(console, client, old)
        assert response.status_code == 409
        assert response.json()["code"] == "undo_stale"
        assert response.json()["current"] == {"token_duration": "14d"}
        assert response.json()["expected"] == {"token_duration": "7d"}
        assert reverse(console, client, new).status_code == 200


def test_br_221_placement_policy_change_is_stale(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ab", A)
    seq = latest(gw)["seq"]
    gw.ok("policy", "mode", "mpx/ab", "--role", A, "write")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        response = reverse(console, client, seq)
        assert response.status_code == 409
        assert response.json()["code"] == "undo_stale"


def test_br_221_sensitive_undo_requires_current_impact(gw: Gateway) -> None:
    Bed(gw)
    gw.ok("tree", "sensitive", "mpx/cairn/staging", "on")
    seq = latest(gw)["seq"]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        plan = console.call(client, "GET", f"changes?seq={seq}").json()
        gw.issue("mpx/cairn")
        response = console.call(
            client, "POST", "changes/undo", {"seq": seq, "impact": plan["impact"]["digest"]}
        )
        assert response.status_code == 409
        assert response.json()["code"] == "impact_changed"
        assert reverse(console, client, seq).status_code == 200
