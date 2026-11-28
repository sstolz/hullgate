"""BR-209, BR-235 (AC-08) — an optional IP allow-list closes the console, without locking its
author out, and the host can always clear it."""

from __future__ import annotations

import json
from pathlib import Path

from tests.admin_support import CLIENT_IP, Console, bundle
from tests.support import Gateway

OUTSIDE = [
    ("GET", "/admin"),
    ("GET", "/admin/assets/app.js"),
    ("GET", "/admin/api/v1/session"),
    ("POST", "/admin/api/v1/auth/options"),
    ("GET", "/admin/nowhere"),
    ("POST", "/admin/api/v1/nowhere"),
]


def test_br_209_empty_list_restricts_nothing(gw: Gateway) -> None:
    console = Console(gw)
    with console.client(address="192.0.2.50") as client:
        console.admin(client)
        listed = console.call(client, "GET", "allowlist").json()
    assert listed == {"cidrs": [], "client_address": "192.0.2.50"}


def test_br_209_non_empty_list_refuses_others_before_any_route(gw: Gateway, tmp_path: Path) -> None:
    console = Console(gw, static_dir=bundle(tmp_path))
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        put = console.call(
            client, "PUT", "allowlist", {"cidrs": ["203.0.113.0/24", "2001:db8::/32"]}
        )
        assert put.status_code == 200, put.text
        assert put.json()["cidrs"] == ["2001:db8::/32", "203.0.113.0/24"]
        assert console.call(client, "GET", "session").status_code == 200  # the author stays in
    with console.client(address="198.51.100.4") as outsider:
        for method, path in OUTSIDE:
            response = outsider.request(method, path, headers=console.headers(csrf=""))
            assert response.status_code == 403, (method, path)
        assert outsider.get("/healthz").status_code == 200  # not under /admin
    with console.client(address="2001:db8::5") as v6:
        assert v6.get("/admin/api/v1/session").status_code == 401  # inside: next check answers


def test_br_209_list_is_read_at_each_request_and_changes_are_audited(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        assert console.call(client, "PUT", "allowlist", {"cidrs": [CLIENT_IP]}).status_code == 200
        assert console.call(client, "PUT", "allowlist", {"cidrs": []}).status_code == 200
    changes = [e for e in gw.audit() if e["action"] == "admin.allowlist"]
    assert [(json.loads(e["before"]), json.loads(e["after"])) for e in changes] == [
        ({"cidrs": []}, {"cidrs": ["203.0.113.7/32"]}),
        ({"cidrs": ["203.0.113.7/32"]}, {"cidrs": []}),
    ]
    assert all(e["actor"].startswith("admin:adm_") for e in changes)


def test_br_209_invalid_cidr_is_refused(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        for body in ({"cidrs": ["not-a-cidr"]}, {"cidrs": "203.0.113.0/24"}, {}):
            response = console.call(client, "PUT", "allowlist", body)
            assert response.status_code == 422, body


def test_br_235_self_exclusion_is_refused(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        response = console.call(client, "PUT", "allowlist", {"cidrs": ["198.51.100.0/24"]})
        assert response.status_code == 409
        assert response.json()["code"] == "allowlist_self"
        assert CLIENT_IP in response.json()["message"]
        assert console.call(client, "GET", "allowlist").json()["cidrs"] == []


def test_br_235_host_clears_the_list(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        assert console.call(client, "PUT", "allowlist", {"cidrs": [CLIENT_IP]}).status_code == 200
    with console.client(address="198.51.100.4") as moved:  # the operator's address changed
        assert moved.get("/admin/api/v1/session").status_code == 403
        assert "allow-list cleared: 1 entries removed" in gw.ok("admin", "allowlist", "clear")
        assert moved.get("/admin/api/v1/session").status_code == 401
    last = [e for e in gw.audit() if e["action"] == "admin.allowlist"][-1]
    assert last["actor"] == "operator-cli"
