"""BR-120 and the relay's thread ceiling (review 470, m2): when the SDK cancels the handling of a
`tools/call` (the client closed the stream), the cancellation is set for the relay, the wait
returns at once, but the call's thread keeps its slot among the `RELAY_THREADS` until it ends and
returns it itself."""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import anyio
import anyio.lowlevel
import pytest

from hullgate.mcp_endpoint import _run_relay
from hullgate.upstream_mcp import Cancel


def test_br_120_cancelled_call_keeps_its_thread_place_until_it_ends() -> None:
    release = threading.Event()
    started = threading.Event()
    seen: dict[str, Any] = {}

    def call() -> dict[str, Any]:
        started.set()
        release.wait(10)  # a thread that does not end at once (cold list, `get_access`)
        return {"content": []}

    async def main() -> None:
        limiter = anyio.CapacityLimiter(1)
        cancel = Cancel()
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as tg:

                async def run() -> None:
                    await _run_relay(call, limiter, anyio.CapacityLimiter(math.inf), cancel)

                tg.start_soon(run)
                while not started.is_set():
                    await anyio.sleep(0.01)
                scope.cancel()  # the SDK cancels the handling
        seen["cancel"] = cancel.is_set()
        seen["held_after_cancel"] = limiter.borrowed_tokens
        release.set()
        limit = time.monotonic() + 5
        while limiter.borrowed_tokens and time.monotonic() < limit:
            await anyio.sleep(0.01)
        seen["held_after_end"] = limiter.borrowed_tokens

    anyio.run(main)
    assert seen["cancel"] is True
    assert seen["held_after_cancel"] == 1  # the thread is still alive: its slot stays taken
    assert seen["held_after_end"] == 0  # returned by the thread when it ends


def test_br_120_completed_call_releases_its_place() -> None:
    async def main() -> tuple[dict[str, Any], int, bool]:
        limiter = anyio.CapacityLimiter(1)
        cancel = Cancel()
        got = await _run_relay(
            lambda: {"ok": True}, limiter, anyio.CapacityLimiter(math.inf), cancel
        )
        return got, limiter.borrowed_tokens, cancel.is_set()

    got, held, cancelled = anyio.run(main)
    assert got == {"ok": True}
    assert held == 0
    assert cancelled is False


@pytest.mark.parametrize("yields", [0, 1, 2, 3, 4])
def test_br_120_call_cancelled_before_its_thread_starts_gives_its_place_back(
    yields: int,
) -> None:
    """Review 473, B3: a cancellation that comes before the thread starts (anyio may never start
    it) returns the slot of the 96; the thread, if it starts anyway, does not call the relay."""
    ran = threading.Event()
    out: dict[str, Any] = {}

    def call() -> dict[str, Any]:
        ran.set()
        return {}

    async def main() -> None:
        limiter = anyio.CapacityLimiter(1)
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_run_relay, call, limiter, anyio.CapacityLimiter(math.inf), Cancel())
                for _ in range(yields):
                    await anyio.lowlevel.checkpoint()
                scope.cancel()
        await anyio.sleep(0.3)
        out["held"] = limiter.borrowed_tokens

    anyio.run(main)
    assert out["held"] == 0, f"slot of the 96 lost (thread started: {ran.is_set()})"
