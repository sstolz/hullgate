"""BR-202 (AC-02) — the console is entered with a passkey and nothing else."""

from __future__ import annotations

from typing import Any

import pytest

from hullgate import config
from hullgate.admin import auth
from tests.admin_support import Console, SoftAuthenticator
from tests.support import Gateway

GENERIC = "Sign-in failed. Try again."


@pytest.fixture
def enrolled(gw: Gateway) -> tuple[Console, SoftAuthenticator]:
    console = Console(gw)
    with console.client() as client:
        authenticator = console.admin(client)
    return console, authenticator


def _assert(console: Console, client: Any, credential: dict[str, Any]) -> Any:
    return console.call(client, "POST", "auth/assert", {"credential": credential}, csrf="")


def _options(console: Console, client: Any) -> dict[str, Any]:
    response = console.call(client, "POST", "auth/options", {}, csrf="")
    assert response.status_code == 200
    options: dict[str, Any] = response.json()["publicKey"]
    return options


def test_br_202_sign_in_with_a_registered_passkey(
    enrolled: tuple[Console, SoftAuthenticator],
) -> None:
    console, authenticator = enrolled
    with console.client() as client:
        options = _options(console, client)
        assert options["userVerification"] == "required"
        assert options["allowCredentials"] == []  # discoverable: nothing typed before
        assert options["rpId"] == "localhost"
        response = _assert(console, client, authenticator.get(options))
        assert response.status_code == 200
        assert response.json()["administrator"]["email"] == "ops@example.test"
        assert console.call(client, "GET", "session").status_code == 200
    signed = [e for e in console.gw.audit() if e["action"] == "admin.sign_in"]
    assert signed[-1]["result"] == "ok"
    assert signed[-1]["actor"].startswith("admin:adm_")


def test_br_202_registration_options_ask_resident_key_and_no_attestation(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        token = console.bootstrap()
        options = console.call(client, "POST", "enrol/options", {"link": token}, csrf="").json()
    public_key = options["publicKey"]
    assert public_key["attestation"] == "none"
    assert public_key["authenticatorSelection"]["residentKey"] == "required"
    assert public_key["authenticatorSelection"]["userVerification"] == "required"
    assert public_key["rp"]["id"] == "localhost"
    assert options["email"] == "ops@example.test"


@pytest.mark.parametrize("case", ["no_uv", "other_origin", "replayed", "unknown", "rp_id"])
def test_br_202_failures_share_one_message(
    enrolled: tuple[Console, SoftAuthenticator], case: str
) -> None:
    console, authenticator = enrolled
    with console.client() as client:
        options = _options(console, client)
        if case == "no_uv":
            credential = authenticator.get(options, uv=False)
        elif case == "other_origin":
            credential = authenticator.get(options, origin="http://localhost:9999")
        elif case == "unknown":
            credential = SoftAuthenticator().get(options)
        elif case == "rp_id":
            credential = authenticator.get(options, rp_id="evil.test")
        else:
            first = authenticator.get(options)
            assert _assert(console, client, first).status_code == 200
            credential = first
        response = _assert(console, client, credential)
    assert response.status_code == 401
    assert response.json() == {"code": "sign_in_failed", "message": GENERIC}
    refused = [e for e in console.gw.audit() if e["action"] == "admin.sign_in"][-1]
    assert refused["result"] == "refused"


def test_br_202_a_challenge_older_than_five_minutes_fails(
    enrolled: tuple[Console, SoftAuthenticator],
) -> None:
    console, authenticator = enrolled
    with console.client() as client:
        options = _options(console, client)
        console.gw.clock.advance(auth.CHALLENGE_TTL_S + 1)
        response = _assert(console, client, authenticator.get(options))
    assert response.status_code == 401


def test_br_202_a_counter_going_backwards_is_refused_and_audited(
    enrolled: tuple[Console, SoftAuthenticator],
) -> None:
    console, authenticator = enrolled
    with console.client() as client:
        assert console.sign_in(client, authenticator).status_code == 200
        response = _assert(console, client, authenticator.get(_options(console, client), count=1))
    assert response.status_code == 401
    refused = [e for e in console.gw.audit() if e["action"] == "admin.sign_in"][-1]
    assert refused["reason"] == "counter_regressed"


def test_br_202_a_zero_counter_authenticator_signs_in_again(gw: Gateway) -> None:
    """Authenticators that report no counter (always 0) are not refused for it."""
    console = Console(gw)
    with console.client() as client:
        authenticator = console.admin(client)
        for _ in range(2):
            assert console.sign_in(client, authenticator, count=0).status_code == 200


def test_br_202_no_password_or_totp_route_exists(gw: Gateway) -> None:
    console = Console(gw)
    paths = {s.path for s in console.app.api_routes}
    assert not {p for p in paths if any(w in p for w in ("password", "totp", "login"))}


@pytest.mark.parametrize(
    ("env", "variable"),
    [
        ({"ADMIN_URL": "http://127.0.0.1:14791"}, "ADMIN_URL"),
        ({"ADMIN_URL": "https://[::1]:14791"}, "ADMIN_URL"),
        ({"ADMIN_URL": "https://admin.example.test", "ADMIN_RP_ID": "other.test"}, "ADMIN_RP_ID"),
        ({"ADMIN_URL": "https://admin.example.test", "ADMIN_RP_ID": "203.0.113.1"}, "ADMIN_RP_ID"),
        ({"ADMIN_URL": "https://admin.example.test", "ADMIN_RP_ID": "xample.test"}, "ADMIN_RP_ID"),
    ],
)
def test_br_202_startup_refuses_an_rp_id_webauthn_cannot_use(
    env: dict[str, str], variable: str
) -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load({"PUBLIC_URL": "https://mcp.example.test", **env})
    assert err.value.variable == variable
    assert variable in str(err.value)


def test_br_202_parent_domain_rp_id_is_accepted() -> None:
    cfg = config.load(
        {
            "PUBLIC_URL": "https://mcp.example.test",
            "ADMIN_URL": "https://admin.example.test",
            "ADMIN_RP_ID": "Example.test",
        }
    )
    assert cfg.admin.rp_id == "example.test"
    assert cfg.admin.origin == "https://admin.example.test"
