"""BR-061 — preregistered clients, or clients registered dynamically behind the barrier."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from tests.support import (
    REDIRECT,
    Gateway,
    authorize_params,
    code_from,
    consent,
    consent_page,
    exchange,
    grant,
    mcp,
    org,
    pkce,
    request_id,
    resource,
    revoke,
    standard_tree,
    start_authorization,
    token_request,
)

ROLE = "mpx/cairn/staging/dev"


def _codes(gw: Gateway) -> int:
    conn = gw.connect()
    try:
        return int(conn.execute("SELECT count(*) FROM auth_code").fetchone()[0])
    finally:
        conn.close()


def test_br_061_unknown_client_gets_error_page_never_a_redirect(gw: Gateway) -> None:
    standard_tree(gw)
    _, challenge = pkce()
    with gw.client() as client:
        r = start_authorization(client, ROLE, authorize_params(ROLE, challenge, "unknown"))
    assert r.status_code == 400
    assert "location" not in r.headers


def test_br_061_preregistered_client_admitted_in_its_subtree_only(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "add", "/", "client", "Acme")
    gw.ok("tree", "add", "acme", "project", "Api")
    gw.ok("tree", "add", "acme/api", "environment", "Dev")
    gw.preregister("mpx/cairn")
    _, challenge = pkce()
    with gw.client() as client:
        ok = start_authorization(client, ROLE, authorize_params(ROLE, challenge))
        assert ok.status_code == 302
        assert ok.headers["location"].endswith("/consent")
        other = "acme/api/dev/principal"
        out = start_authorization(client, other, authorize_params(other, challenge))
        assert out.status_code == 400
        assert "location" not in out.headers


def test_br_061_v2_client_preregistered_from_the_console(gw: Gateway) -> None:
    """BR-061 v2: the operator preregisters a client through the CLI or the console; the console's
    one is admitted exactly like the CLI's, and its refusals are the CLI's."""
    from tests.admin_support import Console
    from tests.support import CLIENT_ID

    standard_tree(gw)
    console = Console(gw)
    with console.client() as admin:
        console.admin(admin)
        bad = console.call(admin, "POST", "clients", {
            "client_id": CLIENT_ID, "path": "mpx", "redirect_uris": ["http://example.com/cb"],
        })  # fmt: skip
        assert bad.status_code == 409
        assert bad.json()["code"] == "refused"
        added = console.call(admin, "POST", "clients", {
            "client_id": CLIENT_ID, "path": "mpx/cairn", "redirect_uris": [REDIRECT],
        })  # fmt: skip
        assert added.status_code == 201, added.text
        listed = console.call(admin, "GET", "clients").json()
    assert [c["client_id"] for c in listed["clients"]] == [CLIENT_ID]
    assert listed["dcr"] is False
    _, challenge = pkce()
    with gw.client() as client:
        ok = start_authorization(client, ROLE, authorize_params(ROLE, challenge))
    assert ok.status_code == 302
    assert ok.headers["location"].endswith("/consent")


def test_br_061_redirect_uri_compared_exactly(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    _, challenge = pkce()
    with gw.client() as client:
        for uri in (REDIRECT + "/x", REDIRECT + "?a=1", "http://localhost:4567/"):
            r = start_authorization(client, ROLE, authorize_params(ROLE, challenge, redirect=uri))
            assert r.status_code == 400, uri
            assert "location" not in r.headers, uri


def test_br_061_client_add_refuses_insecure_redirect(gw: Gateway) -> None:
    standard_tree(gw)
    for uri in ("http://example.com/cb", "ftp://localhost/cb", "https://x.test/cb#f", "cb"):
        r = gw.cli("client", "add", "c1", "--path", "mpx", "--redirect-uri", uri)
        assert r.code == 2, uri
    assert (
        gw.cli(
            "client",
            "add",
            "c1",
            "--path",
            "mpx",
            "--redirect-uri",
            "https://claude.ai/api/mcp/auth_callback",
        ).code
        == 0
    )
    assert gw.cli("client", "add", "c1", "--path", "mpx", "--redirect-uri", REDIRECT).code == 2
    listed = json.loads(gw.ok("client", "list", "--json"))
    assert [(c["client_id"], c["path"], c["state"]) for c in listed] == [("c1", "mpx", "active")]


def test_br_061_unknown_client_at_token_and_revoke_is_invalid_client(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client() as client:
        t = token_request(
            client,
            ROLE,
            {
                "grant_type": "refresh_token",
                "refresh_token": "x",
                "client_id": "unknown",
                "resource": resource(ROLE),
            },
        )
        assert (t.status_code, t.json()["error"]) == (401, "invalid_client")
        v = revoke(client, ROLE, {"token": "x", "client_id": "unknown"})
        assert (v.status_code, v.json()["error"]) == (401, "invalid_client")


def test_br_061_dcr_inactive_by_default(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client() as client:
        r = client.post(f"/o/{org()}/oauth/p/{ROLE}/register", json={"redirect_uris": [REDIRECT]})
    assert r.status_code == 404


def test_br_061_dcr_client_bound_to_its_endpoint_and_behind_the_barrier(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("client", "dcr", "on")
    other = "mpx/cairn/prod/dev"
    with gw.client() as client:
        md = client.get(f"/.well-known/oauth-authorization-server/o/{org()}/oauth/p/{ROLE}").json()
        assert md["registration_endpoint"].endswith(f"/o/{org()}/oauth/p/{ROLE}/register")
        r = client.post(
            f"/o/{org()}/oauth/p/{ROLE}/register",
            json={"redirect_uris": [REDIRECT], "client_name": "banc"},
        )
        assert r.status_code == 201
        cid = r.json()["client_id"]
        assert "client_secret" not in r.json()
        verifier, challenge = pkce()
        first = start_authorization(client, ROLE, authorize_params(ROLE, challenge, cid))
        assert first.headers["location"].endswith("/consent")  # the barrier, never a code
        page = consent_page(client, ROLE, request_id(first))
        assert "banc" in page.text
        code = code_from(consent(gw, client, ROLE, request_id(first)))
        assert exchange(client, ROLE, code, verifier, client_id=cid).status_code == 200
        elsewhere = start_authorization(client, other, authorize_params(other, challenge, cid))
        assert elsewhere.status_code == 400
    gw.ok("client", "dcr", "off")
    with gw.client() as client:
        assert client.post(f"/o/{org()}/oauth/p/{ROLE}/register", json={}).status_code == 404


def test_br_061_public_clients_only_secret_ignored_never_issued(gw: Gateway) -> None:
    """Operator decision: public clients; DCR without a secret, a presented secret ignored."""
    standard_tree(gw)
    gw.ok("client", "dcr", "on")
    with gw.client() as client:
        md = client.get(f"/.well-known/oauth-authorization-server/o/{org()}/oauth/p/{ROLE}").json()
        assert md["token_endpoint_auth_methods_supported"] == ["none"]
        r = client.post(
            f"/o/{org()}/oauth/p/{ROLE}/register",
            json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post"},
        )
        assert r.status_code == 201
        body = r.json()
        assert "client_secret" not in body
        assert body["token_endpoint_auth_method"] == "none"  # noqa: S105
        cid = body["client_id"]
        code, verifier = _code_for(gw, client, cid)
        ignored = exchange(client, ROLE, code, verifier, client_id=cid, client_secret="quelconque")  # noqa: S106
        assert ignored.status_code == 200
    registered = [e for e in gw.audit() if e["action"] == "oauth.client.register"]
    assert [e["actor"] for e in registered] == ["anonymous"]
    conn = gw.connect()
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(client)")]
    finally:
        conn.close()
    assert not [c for c in columns if "secret" in c]


def _code_for(gw: Gateway, client: object, cid: str) -> tuple[str, str]:
    from starlette.testclient import TestClient

    assert isinstance(client, TestClient)
    verifier, challenge = pkce()
    first = start_authorization(client, ROLE, authorize_params(ROLE, challenge, cid))
    return code_from(consent(gw, client, ROLE, request_id(first))), verifier


def test_br_061_client_remove_revokes_its_lineages(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        g = grant(gw, client, ROLE)
        assert mcp(client, ROLE, g["access_token"]).status_code == 200
        out = gw.ok("client", "remove", "client-test")
        assert "OAuth lineages revoked: 1" in out
        assert mcp(client, ROLE, g["access_token"]).status_code == 401
        _, challenge = pkce()
        again = start_authorization(client, ROLE, authorize_params(ROLE, challenge))
        assert again.status_code == 400
    assert any(e["reason"] == "client_removed" for e in gw.audit())


def test_br_061_query_error_redirect_keeps_state(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    _, challenge = pkce()
    with gw.client() as client:
        r = start_authorization(
            client, ROLE, authorize_params(ROLE, challenge, response_type="token", state="s1")
        )
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["error"] == ["unsupported_response_type"]
    assert query["state"] == ["s1"]
    assert _codes(gw) == 0


def test_br_061_cimd_client_row_has_no_node_and_serves_two_roles(gw: Gateway) -> None:
    """S9: a CIMD client is bound to no node; its lineage binds it to the access point."""
    standard_tree(gw)
    conn = gw.connect()
    try:
        conn.execute(
            "INSERT INTO client (client_id, kind, node_id, metadata, created_at) VALUES"
            " ('https://c.example/doc', 'cimd', NULL, '{}', 0)"
        )
        conn.execute(
            "INSERT INTO lineage (id, role_id, client_id, resource, scope, created_at, expires_at)"
            " SELECT 'l_' || id, id, 'https://c.example/doc', 'x', 'mcp', 0, 1 FROM node"
            " WHERE slug = 'dev'"
        )
        conn.commit()
        rows = conn.execute("SELECT node_id FROM client WHERE kind = 'cimd'").fetchall()
        import sqlite3 as sq

        try:
            conn.execute(
                "INSERT INTO client (client_id, kind, node_id, metadata, created_at) VALUES"
                " ('https://c.example/doc', 'cimd', NULL, '{}', 0)"
            )
            duplicate = False
        except sq.IntegrityError:
            duplicate = True
    finally:
        conn.close()
    assert rows == [(None,)]
    assert duplicate
    listed = json.loads(gw.ok("client", "list", "--json"))
    assert [c["path"] for c in listed if c["kind"] == "cimd"] == ["(metadata document)"]


def test_br_061_same_client_preregistered_on_two_nodes(gw: Gateway) -> None:
    """Key (client_id, node_id): the same client preregistered on two nodes."""
    standard_tree(gw)
    gw.ok("tree", "add", "/", "client", "Acme")
    gw.ok("tree", "add", "acme", "project", "Api")
    gw.ok("tree", "add", "acme/api", "environment", "Dev")
    gw.preregister("mpx/cairn/staging")
    gw.preregister("acme")
    again = gw.cli("client", "add", "client-test", "--path", "acme", "--redirect-uri", REDIRECT)
    assert again.code == 2
    _, challenge = pkce()
    with gw.client() as client:
        for path in (ROLE, "acme/api/dev/principal"):
            r = start_authorization(client, path, authorize_params(path, challenge))
            assert r.headers["location"].endswith("/consent"), path
        sibling = start_authorization(
            client, "mpx/cairn/prod/dev", authorize_params("mpx/cairn/prod/dev", challenge)
        )
    assert sibling.status_code == 400
    out = gw.ok("client", "remove", "client-test")
    assert "client removed" in out
    assert [c["state"] for c in json.loads(gw.ok("client", "list", "--json"))] == ["removed"] * 2
