"""BR-055 — the authorization order at the access point is fixed and closed by default.

One case per step, each making exactly that step fail: the response is the step's, no later step
nor MCP processing takes place (the body is not a JSON-RPC response), and the audit reason is
distinct.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager

import pytest
from starlette.testclient import TestClient

from hullgate import tokens
from hullgate.authorization import Credential, Resolver
from tests.support import PUBLIC_URL, Gateway, mcp, org, standard_tree

ROLE_PATH = "mpx/cairn/staging/dev"


def _metadata() -> str:
    """RFC 9728 metadata URL of the role (F-014: under the drawn organisation identifier)."""
    return f"{PUBLIC_URL}/.well-known/oauth-protected-resource/o/{org()}/p/{ROLE_PATH}/mcp"


def _refusals(gw: Gateway) -> list[tuple[str, str]]:
    """(reason, detail) of each refusal; the client address every one carries since F-020
    (BR-313) is checked, then left out of the detail compared."""
    out: list[tuple[str, str]] = []
    for e in gw.audit():
        if e["action"] != "access.refused":
            continue
        detail = json.loads(e["detail"])
        assert detail.pop("address") == "testclient"
        out.append((e["reason"], json.dumps(detail)))
    return out


@pytest.fixture
def ready(gw: Gateway) -> tuple[Gateway, str]:
    standard_tree(gw)
    _, token = gw.issue("mpx/cairn/staging")
    return gw, token


def test_br_055_step1_unknown_path_404_before_authentication(
    ready: tuple[Gateway, str],
) -> None:
    gw, token = ready
    with gw.client() as client:
        for path in (
            "mpx/cairn/staging/nope",
            "mpx/cairn/staging",
            "x/y/z/w",
            "mpx/cairn/staging/dev%20",
            "mpx/cairn/staging/%20dev",
            "mpx/cairn/staging/Dev",
            "mpx/cairn/staging/dev-",
            "mpx/cairn/STAGING/dev",
        ):
            for bearer in (None, token):
                response = mcp(client, path, bearer)
                assert response.status_code == 404, (path, bearer)
                assert response.json() == {"error": "not_found"}
                assert "www-authenticate" not in response.headers
        for raw in ("/p/mpx/cairn/staging/dev", "/p/mpx/cairn/staging/dev/mcp/x", "/p//a/b/c/mcp"):
            assert client.post(raw, json={}).status_code == 404
    assert _refusals(gw) == []


def test_br_055_step2_store_unavailable_503_even_with_valid_token(
    ready: tuple[Gateway, str],
) -> None:
    gw, token = ready
    holder = sqlite3.connect(gw.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with gw.client() as client:
            locked = mcp(client, "mpx/cairn/staging/dev", token)
            missing = mcp(client, "mpx/cairn/staging/nope", token)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert locked.status_code == 503
    assert locked.json() == {"error": "temporarily_unavailable"}
    assert missing.status_code == 404  # step 1 played first: reading remains possible
    assert _refusals(gw) == []


def test_br_055_step3_missing_token_401_with_resource_metadata(
    ready: tuple[Gateway, str],
) -> None:
    gw, _ = ready
    with gw.client() as client:
        response = mcp(client, "mpx/cairn/staging/dev")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == f'Bearer resource_metadata="{_metadata()}"'
    assert "jsonrpc" not in response.text
    assert _refusals(gw) == [("missing_token", '{"step": 3}')]


@pytest.mark.parametrize(
    "bearer",
    ["hg_unknown", "not-a-token", "", "eyJhbGciOiJFUzI1NiJ9.e30.sig"],
)
def test_br_055_step4_unknown_or_malformed_token(ready: tuple[Gateway, str], bearer: str) -> None:
    gw, _ = ready
    with gw.client() as client:
        response = mcp(client, "mpx/cairn/staging/dev", bearer)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        f'Bearer error="invalid_token", resource_metadata="{_metadata()}"'
    )
    assert "jsonrpc" not in response.text
    assert _refusals(gw) == [("invalid_token", '{"step": 4}')]


def test_br_055_step4_expired_token(ready: tuple[Gateway, str]) -> None:
    gw, token = ready
    gw.clock.advance(31 * 86400)
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev", token).status_code == 401
    assert _refusals(gw) == [("invalid_token", '{"step": 4}')]


def test_br_055_step5_revoked_token(ready: tuple[Gateway, str]) -> None:
    gw, _ = ready
    token_id, token = gw.issue("mpx/cairn/staging/dev")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev", token).status_code == 200
        gw.ok("token", "revoke", token_id)  # another "process": seen on the next request
        response = mcp(client, "mpx/cairn/staging/dev", token)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_token"}
    assert _refusals(gw) == [("revoked", '{"step": 5}')]


def test_br_055_step5_token_of_deleted_node(ready: tuple[Gateway, str]) -> None:
    gw, _ = ready
    gw.ok("tree", "add", "mpx/cairn/staging", "role", "Tmp")
    _, token = gw.issue("mpx/cairn/staging/tmp")
    gw.ok("tree", "delete", "mpx/cairn/staging/tmp", "--confirm", "mpx/cairn/staging/tmp")
    gw.ok("tree", "add", "mpx/cairn/staging", "role", "Tmp")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/tmp", token).status_code == 401
    assert _refusals(gw) == [("node_deleted", '{"step": 5}')]


def test_br_055_step6_out_of_scope(ready: tuple[Gateway, str]) -> None:
    gw, _ = ready
    _, other = gw.issue("mpx/cairn/staging/principal")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev", other).status_code == 401
    assert _refusals(gw) == [("out_of_scope", '{"step": 6}')]


def test_br_055_step6_sensitive_requires_role_token(ready: tuple[Gateway, str]) -> None:
    gw, _ = ready
    _, project = gw.issue("mpx/cairn")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/prod/dev", project).status_code == 401
    assert _refusals(gw) == [("sensitive_requires_role_token", '{"step": 6}')]


def _resolver(scopes: frozenset[str], audience: str | None) -> Resolver:
    def resolve(conn: sqlite3.Connection, bearer: str) -> Credential | None:
        if bearer != "fake-oauth":
            return None
        role = conn.execute(
            "SELECT n.id FROM node n JOIN node e ON e.id = n.parent_id "
            "WHERE n.slug = 'dev' AND e.slug = 'staging' AND n.deleted = 0"
        ).fetchone()[0]
        return Credential(
            kind="oauth",
            id="jti-test",
            actor="client-test",
            node_id=role,
            expires_at=1e12,
            revoked=False,
            node_deleted=False,
            scopes=scopes,
            audience=audience,
        )

    return resolve


def _with(
    gw: Gateway, scopes: frozenset[str], audience: str | None
) -> AbstractContextManager[TestClient]:
    return gw.client(resolvers=(tokens.resolve_operator, _resolver(scopes, audience)))


def test_br_055_step6_oauth_audience_must_equal_endpoint(ready: tuple[Gateway, str]) -> None:
    """JWT branch (lot B) simulated by a resolver: the audience is compared exactly."""
    gw, _ = ready
    endpoint = f"{PUBLIC_URL}/o/{org()}/p/mpx/cairn/staging/dev/mcp"
    with _with(gw, frozenset({"mcp"}), endpoint + "/") as client:
        assert mcp(client, "mpx/cairn/staging/dev", "fake-oauth").status_code == 401
    with _with(gw, frozenset({"mcp"}), endpoint) as client:
        assert mcp(client, "mpx/cairn/staging/dev", "fake-oauth").status_code == 200
    assert _refusals(gw) == [("audience_mismatch", '{"step": 6}')]


def test_br_055_step7_insufficient_scope_403(ready: tuple[Gateway, str]) -> None:
    gw, _ = ready
    endpoint = f"{PUBLIC_URL}/o/{org()}/p/mpx/cairn/staging/dev/mcp"
    with _with(gw, frozenset({"offline_access"}), endpoint) as client:
        response = mcp(client, "mpx/cairn/staging/dev", "fake-oauth")
    assert response.status_code == 403
    assert response.headers["www-authenticate"] == (
        f'Bearer error="insufficient_scope", scope="mcp", resource_metadata="{_metadata()}"'
    )
    assert "jsonrpc" not in response.text
    assert _refusals(gw) == [("insufficient_scope", '{"step": 7}')]


def test_br_055_operator_tokens_carry_mcp_scope(ready: tuple[Gateway, str]) -> None:
    gw, token = ready
    with gw.client() as client:
        response = mcp(client, "mpx/cairn/staging/dev", token)
    assert response.status_code == 200
    assert response.json()["result"] == {"tools": []}
    assert _refusals(gw) == []


# --- JWT branch (lot B): key at step 2, JWT at step 4, audience at step 6 -----------------


def _jwt_setup(gw: Gateway) -> dict[str, str]:
    from tests.support import grant

    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        g = grant(gw, client, "mpx/cairn/staging/dev")
    return g


def test_br_055_jwt_accepted_on_its_endpoint(gw: Gateway) -> None:
    g = _jwt_setup(gw)
    with gw.client() as client:
        r = mcp(client, "mpx/cairn/staging/dev", g["access_token"])
    assert r.status_code == 200
    assert "result" in r.json()


def test_br_055_step2_key_unavailable_503_before_token(gw: Gateway) -> None:
    g = _jwt_setup(gw)
    gw.key_path.chmod(0o600)
    saved = gw.key_path.read_bytes()
    gw.key_path.unlink()
    try:
        with gw.client() as client:
            r = mcp(client, "mpx/cairn/staging/dev", g["access_token"])
            anonymous = mcp(client, "mpx/cairn/staging/dev")
    finally:
        gw.key_path.write_bytes(saved)
    assert r.status_code == 503
    assert anonymous.status_code == 503
    assert _refusals(gw) == []


def test_br_055_step5_revoked_jwt(gw: Gateway) -> None:
    g = _jwt_setup(gw)
    lineage = next(
        t["id"]
        for t in __import__("json").loads(gw.ok("token", "list", "--json"))
        if t["type"] == "oauth"
    )
    gw.ok("token", "revoke", lineage)
    with gw.client() as client:
        r = mcp(client, "mpx/cairn/staging/dev", g["access_token"])
    assert r.status_code == 401
    assert _refusals(gw)[-1] == ("revoked", '{"step": 5}')


def test_br_055_step6_audience_of_another_endpoint(gw: Gateway) -> None:
    g = _jwt_setup(gw)
    gw.ok("tree", "add", "mpx/cairn/staging", "role", "Reviewer")
    with gw.client() as client:
        r = mcp(client, "mpx/cairn/staging/reviewer", g["access_token"])
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert _refusals(gw)[-1] == ("audience_mismatch", '{"step": 6}')


def test_br_055_step7_jwt_without_mcp_scope_is_403(gw: Gateway) -> None:
    from tests.support import (
        authorize_params,
        code_from,
        consent,
        exchange,
        pkce,
        request_id,
        start_authorization,
    )

    standard_tree(gw)
    gw.preregister("mpx")
    path = "mpx/cairn/staging/dev"
    with gw.client() as client:
        verifier, challenge = pkce()
        first = start_authorization(
            client, path, authorize_params(path, challenge, scope="offline_access")
        )
        code = code_from(consent(gw, client, path, request_id(first)))
        token = exchange(client, path, code, verifier).json()["access_token"]
        r = mcp(client, path, token)
    assert r.status_code == 403
    assert 'error="insufficient_scope"' in r.headers["www-authenticate"]
    assert _refusals(gw)[-1] == ("insufficient_scope", '{"step": 7}')


def test_br_055_jwt_expired_after_one_hour(gw: Gateway) -> None:
    g = _jwt_setup(gw)
    gw.clock.advance(3601)
    with gw.client() as client:
        r = mcp(client, "mpx/cairn/staging/dev", g["access_token"])
    assert r.status_code == 401
    assert _refusals(gw)[-1] == ("invalid_token", '{"step": 4}')
