"""BR-066 — the audit records issuances, revocations, refusals and tree changes, without secret.

The sweep (AC-06) reads back every audit entry of the databases created by the whole suite
(`tests.support.REGISTRY`, filled by the fixtures) and searches them for each generated token
value and its full fingerprint; it runs last (marker `audit_sweep`).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

import pytest

from tests.support import REGISTRY, Gateway, mcp, standard_tree

UTC_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def _flow(gw: Gateway) -> list[str]:
    """A run that produces each family of entry; returns the issued values."""
    standard_tree(gw)
    gw.ok("settings", "set", "mpx", "token_duration", "3d")
    tid, project = gw.issue("mpx/cairn")
    _, role = gw.issue("mpx/cairn/prod/dev")
    with gw.client() as client:
        mcp(client, "mpx/cairn/prod/dev")
        mcp(client, "mpx/cairn/prod/dev", project)
        mcp(client, "mpx/cairn/prod/dev", "hg_unknown")
        mcp(client, "mpx/cairn/prod/dev", role)
    gw.ok("token", "revoke", tid)
    gw.ok("tree", "rename", "mpx/cairn/staging", "Préprod", "--slug", "preprod")
    gw.ok("tree", "delete", "mpx/cairn/preprod", "--confirm", "mpx/cairn/preprod")
    return [project, role]


def test_br_066_entries_carry_required_fields(gw: Gateway) -> None:
    _flow(gw)
    entries = gw.audit()
    actions = {e["action"] for e in entries}
    assert {
        "tree.add",
        "tree.sensitive",
        "tree.rename",
        "tree.delete",
        "settings.set",
        "token.issue",
        "token.revoke",
        "access.refused",
    } <= actions
    for e in entries:
        assert UTC_RE.match(e["at"]), e
        for key in ("actor", "action", "result", "node_id", "path"):
            assert e[key], (key, e)
    refused = [e for e in entries if e["action"] == "access.refused"]
    assert [e["reason"] for e in refused] == [
        "missing_token",
        "sensitive_requires_role_token",
        "invalid_token",
    ]
    assert refused[0]["actor"] == "anonymous"
    assert refused[1]["actor"].startswith("tok_")
    changes = [e for e in entries if e["action"] in ("tree.rename", "settings.set")]
    assert all(e["before"] and e["after"] for e in changes)


def test_br_066_audit_is_append_only(gw: Gateway) -> None:
    _flow(gw)
    conn = sqlite3.connect(gw.path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="ajout seul"):
            conn.execute("UPDATE audit SET result = 'x'")
        with pytest.raises(sqlite3.DatabaseError, match="ajout seul"):
            conn.execute("DELETE FROM audit")
    finally:
        conn.close()


def test_br_066_refusal_served_even_if_audit_write_fails(gw: Gateway) -> None:
    standard_tree(gw)
    conn = sqlite3.connect(gw.path)
    conn.execute(
        "CREATE TRIGGER test_audit_fails BEFORE INSERT ON audit "
        "BEGIN SELECT RAISE(ABORT, 'failure'); END"
    )
    conn.commit()
    conn.close()
    with gw.client() as client:
        response = mcp(client, "mpx/cairn/staging/dev")
    assert response.status_code == 401


def _scan(paths: list[str], secrets: list[str]) -> list[str]:
    needles = [(s, "value") for s in secrets]
    needles += [(hashlib.sha256(s.encode()).hexdigest(), "fingerprint") for s in secrets]
    leaks: list[str] = []
    for path in paths:
        conn = sqlite3.connect(path)
        try:
            for row in conn.execute("SELECT * FROM audit"):
                text = "\x1f".join("" if v is None else str(v) for v in row)
                leaks.extend(f"{path} seq={row[0]}: {kind}" for n, kind in needles if n in text)
        finally:
            conn.close()
    return leaks


@pytest.mark.audit_sweep
def test_br_066_sweep_finds_no_secret_in_any_audit_entry(gw: Gateway) -> None:
    secrets = _flow(gw)
    all_secrets = sorted(set(REGISTRY.secrets) | set(secrets))
    paths = sorted({str(p) for p in REGISTRY.databases if p.exists()})
    entries = 0
    for path in paths:
        conn = sqlite3.connect(path)
        try:
            entries += int(conn.execute("SELECT count(*) FROM audit").fetchone()[0])
        finally:
            conn.close()
    assert all_secrets
    assert entries
    assert _scan(paths, all_secrets) == []


def test_br_066_no_entry_when_nothing_changes(gw: Gateway) -> None:
    standard_tree(gw)  # prod already sensitive
    gw.ok("settings", "set", "mpx", "token_duration", "3d")
    before = len(gw.audit())
    gw.ok("tree", "sensitive", "mpx/cairn/prod", "on")
    gw.ok("tree", "sensitive", "mpx/cairn/staging", "off")
    gw.ok("settings", "set", "mpx", "token_duration", "3d")
    gw.ok("settings", "set", "/", "token_duration", "30d")
    gw.ok("tree", "rename", "mpx", "MPX")
    gw.ok("tree", "rename", "mpx", "MPX", "--slug", "mpx")
    assert len(gw.audit()) == before
    gw.ok("tree", "sensitive", "mpx/cairn/staging", "on")
    assert len(gw.audit()) == before + 1


def test_br_066_oauth_issuance_refusals_and_barrier_audited(gw: Gateway) -> None:
    from tests.support import consent, grant, pkce, refresh, request_id, revoke

    standard_tree(gw)
    gw.preregister("mpx")
    path = "mpx/cairn/staging/dev"
    from tests.support import authorize_params, start_authorization

    with gw.client() as client:
        g = grant(gw, client, path)
        refresh(client, path, "unknown")
        _, challenge = pkce()
        req = request_id(start_authorization(client, path, authorize_params(path, challenge)))
        consent(gw, client, path, req, code="000001")
        revoke(client, path, {"token": g["refresh_token"], "client_id": "client-test"})
        start_authorization(client, path, authorize_params(path, challenge, "unknown"))
    entries = gw.audit()
    seen = {(e["action"], e["result"], e["reason"]) for e in entries}
    assert ("oauth.code.issue", "ok", None) in seen
    assert ("oauth.token.issue", "ok", None) in seen
    assert ("oauth.token", "refused", "invalid_grant") in seen
    assert ("oauth.authorize", "refused", "unknown_client") in seen
    assert ("lineage.revoke", "ok", "revoked_by_client") in seen
    assert any(a == "oauth.consent" and r in ("consent_failed",) for a, _, r in seen) or any(
        a == "oauth.code.issue" for a, _, _ in seen
    )
    for e in entries:
        assert e["at"].endswith("Z")
        assert e["actor"]
        if e["action"].startswith("oauth.") and e["node_id"]:
            assert e["path"] == path
    dump = repr(entries)
    for value in (g["access_token"], g["refresh_token"]):
        assert value not in dump
        assert hashlib.sha256(value.encode()).hexdigest() not in dump


def test_br_066_client_add_and_remove_audited_without_secret(gw: Gateway) -> None:
    """S6: client registration and removal audited (actor operator-cli), without secret."""
    standard_tree(gw)
    gw.ok("client", "add", "c1", "--path", "mpx", "--redirect-uri", "http://localhost:1/cb")
    gw.ok("client", "remove", "c1")
    entries = {e["action"]: e for e in gw.audit() if e["action"].startswith("client.")}
    assert entries["client.add"]["actor"] == "operator-cli"
    assert '"client_id": "c1"' in entries["client.add"]["after"]
    assert entries["client.remove"]["actor"] == "operator-cli"
    assert '"client_id": "c1"' in entries["client.remove"]["before"]
    assert "secret" not in repr(entries)


def test_br_066_v2_library_events_one_entry_each_without_secret(gw: Gateway) -> None:
    """F-006 (BR-066 v2, lot A): declaration of a service and of an application, creation, value
    replacement, prefix, placement, sharing confirmation, tree signal, placement removal, removal
    of a connection, skipped revocation at the provider."""
    standard_tree(gw)
    gw.ok("tree", "sensitive", "mpx/cairn/prod", "off")
    gw.service()
    secret = "audit-app-secret-" + "x" * 16
    REGISTRY.secrets.append(secret)
    gw.ok("application", "declare", "app", "--service", "gitea-dev", "--mode", "declared",
          "--client-id", "c", "--secret-file", "-", stdin=secret)  # fmt: skip
    value = gw.static("mpx/gitea-dev", "gd")
    replaced = "audit-replaced-" + "y" * 16
    REGISTRY.secrets.append(replaced)
    gw.ok("connection", "reauthorize", "mpx/gitea-dev", "--file", "-", stdin=replaced)
    gw.ok("connection", "prefix", "mpx/gitea-dev", "gx")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/prod/dev")
    gw.ok("tree", "sensitive", "mpx/cairn/prod", "on")
    gw.ok("tree", "add", "/", "client", "Acme")
    gw.ok("tree", "add", "acme", "project", "Site")
    gw.ok("tree", "add", "acme/site", "environment", "Dev")
    gw.ok("connection", "place", "mpx/gitea-dev", "acme/site/dev/principal", "--confirm-share")
    gw.ok("connection", "unplace", "mpx/gitea-dev", "acme/site/dev/principal")
    gw.ok("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
    gw.ok("application", "remove", "app")
    gw.ok("service", "remove", "gitea-dev")
    entries = gw.audit()
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["action"]] = counts.get(e["action"], 0) + 1
    expected = {
        "service.declare": 1,
        "application.declare": 1,
        "connection.add": 1,
        "connection.replace_value": 1,
        "connection.prefix": 1,
        "connection.place": 3,
        "connection.share_signal": 1,
        "connection.share_confirmed": 1,
        "connection.unplace": 1,
        "connection.remove": 1,
        "connection.upstream_revoke": 1,
        "application.remove": 1,
        "service.remove": 1,
    }
    assert {k: counts.get(k, 0) for k in expected} == expected
    for e in entries:
        assert UTC_RE.match(e["at"])
        text = "\x1f".join("" if v is None else str(v) for v in e.values())
        for needle in (value, replaced, secret):
            assert needle not in text
            assert hashlib.sha256(needle.encode()).hexdigest() not in text


def test_br_066_v2_lot_b_events_one_entry_each_without_secret(gw: Gateway) -> None:
    """F-006 (BR-066 v2, lot B): registration and reregistration of an application, opening and
    refusal of an authorization, authorization (account and scopes before and after), outcome of a
    renewal, refusal signal, revocation and upstream revocation, rediscovery, key unavailable; no
    value of a token, a code, a callback URL or a provider body."""
    from tests.fake_provider import Reply

    standard_tree(gw)
    provider = gw.provider()
    gw.service()
    gw.ok("application", "declare", "dyn", "--service", "gitea-dev", "--mode", "dynamic")
    url = gw.open_oauth("mpx/gitea-dev", "gd", "gitea-dev", "dyn")
    callback = provider.approve(url)
    forged = callback.replace("state=", "state=x")
    assert gw.cli("connection", "authorize", "mpx/gitea-dev", "--callback", "-", stdin=forged).code
    gw.ok("connection", "authorize", "mpx/gitea-dev", "--callback", "-", stdin=callback)
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok("connection", "reauthorize", "mpx/gitea-dev")
    gw.ok("connection", "renew", "mpx/gitea-dev")
    provider.script.append(Reply(status=503, body={"error_description": "TRAP-503"}))
    gw.expire("mpx/gitea-dev", 30)
    with pytest.raises(Exception, match="access_temporarily_unavailable"):
        gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.signal("mpx/gitea-dev", 2)
    gw.ok("connection", "revoke", "mpx/gitea-dev")
    gw.ok("service", "rediscover", "gitea-dev", "--confirm", "gitea-dev")
    gw.ok("application", "reregister", "dyn", "--confirm", "dyn")
    entries = gw.audit()
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["action"]] = counts.get(e["action"], 0) + 1
    expected = {
        "service.declare": 1,
        "service.rediscover": 1,
        "application.register": 1,
        "application.reregister": 1,
        "connection.add": 1,
        "connection.authorize_refused": 1,
        "connection.authorize": 1,
        "connection.authorize_open": 1,
        "connection.renew": 2,
        "connection.refusal_signal": 1,
        "connection.revoke": 1,
        "connection.upstream_revoke": 1,
    }
    assert {k: counts.get(k, 0) for k in expected} == expected
    authorize = next(e for e in entries if e["action"] == "connection.authorize")
    assert '"account": "cairn-ai"' in authorize["after"]
    assert "scopes" in authorize["before"]
    secrets_ = [*provider.access_tokens, *provider.refresh_tokens, *provider.codes, callback]
    secrets_ += [c.form["code_verifier"] for c in provider.journal if "code_verifier" in c.form]
    for e in entries:
        text = "\x1f".join("" if v is None else str(v) for v in e.values())
        assert "TRAP" not in text
        assert "http://127.0.0.1/oauth/callback?" not in text
        for needle in secrets_:
            assert needle not in text
            assert hashlib.sha256(needle.encode()).hexdigest() not in text
