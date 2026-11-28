"""BR-095 — `revoke` cuts access without losing the placements (AC-24); under the BR-089 lease."""

from __future__ import annotations

import json
import threading
import time

import pytest

from hullgate import connections, lifecycle
from tests import fake_provider
from tests.fake_provider import Reply
from tests.support import Gateway, standard_tree

ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"


def _ready(gw: Gateway) -> fake_provider.Provider:
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    return provider


def test_br_095_revoke_keeps_name_prefix_placements(gw: Gateway) -> None:
    provider = _ready(gw)
    tokens = gw.tokens(NAME)
    assert tokens is not None
    out = gw.ok("connection", "revoke", NAME)
    assert "revocation at the provider: revoked" in out
    assert {(h, t) for h, _c, t in provider.revoked} == {
        ("refresh_token", tokens["refresh_token"]),
        ("access_token", tokens["access_token"]),
    }
    row = gw.row(NAME)
    assert (row["state"], row["tokens_enc"], row["prefix"]) == ("reauth_required", None, "gd")
    [view] = json.loads(gw.ok("connection", "status", NAME, "--json"))
    assert view["placements"] == [{"role": ROLE, "sensitive": False}]
    with pytest.raises(connections.ReauthRequired):
        gw.access(NAME, ROLE)
    entry = [e for e in gw.audit() if e["action"] == "connection.revoke"][-1]
    assert json.loads(entry["after"])["state"] == "reauth_required"


def test_br_095_provider_down_audited_state_still_changes(gw: Gateway) -> None:
    provider = _ready(gw)
    provider.revoke_status = 500  # provider down
    out = gw.ok("connection", "revoke", NAME)
    assert "failed (HTTP 500)" in out
    assert provider.calls(fake_provider.REVOKE)
    assert gw.row(NAME)["state"] == "reauth_required"
    entry = [e for e in gw.audit() if e["action"] == "connection.upstream_revoke"][-1]
    assert entry["result"] == "failed"


def test_br_095_revoke_waits_for_renewal_then_revokes_newest(gw: Gateway) -> None:
    provider = _ready(gw)
    gw.expire(NAME, 30)
    provider.script.append(Reply(normal=True, delay=2.0))
    got: list[object] = []
    renewal = threading.Thread(target=lambda: got.append(gw.access(NAME, ROLE)))
    renewal.start()
    time.sleep(0.5)
    started = time.monotonic()
    gw.ok("connection", "revoke", NAME)
    assert time.monotonic() - started >= 1.0  # waited for the end of the renewal
    renewal.join()
    assert isinstance(got[0], connections.Access)
    assert gw.row(NAME)["tokens_enc"] is None
    newest_refresh = list(provider.refresh_tokens)[-1]
    assert ("refresh_token", newest_refresh) in {(h, t) for h, _c, t in provider.revoked}


def test_br_095_late_renewal_result_discarded_and_revoked(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ready(gw)
    monkeypatch.setattr(lifecycle, "LEASE_S", 1.0)
    gw.expire(NAME, 30)
    provider.script.append(Reply(normal=True, delay=3.0))
    errors: list[BaseException] = []

    def renew() -> None:
        try:
            gw.access(NAME, ROLE)
        except connections.AccessError as exc:
            errors.append(exc)

    renewal = threading.Thread(target=renew)
    renewal.start()
    time.sleep(0.3)
    gw.ok("connection", "revoke", NAME)  # overrides the expired lease (end + 1 s)
    renewal.join()
    assert isinstance(errors[0], connections.TemporarilyUnavailable)
    row = gw.row(NAME)
    assert (row["state"], row["tokens_enc"]) == ("reauth_required", None)
    late = list(provider.refresh_tokens)[-1]  # issued by the late renewal
    assert ("refresh_token", late) in {(h, t) for h, _c, t in provider.revoked}


def test_br_095_revoke_under_lock_accepted(gw: Gateway) -> None:
    _ready(gw)
    gw.ok("tree", "lock", "mpx/cairn")
    gw.ok("connection", "revoke", NAME)
    assert gw.row(NAME)["state"] == "reauth_required"
    assert gw.cli("connection", "remove", NAME, "--confirm", NAME).code == 2


def test_br_095_and_088_revoke_without_key_erases_locally(gw: Gateway) -> None:
    """AC-17, `revoke` clause: without the key, tokens erased, no upstream revocation, audit."""
    provider = _ready(gw)
    gw.connections_key_path.chmod(0o600)
    gw.connections_key_path.unlink()
    out = gw.ok("connection", "revoke", NAME)
    assert "key_unavailable" in out
    assert gw.row(NAME)["tokens_enc"] is None
    assert provider.calls(fake_provider.REVOKE) == []
    entry = [e for e in gw.audit() if e["action"] == "connection.upstream_revoke"][-1]
    assert (entry["result"], entry["reason"]) == ("skipped", "key_unavailable")


def test_br_095_remove_revokes_upstream(gw: Gateway) -> None:
    provider = _ready(gw)
    tokens = gw.tokens(NAME)
    assert tokens is not None
    out = gw.ok("connection", "remove", NAME, "--confirm", NAME)
    assert "revocation at the provider: revoked" in out
    assert tokens["refresh_token"] in {t for _h, _c, t in provider.revoked}
