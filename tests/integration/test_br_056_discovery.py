"""BR-056 — one authorization server per access point, discoverable (T14)."""

from __future__ import annotations

from tests.support import (
    Gateway,
    exchange,
    get_code,
    grant,
    issuer,
    mcp,
    org,
    refresh,
    resource,
    standard_tree,
    token_request,
)

A = "mpx/cairn/staging/dev"
B = "mpx/cairn/prod/dev"


def test_br_056_resource_metadata_names_its_own_issuer(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client() as client:
        for path in (A, B):
            r = client.get(f"/.well-known/oauth-protected-resource/o/{org()}/p/{path}/mcp")
            assert r.status_code == 200
            body = r.json()
            assert body["resource"] == resource(path)
            assert body["authorization_servers"] == [issuer(path)]
            assert body["scopes_supported"] == ["mcp"]


def test_br_056_server_metadata_rfc8414(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client() as client:
        r = client.get(f"/.well-known/oauth-authorization-server/o/{org()}/oauth/p/{A}")
    assert r.status_code == 200
    md = r.json()
    assert md["issuer"] == issuer(A)
    assert md["authorization_endpoint"] == f"{issuer(A)}/authorize"
    assert md["token_endpoint"] == f"{issuer(A)}/token"
    assert md["revocation_endpoint"] == f"{issuer(A)}/revoke"
    assert md["code_challenge_methods_supported"] == ["S256"]
    assert md["scopes_supported"] == ["mcp", "offline_access"]
    assert "none" in md["token_endpoint_auth_methods_supported"]
    assert md["client_id_metadata_document_supported"] is True
    assert "registration_endpoint" not in md  # DCR inactive by default (BR-061)


def test_br_056_urls_derive_from_public_url_not_host(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client() as client:
        r = client.get(
            f"/.well-known/oauth-authorization-server/o/{org()}/oauth/p/{A}",
            headers={"Host": "evil.example"},
        )
        rs = client.get(
            f"/.well-known/oauth-protected-resource/o/{org()}/p/{A}/mcp",
            headers={"Host": "evil.example"},
        )
    assert "evil" not in r.text
    assert "evil" not in rs.text


def test_br_056_unknown_paths_and_root_are_404(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client() as client:
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
            f"/.well-known/oauth-protected-resource/o/{org()}/p/mpx/cairn/staging/nope/mcp",
            f"/.well-known/oauth-authorization-server/o/{org()}/oauth/p/mpx/cairn",
            f"/o/{org()}/oauth/p/mpx/cairn/staging/dev/unknown",
            f"/o/{org()}/oauth/p/mpx/cairn/staging/nope/token",
        ):
            assert client.get(path).status_code == 404, path


def test_br_056_code_and_tokens_of_one_endpoint_refused_by_another(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        code, verifier = get_code(gw, client, A)
        foreign = token_request(
            client,
            B,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": "http://localhost:4567/callback",
                "client_id": "client-test",
                "resource": resource(B),
            },
        )
        assert (foreign.status_code, foreign.json()["error"]) == (400, "invalid_grant")
        assert exchange(client, A, code, verifier).status_code == 200
        g = grant(gw, client, A)
        assert mcp(client, B, g["access_token"]).status_code == 401
        assert mcp(client, A, g["access_token"]).status_code == 200
        other = token_request(
            client,
            B,
            {
                "grant_type": "refresh_token",
                "refresh_token": g["refresh_token"],
                "client_id": "client-test",
                "resource": resource(B),
            },
        )
        assert other.json()["error"] == "invalid_grant"
        assert refresh(client, A, g["refresh_token"]).status_code == 200


def test_br_056_public_clients_only_in_metadata(gw: Gateway) -> None:
    """S1 : `token_endpoint_auth_methods_supported` = [`none`]."""
    standard_tree(gw)
    with gw.client() as client:
        md = client.get(f"/.well-known/oauth-authorization-server/o/{org()}/oauth/p/{A}").json()
    assert md["token_endpoint_auth_methods_supported"] == ["none"]
    assert md["revocation_endpoint_auth_methods_supported"] == ["none"]
