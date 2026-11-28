"""BR-090 — the new token is stored before it is returned (AC-19): write failing until the
end of the lease, reply cut after consumption, write blocked then released."""

from __future__ import annotations

import sqlite3
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
    gw.expire(NAME, 30)
    return provider


def _hold_lock(gw: Gateway, after: float, duration: float) -> threading.Thread:
    """A second connection holds the write lock from `after` to `after + duration`."""

    def hold() -> None:
        time.sleep(after)
        conn = sqlite3.connect(gw.path, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        time.sleep(duration)
        conn.execute("ROLLBACK")
        conn.close()

    thread = threading.Thread(target=hold)
    thread.start()
    return thread


def _unchanged(before: dict[str, object], after: dict[str, object]) -> None:
    for column in ("state", "tokens_enc", "generation", "expires_at"):
        assert after[column] == before[column], column


def test_br_090_write_impossible_until_lease_end(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ready(gw)
    monkeypatch.setattr(lifecycle, "LEASE_S", 2.0)
    provider.script.append(Reply(normal=True, delay=0.6))  # rotates the refresh token
    before = gw.row(NAME)
    holder = _hold_lock(gw, after=0.3, duration=2.6)  # until after the end of the lease
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    holder.join()
    after = gw.row(NAME)
    _unchanged(before, after)
    assert after["last_outcome"] == "uncertain"
    assert [e["result"] for e in gw.audit() if e["action"] == "connection.renew"] == ["uncertain"]
    assert "last outcome uncertain" in gw.ok("connection", "status", NAME)
    late = list(provider.refresh_tokens)[-1]
    assert late in {t for _h, _c, t in provider.revoked}  # tokens never written: revoked
    # the next renewal presents the old token, already rotated: invalid_grant
    gw.clock.advance(int(after["not_before"]) - int(gw.clock()))
    with pytest.raises(connections.ReauthRequired):
        gw.access(NAME, ROLE)
    entry = [e for e in gw.audit() if e["action"] == "connection.renew"][-1]
    assert entry["reason"] == "invalid_grant after an uncertain outcome"


def test_br_090_response_cut_after_consumption(gw: Gateway) -> None:
    provider = _ready(gw)
    provider.script.append(Reply(normal=True, cut=True))
    before = gw.row(NAME)
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    after = gw.row(NAME)
    _unchanged(before, after)
    assert after["last_outcome"] == "uncertain"
    gw.clock.advance(int(after["not_before"]) - int(gw.clock()))
    with pytest.raises(connections.ReauthRequired):
        gw.access(NAME, ROLE)
    entry = [e for e in gw.audit() if e["action"] == "connection.renew"][-1]
    assert entry["reason"] == "invalid_grant after an uncertain outcome"


def test_br_090_blocked_then_released_before_lease_end(gw: Gateway) -> None:
    provider = _ready(gw)
    provider.script.append(Reply(normal=True, delay=0.6))
    holder = _hold_lock(gw, after=0.3, duration=1.5)
    access = gw.access(NAME, ROLE)
    holder.join()
    assert access.generation == 2
    tokens = gw.tokens(NAME)
    assert tokens is not None
    assert access.authorization == f"Bearer {tokens['access_token']}"
    conn = gw.connect()
    try:
        # tokens and audit entry in the same transaction: the entry exists, the outcome is `ok`
        entry = conn.execute(
            "SELECT result FROM audit WHERE action = 'connection.renew' ORDER BY seq DESC"
        ).fetchone()
    finally:
        conn.close()
    assert entry == ("ok",)
    assert len(provider.calls(fake_provider.TOKEN, "refresh_token")) == 1
