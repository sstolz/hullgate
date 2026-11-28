"""BR-093 — a transient failure changes neither the state nor the token (AC-22): each
transient, uncertain and anomaly line of the table of BR-092."""

from __future__ import annotations

import socket

import pytest

from hullgate import connections, outbound
from tests import fake_provider
from tests.fake_provider import Reply
from tests.support import Gateway, standard_tree

ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


CASES = {
    "connection_refused": (None, "transient"),
    "deadline": (Reply(normal=True, delay=2.0), "uncertain"),
    "503": (Reply(status=503), "transient"),
    "429": (Reply(status=429), "transient"),
    "400_unknown": (Reply(status=400, body={"error": "what"}), "anomaly"),
    "malformed_json": (
        Reply(raw=b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n{nope"),
        "anomaly",
    ),
    "too_large": (Reply(size=300 * 1024), "anomaly"),
    "redirection": (
        Reply(status=302, headers={"Location": "https://evil.example.test/token"}),
        "anomaly",
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_br_093_state_and_ciphertext_unchanged(
    gw: Gateway, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    gw.expire(NAME, 30)
    reply, kind = CASES[case]
    before = gw.row(NAME)
    if reply is None:
        port = _closed_port()
        monkeypatch.setattr(gw.net, "dial", lambda _a, _p: ("127.0.0.1", port))
    else:
        provider.script.append(reply)
    if case == "deadline":
        monkeypatch.setattr(outbound, "DEADLINE_S", 1.0)
    with pytest.raises(connections.TemporarilyUnavailable):
        gw.access(NAME, ROLE)
    after = gw.row(NAME)
    for column in ("state", "tokens_enc", "generation", "expires_at", "account"):
        assert after[column] == before[column], column
    assert after["last_outcome"] == kind
    entry = [e for e in gw.audit() if e["action"] == "connection.renew"][-1]
    assert entry["result"] == kind
    evil = gw.net.providers.get("evil.example.test")
    assert evil is None or evil.journal == []  # no redirect followed
    assert fake_provider.TOKEN  # (upstream journal read by the other tests)
