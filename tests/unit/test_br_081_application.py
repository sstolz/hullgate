"""BR-081 — an outbound OAuth application belongs to the organisation and serves several
connections (AC-10)."""

from __future__ import annotations

import json

from tests.support import REGISTRY, Gateway, standard_tree


def _app(gw: Gateway, *extra: str, stdin: str = "") -> None:
    gw.ok(
        "application", "declare", "gitea-app", "--service", "gitea-dev", "--mode", "declared",
        "--client-id", "client-123", *extra, stdin=stdin,
    )  # fmt: skip


def _oauth(gw: Gateway, name: str, prefix: str) -> None:
    gw.ok(
        "connection", "add", name, "--service", "gitea-dev", "--application", "gitea-app",
        "--prefix", prefix,
    )  # fmt: skip


def test_br_081_application_serves_several_connections_and_removal_guard(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    _app(gw)
    _oauth(gw, "mpx/gitea-dev", "gd")
    _oauth(gw, "mpx/gitea-rev", "gr")  # same application, two connections (spec l. 145-146)
    states = json.loads(gw.ok("connection", "status", "--json"))
    assert {s["application"] for s in states} == {"gitea-app"}
    assert {s["state"] for s in states} == {"pending"}
    refused = gw.cli("application", "remove", "gitea-app")
    assert refused.code == 2
    assert "mpx/gitea-dev, mpx/gitea-rev" in refused.err
    gw.ok("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
    assert gw.cli("application", "remove", "gitea-app").code == 2
    gw.ok("connection", "remove", "mpx/gitea-rev", "--confirm", "mpx/gitea-rev")
    gw.ok("application", "remove", "gitea-app")
    assert json.loads(gw.ok("application", "list", "--json")) == []
    assert [e["action"] for e in gw.audit()].count("application.remove") == 1


def test_br_081_declaration_fields_and_redirect_uri(gw: Gateway) -> None:
    gw.service()
    secret = "s3cret-" + "q" * 20
    REGISTRY.secrets.append(secret)
    out = gw.ok(
        "application", "declare", "gitea-app", "--service", "gitea-dev", "--mode", "declared",
        "--client-id", "client-123", "--secret-file", "-", stdin=secret + "\n",
    )  # fmt: skip
    assert "http://127.0.0.1/oauth/callback" in out
    [app] = json.loads(gw.ok("application", "list", "--json"))
    assert app == {
        "id": "gitea-app",
        "service": "gitea-dev",
        "mode": "declared",
        "client_id": "client-123",
        "redirect_uri": "http://127.0.0.1/oauth/callback",
        "secret": "encrypted",
        "secret_expires_at": None,
        "method": "client_secret_basic",
    }
    # duplicate, unknown service, application of another service
    assert gw.cli(
        "application", "declare", "gitea-app", "--service", "gitea-dev", "--mode", "declared",
        "--client-id", "x",
    ).code == 2  # fmt: skip
    assert gw.cli(
        "application", "declare", "a2", "--service", "absent", "--mode", "declared",
        "--client-id", "x",
    ).code == 2  # fmt: skip
    gw.service("other", "generic")
    standard_tree(gw)
    result = gw.cli(
        "connection", "add", "mpx/x", "--service", "other", "--application", "gitea-app",
        "--prefix", "xx",
    )  # fmt: skip
    assert result.code == 2
    assert "declared for the service gitea-dev" in result.err


def test_br_081_service_removal_refused_while_referenced(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    _app(gw)
    assert gw.cli("service", "remove", "gitea-dev").code == 2
    gw.ok("application", "remove", "gitea-app")
    gw.static("mpx/gitea-dev", "gd")
    refused = gw.cli("service", "remove", "gitea-dev")
    assert refused.code == 2
    assert "mpx/gitea-dev" in refused.err
    gw.ok("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
    gw.ok("service", "remove", "gitea-dev")
