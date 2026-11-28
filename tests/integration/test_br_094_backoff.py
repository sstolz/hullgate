"""BR-094 — `Retry-After` is honored, else a bounded exponential backoff (AC-23), injected clock."""

from __future__ import annotations

import asyncio
import email.utils

import pytest

from hullgate import connections
from hullgate.db import Database
from tests import fake_provider
from tests.fake_provider import Reply
from tests.support import Gateway, auth_url, standard_tree

ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"


def _ready(gw: Gateway) -> fake_provider.Provider:
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    gw.expire(NAME, 30)
    return provider


def _refreshes(provider: fake_provider.Provider) -> int:
    return len(provider.calls(fake_provider.TOKEN, "refresh_token"))


def _delay(gw: Gateway) -> int:
    return int(gw.row(NAME)["not_before"]) - int(gw.clock())


def test_br_094_retry_after_seconds_respected(gw: Gateway) -> None:
    provider = _ready(gw)
    provider.script.append(Reply(status=429, headers={"Retry-After": "30"}))
    with pytest.raises(connections.TemporarilyUnavailable) as err:
        gw.access(NAME, ROLE)
    assert err.value.retry_after == 30
    gw.clock.advance(29)
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    assert _refreshes(provider) == 1  # no call before 30 s
    gw.clock.advance(1)
    gw.expire(NAME, 30)
    gw.access(NAME, ROLE)
    assert _refreshes(provider) == 2


def test_br_094_retry_after_capped_at_15_min(gw: Gateway) -> None:
    provider = _ready(gw)
    provider.script.append(Reply(status=429, headers={"Retry-After": "86400"}))
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    assert _delay(gw) == 900


@pytest.mark.parametrize("value", ["demain", "past"])
def test_br_094_invalid_or_past_retry_after_falls_back(gw: Gateway, value: str) -> None:
    provider = _ready(gw)
    if value == "past":
        value = email.utils.formatdate(gw.clock() - 3600, usegmt=True)
    provider.script.append(Reply(status=503, headers={"Retry-After": value}))
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    assert _delay(gw) == 1


def test_br_094_exponential_backoff_capped_and_reset(gw: Gateway) -> None:
    provider = _ready(gw)
    delays = []
    for _ in range(4):
        provider.script.append(Reply(status=503))
        with pytest.raises(connections.TemporarilyUnavailable):
            gw.access(NAME, ROLE)
        delays.append(_delay(gw))
        gw.clock.advance(delays[-1])
        gw.expire(NAME, 30)
    assert delays == [1, 2, 4, 8]
    conn = gw.connect()
    conn.execute("UPDATE connection SET fail_count = 15")
    conn.commit()
    conn.close()
    provider.script.append(Reply(status=503))
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    assert _delay(gw) == 900
    # a second server instance sees the same "not before" date
    other = Database(str(gw.path))
    assert other.open()
    try:
        calls = _refreshes(provider)
        with pytest.raises(connections.TemporarilyUnavailable):
            asyncio.run(
                connections.get_access(
                    other,
                    gw.connections_key(),
                    gw.connection_id(NAME),
                    gw.node_id(ROLE),
                    clock=gw.clock,
                    out=gw.outbound,
                )
            )
        assert _refreshes(provider) == calls
    finally:
        other.close()
    # a success resets the backoff
    gw.clock.advance(900)
    gw.access(NAME, ROLE)
    assert gw.row(NAME)["fail_count"] == 0
    assert gw.row(NAME)["not_before"] is None
    gw.expire(NAME, 30)
    provider.script.append(Reply(status=503))
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    assert _delay(gw) == 1


def test_br_094_successful_authorize_resets_backoff(gw: Gateway) -> None:
    provider = _ready(gw)
    for _ in range(3):
        provider.script.append(Reply(status=503))
        with pytest.raises(connections.TemporarilyUnavailable):
            gw.access(NAME, ROLE)
        gw.clock.advance(_delay(gw))
        gw.expire(NAME, 30)
    assert gw.row(NAME)["fail_count"] == 3
    provider.script.append(Reply(status=503))
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    url = auth_url(gw.ok("connection", "reauthorize", NAME))
    gw.ok("connection", "authorize", NAME, "--callback", "-", stdin=provider.approve(url))
    row = gw.row(NAME)
    assert (row["fail_count"], row["not_before"]) == (0, None)
