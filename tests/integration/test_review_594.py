"""Review 594: malformed credentials and calendar-edge expiries through real entrypoints."""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.admin_support import CLIENT_IP, Console, b64, unb64
from tests.integration.test_br_060_consent import _pending
from tests.integration.test_br_060_passkey import enrolled, post, public
from tests.policy_support import rows
from tests.support import PUBLIC_URL, Gateway, standard_tree


@pytest.mark.parametrize(
    ("field", "surface"), [("id", "barrier"), ("challenge", "barrier"), ("challenge", "console")]
)
def test_b1_surrogates_are_counted_and_audited(gw: Gateway, surface: str, field: str) -> None:
    console, key = enrolled(gw)
    peer = "203.0.113.8"
    action = "oauth.consent" if surface == "barrier" else "admin.sign_in"
    transport = public(gw, console.origin, peer) if surface == "barrier" else console.client(peer)
    with transport as client:
        req = _pending(gw, client) if surface == "barrier" else ""
        options = (
            post(client, req, "passkey-options")
            if surface == "barrier"
            else console.call(client, "POST", "auth/options", {}, csrf="")
        ).json()["publicKey"]
        valid = key.get(options, PUBLIC_URL if surface == "barrier" else console.origin.origin)
        credential = copy.deepcopy(valid)
        if field == "id":
            credential["id"] = "\ud800"
        else:
            data = json.loads(unb64(credential["response"]["clientDataJSON"]))
            data["challenge"] = "\ud800"
            credential["response"]["clientDataJSON"] = b64(json.dumps(data).encode())

        def submit(value: dict[str, Any]) -> Any:
            if surface == "barrier":
                return post(client, req, "passkey", value)
            # Escaped JSON exercises server parsing rather than httpx's UTF-8 encoder.
            return client.post(
                "/admin/api/v1/auth/assert",
                content=json.dumps({"credential": value}),
                headers={**console.headers(csrf=""), "Content-Type": "application/json"},
            )

        assert rows(gw, "SELECT challenge FROM webauthn_challenge") == [(options["challenge"],)]
        previous = len(gw.audit())
        response = submit(credential)
        assert response.status_code == (200 if surface == "barrier" else 401)
        assert rows(gw, "SELECT address FROM admin_attempt") == [(peer,)]
        events = gw.audit()[previous:]
        assert len(events) == 1
        assert (events[0]["action"], events[0]["result"]) == (action, "refused")
        assert json.loads(events[0]["detail"])["address"] == peer
        # A valid challenge carried with a bad id is consumed, even on failure. A malformed
        # challenge identifies no stored row; it must not delete unrelated live challenges.
        remaining = [] if field == "id" else [(options["challenge"],)]
        assert rows(gw, "SELECT challenge FROM webauthn_challenge") == remaining
        if field == "id":
            assert submit(valid).status_code == (200 if surface == "barrier" else 401)
            assert rows(gw, "SELECT address FROM admin_attempt") == [(peer,), (peer,)]
        else:
            for _ in range(4):
                assert submit(credential).status_code == (200 if surface == "barrier" else 401)
            assert submit(credential).status_code == 429
            assert rows(gw, "SELECT count(*) FROM admin_attempt") == [(5,)]
        assert rows(gw, "SELECT count(*) FROM auth_code") == [(0,)]


@pytest.mark.parametrize("surface", ["cli", "api"])
@pytest.mark.parametrize("operation", ["add", "replace"])
@pytest.mark.parametrize("expiry", ["9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59"])
def test_n1_out_of_utc_calendar_is_refused(
    gw: Gateway, surface: str, operation: str, expiry: str
) -> None:
    standard_tree(gw)
    gw.service()
    if operation == "replace":
        gw.static()
    before = rows(gw, "SELECT * FROM connection")
    if surface == "cli":
        args = (
            ["add", "mpx/gitea-dev", "--service", "gitea-dev", "--provided", "--prefix", "gd"]
            if operation == "add"
            else ["reauthorize", "mpx/gitea-dev"]
        )
        result = gw.cli(
            "connection",
            *args,
            "--file",
            "-",
            "--expires-at",
            expiry,
            stdin="throwaway-provided-token\n",
        )
        assert result.code == 2
        assert (
            result.err.strip() == "refused: expires_at: ISO 8601 timestamp with timezone expected"
        )
    else:
        console = Console(gw)
        with console.client() as client:
            console.admin(client)
            response = console.call(
                client,
                "POST",
                "connections" if operation == "add" else "connections/reauthorize",
                {
                    "name": "mpx/gitea-dev",
                    "service": "gitea-dev",
                    "prefix": "gd",
                    "provided": {"value": "throwaway-provided-token", "expires_at": expiry},
                },
            )
            assert response.status_code == 409
            assert response.json()["message"] == (
                "expires_at: ISO 8601 timestamp with timezone expected"
            )
    assert rows(gw, "SELECT * FROM connection") == before


@pytest.mark.parametrize("expiry", ["0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z"])
def test_n1_calendar_edges_round_trip(gw: Gateway, expiry: str) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/edge", "ed", "gitea-dev", "--expires-at", expiry)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        response = console.call(client, "GET", "connections")
        assert response.status_code == 200
        assert response.json()["connections"][0]["access_ends_at"] == expiry
    event = next(e for e in gw.audit() if e["action"] == "connection.add")
    assert json.loads(event["after"])["expires_at"] == expiry


def test_n2_runbook_proxy_address_blocks_loopback_forgery(gw: Gateway) -> None:
    runbook = Path("docs/guides/admin.md").read_text().split("## Step D3", 1)[1]
    proxy = re.search(r"^TRUSTED_PROXIES=(.+)$", runbook, re.MULTILINE)
    assert proxy is not None
    console = Console(gw, admin_url="https://admin.example.test", proxies=proxy[1])
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        assert console.call(client, "PUT", "allowlist", {"cidrs": [CLIENT_IP]}).status_code == 200
    with console.client("172.18.0.1") as host:
        for headers in ({}, {"X-Forwarded-For": CLIENT_IP}):
            assert host.get("/admin/api/v1/session", headers=headers).status_code == 403
    with console.client("172.18.0.2") as traefik:
        # The real proxy can forward an allowed address; authentication remains required.
        assert (
            traefik.get("/admin/api/v1/session", headers={"X-Forwarded-For": CLIENT_IP}).status_code
            == 401
        )
