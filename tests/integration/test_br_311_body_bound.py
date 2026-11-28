"""BR-311 (AC-13) — an access-point request body is bounded at 2 MiB: 2 MiB is served, one byte
more gets 413 `request_too_large`, by `Content-Length` and by stream; a refused `Content-Length`
reads no body; audited `access.body_refused` with the actor; an anonymous caller is refused before
any body is read."""

from __future__ import annotations

import json
import tracemalloc
from collections.abc import Iterator
from typing import Any

import anyio
import pytest

from hullgate import mcp_endpoint
from hullgate.mcp_endpoint import BODY_MAX, TOO_LARGE, _read_body
from tests.support import ACCEPT, Gateway, ap, standard_tree

ROLE = "mpx/cairn/staging/dev"
HEADERS = {
    "Accept": ACCEPT,
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


def _body(size: int) -> bytes:
    """A `tools/list` request padded with whitespace to exactly `size` bytes."""
    core = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    return core[:-1] + b" " * (size - len(core)) + b"}"


def _chunks(body: bytes, step: int = 256 * 1024) -> Iterator[bytes]:
    for i in range(0, len(body), step):
        yield body[i : i + step]


def test_br_311_two_mib_is_served(gw: Gateway) -> None:
    standard_tree(gw)
    _, token = gw.issue(ROLE)
    body = _body(BODY_MAX)
    assert len(body) == BODY_MAX
    with gw.client() as client:
        response = client.post(
            ap(ROLE), content=body, headers={**HEADERS, "Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200, response.text
    assert "result" in response.json()


@pytest.mark.parametrize("streamed", [False, True], ids=["content-length", "stream"])
def test_br_311_one_byte_more_gets_413(gw: Gateway, streamed: bool) -> None:
    standard_tree(gw)
    token_id, token = gw.issue(ROLE)
    body = _body(BODY_MAX + 1)
    content: Any = _chunks(body) if streamed else body
    with gw.client(peer="192.0.2.44") as client:
        response = client.post(
            ap(ROLE),
            content=content,
            headers={**HEADERS, "Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 413
    assert response.json() == {"error": "request_too_large"}
    entries = [e for e in gw.audit() if e["action"] == "access.body_refused"]
    assert len(entries) == 1
    assert entries[0]["actor"] == token_id
    assert entries[0]["reason"] == "request_too_large"
    assert json.loads(entries[0]["detail"])["address"] == "192.0.2.44"  # BR-313


def test_br_311_refused_content_length_reads_no_body() -> None:
    reads: list[object] = []

    async def receive() -> dict[str, Any]:
        reads.append(1)
        return {"type": "http.request", "body": b"x", "more_body": False}

    scope = {"headers": [(b"content-length", str(BODY_MAX + 1).encode())]}
    assert anyio.run(_read_body, scope, receive, BODY_MAX) is TOO_LARGE
    assert reads == []


def test_br_311_stream_stops_at_the_first_chunk_beyond() -> None:
    sent: list[int] = []
    chunk = b"a" * (512 * 1024)

    async def receive() -> dict[str, Any]:
        sent.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": True}  # never ends

    assert anyio.run(_read_body, {"headers": []}, receive, BODY_MAX) is TOO_LARGE
    assert sum(sent) == BODY_MAX + len(chunk)  # five chunks read, the fifth crosses the bound


def test_br_311_one_byte_events_hold_of_the_order_of_the_bound() -> None:
    """Review 585, B2: a body within the bound arriving in 1-byte events (a client trickling it
    through uvicorn) holds of the order of the bound, not one object per event."""
    sent = 0

    async def receive() -> dict[str, Any]:
        nonlocal sent
        sent += 1
        return {"type": "http.request", "body": b"a", "more_body": sent < BODY_MAX}

    tracemalloc.start()
    try:
        # Allocation tracing greatly slows millions of events; this test measures memory,
        # while HULL-85 tests the production deadline separately.
        body = anyio.run(_read_body, {"headers": []}, receive, BODY_MAX, 300.0)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert isinstance(body, bytes)
    assert len(body) == BODY_MAX
    assert peak < 3 * BODY_MAX  # the buffer and the returned copy, with growth slack


def test_br_311_anonymous_caller_is_refused_before_any_body(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_tree(gw)
    called: list[object] = []
    original = mcp_endpoint._read_body

    async def spy(*args: Any) -> Any:
        called.append(args)
        return await original(*args)

    monkeypatch.setattr(mcp_endpoint, "_read_body", spy)
    with gw.client() as client:
        response = client.post(ap(ROLE), content=_body(BODY_MAX + 1), headers=HEADERS)
    assert response.status_code == 401
    assert called == []
