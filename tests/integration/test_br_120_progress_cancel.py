"""BR-120 (F-008 lot C, AC-25): progress and cancellation relayed.

The endpoint runs under uvicorn in a thread (a real TCP connection, which the client closes to
cancel), in front of the fake MCP upstream. A `tools/call` carrying a `progressToken`, from a
client that accepts `text/event-stream`, receives as a stream, in order and under its own token,
only the progress the upstream emits for that call; any other notification from the upstream is
discarded. When the client closes the connection of a call in progress, the upstream receives
`notifications/cancelled` for that request and sees its stream cut in under 1 s; the call is
audited `cancelled` and never replayed.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx2 as httpx
import pytest
import uvicorn

from hullgate.app import build_public_app
from hullgate.relay import Relay
from hullgate.upstream_mcp import CANCELLED, MODERN, PER_CONNECTION, TOTAL
from tests.fake_upstream import Fault
from tests.relay_bed import A, Bed, names
from tests.support import ACCEPT, PUBLIC_URL, Gateway, org

LEGACY = "2025-11-25"
RID = 7


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def served(bed: Bed, relay: Relay | None = None) -> Iterator[str]:
    """The gateway's public listener (relay included) under uvicorn, on 127.0.0.1."""
    gw = bed.gw
    app = build_public_app(gw.db, PUBLIC_URL, key=gw.key, totp=gw.totp, clock=gw.clock,
                           relay=relay or gw.relay())  # fmt: skip
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert srv.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        thread.join(10)


def _request(
    role: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
    version: str,
    progress: str | None,
    accept: str = ACCEPT,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    params: dict[str, Any] = {"name": name, "arguments": arguments}
    meta: dict[str, Any] = {}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "Content-Type": "application/json",
        "MCP-Protocol-Version": version,
    }
    if version == MODERN:
        meta.update({
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        })  # fmt: skip
        headers.update({"Mcp-Method": "tools/call", "Mcp-Name": name})
    if progress is not None:
        meta["progressToken"] = progress
    if meta:
        params["_meta"] = meta
    body = {"jsonrpc": "2.0", "id": RID, "method": "tools/call", "params": params}
    return f"/o/{org()}/p/{role}/mcp", headers, body


def _messages(response: httpx.Response) -> Iterator[dict[str, Any]]:
    """JSON-RPC messages of a streamed response, as they arrive."""
    for line in response.iter_lines():
        if line.startswith("data:") and line[5:].strip():
            yield json.loads(line[5:])


def _call(
    base: str,
    role: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
    version: str,
    progress: str | None,
    accept: str = ACCEPT,
) -> tuple[str, list[dict[str, Any]]]:
    """Full call: content type of the response and messages received, in order."""
    path, headers, body = _request(role, token, name, arguments, version, progress, accept)
    with (
        httpx.Client(base_url=base, timeout=30) as client,
        client.stream("POST", path, headers=headers, json=body) as response,
    ):
        assert response.status_code == 200, response.read()
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("text/event-stream"):
            return ctype, list(_messages(response))
        return ctype, [json.loads(response.read())]


def _announce(base: str, token: str) -> str:
    """`tools/list` of role A: the public names are born at announcement."""
    headers = {"Authorization": f"Bearer {token}", "Accept": ACCEPT,
               "MCP-Protocol-Version": "2025-06-18"}  # fmt: skip
    with httpx.Client(base_url=base) as c:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        return c.post(f"/o/{org()}/p/{A}/mcp", headers=headers, json=body).text


def _progress(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m["params"] for m in messages if m.get("method") == "notifications/progress"]


def _wait(predicate: Any, seconds: float = 5.0) -> bool:
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _issues(bed: Bed) -> list[str]:
    return [json.loads(a["detail"])["issue"] for a in bed.gw.audit()
            if a["action"] == "relay.tool_call"]  # fmt: skip


@pytest.mark.parametrize(
    ("generation", "version"),
    [("modern", LEGACY), ("modern", MODERN), ("session", LEGACY), ("session", MODERN)],
)
def test_br_120_progress_is_relayed_in_order_under_the_client_token(
    bed: Bed, generation: str, version: str
) -> None:
    up = bed.connection("pg", A, generation=generation)
    with served(bed) as base:
        token = bed.token(A)
        assert "pg__read" in _announce(base, token)
        up.faults.append(Fault("progress", on="tools/call"))
        ctype, messages = _call(base, A, token, "pg__read", {"q": "été"}, version, "client-token")
    assert ctype.startswith("text/event-stream")
    got = _progress(messages)
    marker = json.dumps({"q": "été"}, sort_keys=True)
    assert [(p["progressToken"], p["progress"], p["total"]) for p in got] == [
        ("client-token", 1, 3), ("client-token", 2, 3), ("client-token", 3, 3)
    ]  # fmt: skip
    assert [p["message"] for p in got] == [f"{marker} {i}" for i in (1, 2, 3)]
    # any other notification from the upstream is discarded: other token, `notifications/message`
    assert [m.get("method") for m in messages if "method" in m] == ["notifications/progress"] * 3
    raw = json.dumps(messages, ensure_ascii=False)
    assert "INTRUDER-PROGRESS" not in raw
    assert "INTRUDER-MESSAGE" not in raw
    final = messages[-1]
    assert final["id"] == RID
    assert final["result"]["isError"] is False
    # the upstream received the relay's token, unique, never the client's; arguments intact
    (sent,) = up.calls("tools/call")
    assert sent.body is not None
    params = sent.body["params"]
    assert params["arguments"] == {"q": "été"}
    assert str(params["_meta"]["progressToken"]).startswith("hullgate-")
    assert "client-token" not in json.dumps(sent.body)
    assert _issues(bed) == ["ok"]
    (entry,) = [a for a in bed.gw.audit() if a["action"] == "relay.tool_call"]
    assert json.loads(entry["detail"])["stream"] is True  # reading of the AC-OP of lot C


def test_br_120_two_concurrent_calls_never_mix_their_progress(bed: Bed) -> None:
    up = bed.connection("pc", A)
    with served(bed) as base:
        token = bed.token(A)
        _announce(base, token)
        up.faults += [Fault("progress", on="tools/call"), Fault("progress", on="tools/call")]
        out: dict[str, list[dict[str, Any]]] = {}
        barrier = threading.Barrier(2)

        def one(mark: str) -> None:
            barrier.wait()
            out[mark] = _call(base, A, token, "pc__read", {"m": mark}, LEGACY, f"t-{mark}")[1]

        threads = [threading.Thread(target=one, args=(m,)) for m in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    sent = [r.body["params"]["_meta"]["progressToken"] for r in up.calls("tools/call") if r.body]
    assert len(set(sent)) == 2  # one relay token per call
    for mark in ("A", "B"):
        got = _progress(out[mark])
        assert [p["progressToken"] for p in got] == [f"t-{mark}"] * 3
        assert all(p["message"].startswith(json.dumps({"m": mark})) for p in got)


def test_br_120_without_progress_token_nothing_is_streamed_nor_sent(bed: Bed) -> None:
    """A call with no `progressToken` (or from a client that does not accept SSE) stays JSON: no
    progress token is sent to the upstream, every notification is discarded."""
    up = bed.connection("pn", A)
    with served(bed) as base:
        token = bed.token(A)
        _announce(base, token)
        up.faults += [Fault("progress", on="tools/call"), Fault("progress", on="tools/call")]
        plain = _call(base, A, token, "pn__read", {}, LEGACY, None)
        json_only = _call(base, A, token, "pn__read", {}, LEGACY, "t", accept="application/json")
    for ctype, messages in (plain, json_only):
        assert ctype.startswith("application/json")
        assert messages[0]["result"]["isError"] is False
        assert "notifications/" not in json.dumps(messages)
    for r in up.calls("tools/call"):
        assert r.body is not None
        assert "progressToken" not in json.dumps(r.body)


def _open(base: str, name: str, token: str, version: str, progress: str | None) -> socket.socket:
    """`tools/call` on a raw TCP connection, which the test closes itself (the HTTP test client
    would wait for the headers, which a JSON response sends only at the end of the call)."""
    path, headers, body = _request(A, token, name, {}, version, progress)
    port = int(base.rsplit(":", 1)[1])
    data = json.dumps(body).encode()
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    raw = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{head}"
           f"Content-Length: {len(data)}\r\n\r\n").encode() + data  # fmt: skip
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(raw)
    return sock


def _places(relay: Relay) -> tuple[list[int], int]:
    """Free BR-108 slots: per connection, and in total."""
    t = relay.client.transport
    return [sem._value for sem in t._per.values()], t._total._value


def _all_free(relay: Relay) -> bool:
    per, total = _places(relay)
    return per == [PER_CONNECTION] * len(per) and total == TOTAL


@pytest.mark.parametrize("streamed", [True, False])
@pytest.mark.parametrize("version", [LEGACY, MODERN])
@pytest.mark.parametrize("generation", ["modern", "session"])
def test_br_120_closing_the_stream_cancels_the_upstream_call(
    bed: Bed, generation: str, version: str, streamed: bool
) -> None:
    """The client closes the connection of a call in progress (streamed with `progressToken`, or
    JSON without), upstream without or with a session × client 2025-11-25 or 2026-07-28 (review
    470, m1): `notifications/cancelled` for that request and upstream stream cut in under 1 s, one
    single `tools/call` request (never replayed), audit `cancelled`, BR-108 slots returned."""
    up = bed.connection("pk", A, generation=generation)
    relay = bed.gw.relay()
    with served(bed, relay) as base:
        token = bed.token(A)
        _announce(base, token)
        up.faults.append(Fault("progress_hang", on="tools/call", seconds=20.0))
        sock = _open(base, "pk__read", token, version, "cancelled-token" if streamed else None)
        try:
            if streamed:  # the response is streamed and the progress arrives
                seen = b""
                while b"notifications/progress" not in seen:
                    chunk = sock.recv(4096)
                    assert chunk, seen
                    seen += chunk
                assert b"text/event-stream" in seen
            else:  # JSON response: nothing arrives before the end of the call
                assert _wait(lambda: len(up.calls("tools/call")) == 1)
                time.sleep(0.2)  # the upstream holds its stream
        finally:
            closed = time.monotonic()
            sock.close()  # the client closes the connection
        assert _wait(lambda: bool(up.cut_at), 5.0)
        assert _wait(lambda: bool(up.calls("notifications/cancelled")), 5.0)
        assert _wait(lambda: CANCELLED in _issues(bed), 5.0)
        assert _wait(lambda: _all_free(relay), 5.0), _places(relay)
    # reading (pytest -s): cut delay seen from the upstream
    label = f"{generation}, {version}, stream={streamed}"
    print(f"BR-120 cancellation ({label}): cut {up.cut_at[0] - closed:.3f} s")
    assert up.cut_at[0] - closed < 1.0, up.cut_at[0] - closed
    (call,) = up.calls("tools/call")
    (note,) = up.calls("notifications/cancelled")
    assert call.body is not None
    assert note.body is not None
    assert note.body["params"]["requestId"] == call.body["id"]
    assert note.headers.get("mcp-session-id") == call.headers.get("mcp-session-id")
    assert note.at - closed < 1.0
    assert _issues(bed) == [CANCELLED]
    (entry,) = [a for a in bed.gw.audit() if a["action"] == "relay.tool_call"]
    detail = json.loads(entry["detail"])  # BR-122: fields of the cancelled call, with no argument
    assert (detail["connection"], detail["upstream_name"]) == ("mpx/pk", "read")
    assert detail["public_name"] == "pk__read"
    assert detail["version"] == (MODERN if generation == "modern" else "2025-11-25")
    assert entry["result"] == "error"
    assert up.executions == {"read": 1}  # never replayed


def test_br_120_cancel_notice_is_sent_when_the_connection_is_saturated(bed: Bed) -> None:
    """Review 470, B1: 4 calls held on the connection (BR-108 full), the client closes one: the
    upstream receives `notifications/cancelled` for that call, on the slot of the cancelled call;
    never more than 4 slots taken on the connection, all returned at the end."""
    up = bed.connection("ps", A, generation="session")
    relay = bed.gw.relay()
    with served(bed, relay) as base:
        token = bed.token(A)
        _announce(base, token)
        for _ in range(PER_CONNECTION):
            up.faults.append(Fault("progress_hang", on="tools/call", seconds=20.0))
        socks = [_open(base, "ps__read", token, LEGACY, None) for _ in range(PER_CONNECTION)]
        try:
            assert _wait(lambda: len(up.calls("tools/call")) == PER_CONNECTION)
            assert _places(relay)[0] == [0]  # connection full
            time.sleep(0.2)
            socks[0].close()
            assert _wait(lambda: bool(up.calls("notifications/cancelled")), 5.0)
            assert min(_places(relay)[0]) >= 0
            assert len(up.cut_at) == 1
            assert _wait(lambda: _places(relay)[0] == [1], 5.0), _places(relay)
        finally:
            for sock in socks[1:]:
                sock.close()
        assert _wait(lambda: len(up.calls("notifications/cancelled")) == PER_CONNECTION, 5.0)
        assert _wait(lambda: _all_free(relay), 5.0), _places(relay)
    calls = {r.body["id"] for r in up.calls("tools/call") if r.body}
    notes = {r.body["params"]["requestId"] for r in up.calls("notifications/cancelled") if r.body}
    assert notes == calls
    assert _issues(bed) == [CANCELLED] * PER_CONNECTION


def test_br_120_cancel_after_completion_is_harmless(bed: Bed) -> None:
    """Presence witness: a call carried to its end sends no cancellation to the upstream, even
    when the connection closes afterwards (the disconnect follows the response)."""
    up = bed.connection("pz", A)
    with bed.serve() as tc:
        names(tc, A, bed.token(A))
    with served(bed) as base:
        up.faults.append(Fault("progress", on="tools/call"))
        _call(base, A, bed.token(A), "pz__read", {}, LEGACY, "t")
        _call(base, A, bed.token(A), "pz__read", {}, LEGACY, None)
        time.sleep(0.3)
    assert up.calls("notifications/cancelled") == []
    assert up.cut_at == []
    assert _issues(bed) == ["ok", "ok"]
    streams = [json.loads(a["detail"]).get("stream") for a in bed.gw.audit()
               if a["action"] == "relay.tool_call"]  # fmt: skip
    assert streams == [True, None]
