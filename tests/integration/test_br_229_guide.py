"""AC-30: server-derived steps and dismissal are independent of ordinary domain acts."""

from typing import Any

from tests.admin_support import Console
from tests.policy_support import rows
from tests.support import Gateway


def test_br_229_steps_follow_state_and_only_dismissal_writes(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)

        def read() -> dict[str, Any]:
            response = console.call(client, "GET", "setup")
            assert response.status_code == 200, response.text
            value: dict[str, Any] = response.json()
            return value

        assert [s["done"] for s in read()["steps"]] == [False] * 5
        targets = {step["id"]: step["url"] for step in read()["steps"]}
        assert targets == {
            "client": "/admin/tree?setup=client",
            "service": "/admin/tree?setup=service",
            "connection": "/admin/tree?setup=connection",
            "placement": "/admin/tree?setup=placement",
            "credential": "/admin/tokens?setup=credential",
        }
        assert len(set(targets.values())) == 5
        assert read()["show"]
        gw.build(("/", "client", "Mpx"))
        assert [s["done"] for s in read()["steps"]] == [True, False, False, False, False]
        gw.service()
        assert read()["services"][0]["scopes"] == ["write:issue", "openid"]
        gw.static()
        gw.build(("mpx", "project", "App"), ("mpx/app", "environment", "Dev"))
        gw.ok("connection", "place", "mpx/gitea-dev", "mpx/app/dev/principal")
        assert [s["done"] for s in read()["steps"]] == [True] * 4 + [False]
        gw.ok("token", "create", "--path", "mpx/app/dev/principal", "--label", "test")
        assert all(s["done"] for s in read()["steps"])
        assert not read()["show"]
        before = (
            rows(gw, "SELECT * FROM node"),
            rows(gw, "SELECT * FROM connection"),
            rows(gw, "SELECT * FROM setting"),
        )
        assert console.call(client, "POST", "setup/dismiss").status_code == 200
        assert read()["dismissed"]
        assert before == (
            rows(gw, "SELECT * FROM node"),
            rows(gw, "SELECT * FROM connection"),
            rows(gw, "SELECT * FROM setting"),
        )
        assert all(s["url"].startswith("/admin/") for s in read()["steps"])


def test_br_228_organisation_contains_account_and_configured_urls(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        response = console.call(client, "GET", "organisation")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["passkeys"] == 1
        assert data["last_sign_in"] is not None
        assert data["administrator"]["email"] == "ops@example.test"
        assert data["connection_expiry_days"] == 7
        assert data["admin_url"] == console.origin.origin
