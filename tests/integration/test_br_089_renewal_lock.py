"""BR-089 — the renewal of a connection is serialized, across all processes (AC-18).

A lease per connection in short transactions: the call to the provider is made outside any
transaction, so `/readyz` and inbound OAuth answer during a held renewal. The proof cites the fake
provider's upstream journal (`provider.calls`).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hullgate import connections, lifecycle
from tests import fake_provider
from tests.fake_provider import Reply
from tests.support import Gateway, grant, standard_tree

ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"

CHILD = """
import json, ssl, sys, time
from hullgate import connections
from hullgate.cipher import ConnectionsKey
from hullgate.db import connect
from hullgate.outbound import Outbound
db, key, cid, rid, cafile, port, start = sys.argv[1:]
out = Outbound(
    context=ssl.create_default_context(cafile=cafile),
    # Mocked routable address; no external connection is made.
    resolve=lambda host, p: ["11.1.1.1"],
    dial=lambda address, p: ("127.0.0.1", int(port)),
)
conn = connect(db)
while time.time() < float(start):
    time.sleep(0.005)
try:
    got = connections.get_access_sync(conn, ConnectionsKey(key), cid, rid, out=out)
    print(json.dumps({"ok": True, "generation": got.generation}))
except connections.AccessError as exc:
    print(json.dumps({"ok": False, "code": exc.code}))
"""


def _ready(gw: Gateway) -> fake_provider.Provider:
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    return provider


def test_br_089_two_processes_one_renewal(gw: Gateway, tmp_path: Path) -> None:
    provider = _ready(gw)
    conn = gw.connect()
    conn.execute("UPDATE connection SET expires_at = ?", (int(time.time()) + 30,))
    conn.commit()
    conn.close()
    provider.script.append(Reply(normal=True, delay=1.0))  # single-use refresh token
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    start = str(time.time() + 1.0)
    args = [
        str(gw.path),
        str(gw.connections_key_path),
        gw.connection_id(NAME),
        gw.node_id(ROLE),
        str(gw.net.cert_path),
        str(gw.net.port),
        start,
    ]
    procs = [
        subprocess.Popen([sys.executable, str(script), *args], stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    results = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    assert results == [{"ok": True, "generation": 2}] * 2, results
    refreshes = provider.calls(fake_provider.TOKEN, "refresh_token")
    assert len(refreshes) == 1  # a single renewal reached the provider
    assert not [e for e in gw.audit() if e["action"] == "connection.renew" and e["result"] != "ok"]


def test_br_089_readyz_and_inbound_oauth_during_held_renewal(gw: Gateway) -> None:
    provider = _ready(gw)
    gw.oauth("mpx/gitea-rev", "gr", "gitea-rev", "app-rev")
    gw.ok("connection", "place", "mpx/gitea-rev", ROLE)
    gw.preregister()
    gw.expire(NAME, 30)
    gw.expire("mpx/gitea-rev", 30)
    provider.script.append(Reply(normal=True, delay=8.0))
    got: list[object] = []
    held = threading.Thread(target=lambda: got.append(gw.access(NAME, ROLE)))
    held.start()
    time.sleep(0.5)
    with gw.client() as client:
        started = time.monotonic()
        assert client.get("/readyz").status_code == 200
        body = grant(gw, client, ROLE)  # complete inbound OAuth issuance
        assert body["access_token"]
        other = gw.access("mpx/gitea-rev", ROLE)  # another connection: without waiting
        elapsed = time.monotonic() - started
    assert other.generation == 2
    assert elapsed < 5.0, elapsed
    assert held.is_alive()  # the first renewal is still held
    held.join()
    assert isinstance(got[0], connections.Access)
    assert got[0].generation == 2


def test_br_089_waiter_gives_up_after_lease_without_second_call(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ready(gw)
    monkeypatch.setattr(lifecycle, "LEASE_S", 2.0)
    gw.expire(NAME, 30)
    provider.script.append(Reply(normal=True, delay=4.0))
    first: list[BaseException] = []

    def hold() -> None:
        try:
            gw.access(NAME, ROLE)
        except connections.AccessError as exc:
            first.append(exc)

    holder = threading.Thread(target=hold)
    holder.start()
    time.sleep(0.3)
    started = time.monotonic()
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    assert 2.0 <= time.monotonic() - started < 4.0
    assert len(provider.calls(fake_provider.TOKEN, "refresh_token")) == 1
    holder.join()
    assert isinstance(
        first[0], connections.TemporarilyUnavailable
    )  # result outside the lease discarded
    late = list(provider.refresh_tokens)[-1]
    assert late in {t for _h, _c, t in provider.revoked}


def test_br_089_stale_lease_of_killed_process(gw: Gateway) -> None:
    provider = _ready(gw)
    gw.expire(NAME, 30)
    conn = gw.connect()
    conn.execute(
        "UPDATE connection SET renewing_until = ?, renewal_owner = 'mort'", (int(gw.clock()) + 1,)
    )
    conn.commit()
    conn.close()
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)  # waits for the end of the lease + 1 s, no call
    assert provider.calls(fake_provider.TOKEN, "refresh_token") == []
    gw.clock.advance(
        2
    )  # lease expired without a result (`lease_expired`): `uncertain` audited, then renewal
    assert gw.access(NAME, ROLE).generation == 2
    results = [e["result"] for e in gw.audit() if e["action"] == "connection.renew"]
    assert results == ["uncertain", "ok"]


def test_br_089_renouveler_command(gw: Gateway) -> None:
    provider = _ready(gw)
    out = gw.ok("connection", "renew", NAME)
    assert "renewed: mpx/gitea-dev, generation 2" in out
    assert len(provider.calls(fake_provider.TOKEN, "refresh_token")) == 1
    entry = [e for e in gw.audit() if e["action"] == "connection.renew"][-1]
    assert entry["result"] == "ok"
    provider.script.append(Reply(status=429, headers={"Retry-After": "60"}))
    assert gw.cli("connection", "renew", NAME).code == 1
    during = gw.cli("connection", "renew", NAME)
    assert during.code == 2
    assert "not before" in during.err
    assert len(provider.calls(fake_provider.TOKEN, "refresh_token")) == 2
    gw.static("mpx/provided", "gf")
    static = gw.cli("connection", "renew", "mpx/provided")
    assert static.code == 2
    assert "provided token" in static.err
    gw.connections_key_path.chmod(0o600)
    gw.connections_key_path.unlink()
    assert gw.cli("connection", "renew", NAME).code == 1


def test_br_089_async_contract_does_not_hold_process_lock(gw: Gateway) -> None:
    """The async contract (#9) holds the `Database` lock only per short transaction."""
    provider = _ready(gw)
    gw.expire(NAME, 30)
    provider.script.append(Reply(normal=True, delay=2.0))

    async def both() -> tuple[connections.Access, float]:
        task = asyncio.create_task(
            connections.get_access(
                gw.db,
                gw.connections_key(),
                gw.connection_id(NAME),
                gw.node_id(ROLE),
                clock=gw.clock,
                out=gw.outbound,
            )
        )
        await asyncio.sleep(0.5)
        started = time.monotonic()
        ready = await asyncio.to_thread(gw.db.readiness)
        waited = time.monotonic() - started
        assert ready.ok
        assert not task.done()
        return await task, waited

    access, readiness_wait = asyncio.run(both())
    assert access.generation == 2
    assert readiness_wait < 0.5
