"""HULL-85 whole-body deadline and refusal vocabulary."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from hullgate.body import BODY_READ_TIMEOUT, read_body
from hullgate.mcp_endpoint import A_BODY_REFUSED, REQUEST_TIMEOUT


def test_deadline_is_total_not_reset_by_each_chunk() -> None:
    received: list[int] = []

    async def receive() -> dict[str, Any]:
        await anyio.sleep(0.02)
        received.append(1)
        return {"type": "http.request", "body": b"x", "more_body": True}

    with pytest.raises(TimeoutError):
        anyio.run(read_body, {"headers": []}, receive, 1000, 0.09)
    assert 1 <= len(received) < 8


def test_queued_chunks_cannot_starve_deadline() -> None:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": True}

    with pytest.raises(TimeoutError):
        anyio.run(read_body, {"headers": []}, receive, 1000, 0.01)


def test_fixed_deadline_is_thirty_seconds() -> None:
    assert BODY_READ_TIMEOUT == 30.0


def test_br_131_body_timeout_vocabulary() -> None:
    # HULL-85's addition is also listed in docs/request-bodies.md's closed vocabulary table.
    assert A_BODY_REFUSED == "access.body_refused"
    assert REQUEST_TIMEOUT == "request_timeout"
