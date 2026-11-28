"""Shared bounded body reader: one bytearray and one monotonic deadline for the whole read."""

from __future__ import annotations

import anyio
from starlette.types import Receive, Scope

BODY_READ_TIMEOUT = 30.0  # Fixed whole-body deadline (HULL-85, F-016 §3.15).


class TooLarge:
    """A body beyond the bound it was read under (BR-311)."""


TOO_LARGE = TooLarge()


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            text = value.decode("latin-1").strip()
            return int(text) if text.isdigit() else None
    return None


async def read_body(
    scope: Scope, receive: Receive, limit: int, timeout: float | None = None
) -> bytes | TooLarge | None:
    """A bounded read shared by both listeners (BR-311, HULL-85): the whole body when it holds
    in `limit` bytes; `TOO_LARGE` when its `Content-Length` announces more (nothing read), or at
    the first chunk that carries it past `limit` (nothing beyond that chunk is held); None if the
    client disconnects before the end. Raises TimeoutError at the total monotonic deadline."""
    declared = _declared_length(scope)
    if declared is not None and declared > limit:
        return TOO_LARGE
    # One growing buffer, never one object per event: what is held stays of the order of the
    # bound whatever the event size, a client trickling 1-byte events included (review 585, B2).
    buffer = bytearray()
    with anyio.fail_after(BODY_READ_TIMEOUT if timeout is None else timeout):
        events = 0
        while True:
            # A receive implementation may return queued chunks without yielding.
            if events % 1024 == 0:
                await anyio.lowlevel.checkpoint()
            events += 1
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            if isinstance(chunk, bytes) and chunk:
                if len(buffer) + len(chunk) > limit:
                    return TOO_LARGE
                buffer += chunk
            if not message.get("more_body", False):
                return bytes(buffer)
