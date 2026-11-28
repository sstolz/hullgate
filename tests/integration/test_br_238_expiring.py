"""AC-38: known non-renewable expiry, global immediate threshold, CLI/API parity."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.admin_support import Console
from tests.fake_provider import Reply
from tests.policy_support import rows
from tests.support import Gateway, standard_tree


def test_br_238_provided_expiry_and_threshold_are_live(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    end = datetime.fromtimestamp(gw.clock() + 6 * 86400, UTC).isoformat()
    gw.static("mpx/known", "kn", "gitea-dev", "--expires-at", end)
    gw.static("mpx/unknown", "un")
    gw.oauth("mpx/renewable", "rn")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)

        def read() -> dict[str, Any]:
            response = console.call(client, "GET", "connections")
            assert response.status_code == 200, response.text
            return {c["name"]: c for c in response.json()["connections"]}

        assert read()["mpx/known"]["expiring"]
        assert not read()["mpx/unknown"]["expiring"]
        assert not read()["mpx/renewable"]["expiring"]
        assert (
            console.call(
                client,
                "PUT",
                "settings",
                {"path": "/", "key": "connection_expiry_days", "value": "5"},
            ).status_code
            == 200
        )
        assert not read()["mpx/known"]["expiring"]
        gw.ok("settings", "set", "/", "connection_expiry_days", "7")
        assert read()["mpx/known"]["expiring"]
        replaced = console.call(
            client,
            "POST",
            "connections/reauthorize",
            {"name": "mpx/known", "provided": {"value": "replacement-value"}},
        )
        assert replaced.status_code == 200
        assert not read()["mpx/known"]["expiring"]
        assert read()["mpx/known"]["access_ends_at"] is None
    changed = [a for a in gw.audit() if a["action"] == "settings.changed"]
    assert len(changed) == 2
    assert json.loads(changed[0]["after"]) == {"connection_expiry_days": "5"}


@pytest.mark.parametrize("value", ["0", "91", "1.5", "-1", "true", "1d", "", "9" * 5000])
def test_br_238_invalid_threshold_refuses_identically(gw: Gateway, value: str) -> None:
    console = Console(gw)
    cli = gw.cli("settings", "set", "/", "connection_expiry_days", value)
    assert cli.code == 2
    with console.client() as client:
        console.admin(client)
        api = console.call(
            client,
            "PUT",
            "settings",
            {"path": "/", "key": "connection_expiry_days", "value": value},
        )
        assert api.status_code == 409
        assert cli.err.strip() == "refused: " + api.json()["message"]


def test_br_238_setting_cannot_be_overridden_below_org(gw: Gateway) -> None:
    standard_tree(gw)
    assert gw.cli("settings", "set", "mpx", "connection_expiry_days", "5").code == 2
    assert rows(gw, "SELECT value FROM setting WHERE key = 'connection_expiry_days'") == [("7",)]


def test_br_238_refresh_expiry_is_saved_and_rotation_without_expiry_clears_it(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.provider()
    provider.refresh_expires_in = 6 * 86400
    gw.oauth()
    assert rows(gw, "SELECT refresh_expires_at FROM connection") == [(int(gw.clock()) + 6 * 86400,)]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        assert console.call(client, "GET", "connections").json()["connections"][0]["expiring"]
        # No rotation: a previously known deadline is not lost merely because omitted.
        provider.rotate = False
        provider.refresh_expires_in = None
        gw.ok("connection", "renew", "mpx/gitea-dev")
        assert rows(gw, "SELECT refresh_expires_at FROM connection")[0][0] is not None
        provider.rotate = True
        gw.ok("connection", "renew", "mpx/gitea-dev")
        assert rows(gw, "SELECT refresh_expires_at FROM connection") == [(None,)]
        assert not console.call(client, "GET", "connections").json()["connections"][0]["expiring"]


@pytest.mark.parametrize("value", [-1, True, 1.5, "3600", 10**100, 253402300800])
def test_br_238_invalid_refresh_expiry_is_a_provider_anomaly(gw: Gateway, value: Any) -> None:
    standard_tree(gw)
    gw.service()
    gw.application()
    provider = gw.provider()
    url = gw.open_oauth()
    provider.script.append(Reply(normal=True, body={"refresh_token_expires_in": value}))
    result = gw.cli(
        "connection",
        "authorize",
        "mpx/gitea-dev",
        "--callback",
        "-",
        stdin=provider.approve(url),
    )
    assert result.code != 0
    assert "invalid refresh_token_expires_in" in result.err
    assert gw.row("mpx/gitea-dev")["tokens_enc"] is None
