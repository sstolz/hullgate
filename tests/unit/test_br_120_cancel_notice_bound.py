"""BR-120 and BR-107 (review 473, B2): the `notifications/cancelled` notice of a cancelled call
has a hard deadline of 2 s (`CLOSE_S`); an upstream that answers the notice with a slow stream
does not hold the connection's BR-108 slots beyond it, and they are returned exactly once."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from hullgate.upstream_mcp import (
    CLOSE_S,
    PER_CONNECTION,
    TOTAL,
    Cancel,
    Target,
    Transport,
    UpstreamError,
)


class _SlowUpstream:
    """Raw HTTP upstream: the 1st POST (the call) holds an SSE stream; the 2nd (the notice)
    answers 200 as a stream and drips a comment every 0.5 s for `trickle` seconds."""

    def __init__(self, trickle: float) -> None:
        self.trickle = trickle
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = int(self.srv.getsockname()[1])
        self.n = 0
        self.notice_at: float | None = None
        self.stop = threading.Event()
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self) -> None:
        while not self.stop.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.n += 1
            threading.Thread(target=self.serve, args=(c, self.n), daemon=True).start()

    def serve(self, c: socket.socket, n: int) -> None:
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    return
                data += chunk
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            if n == 1:
                self.stop.wait(30)
            else:
                self.notice_at = time.monotonic()
                end = time.monotonic() + self.trickle
                while time.monotonic() < end and not self.stop.is_set():
                    c.sendall(b": k\n\n")
                    time.sleep(0.5)
        except OSError:
            pass
        finally:
            c.close()

    def close(self) -> None:
        self.stop.set()
        self.srv.close()


def test_br_120_cancel_notice_has_a_hard_deadline() -> None:
    up = _SlowUpstream(trickle=8.0)
    try:
        t = Transport(
            resolve=lambda _h, _p: ["10.0.0.9"], dial=lambda _a, _p: ("127.0.0.1", up.port)
        )
        target = Target(f"http://up.example.test:{up.port}/mcp", internal=True)
        cancel = Cancel()
        notice = ({"Content-Type": "application/json"}, b'{"jsonrpc":"2.0","method":"x"}')
        errors: list[Any] = []

        def call() -> None:
            try:
                t.exchange("c1", target, "POST", {"Content-Type": "application/json"}, b"{}",
                           60.0, 1, None, None, cancel, notice)  # fmt: skip
            except UpstreamError as exc:
                errors.append(exc)

        caller = threading.Thread(target=call)
        caller.start()
        time.sleep(0.5)
        cancel.set()
        caller.join(5)
        limit = time.monotonic() + CLOSE_S + 1.0  # deadline of the notice + margin
        while t._total._value != TOTAL and time.monotonic() < limit:
            time.sleep(0.05)
        assert up.notice_at is not None  # the notice was sent
        assert [e.code for e in errors] == ["cancelled"]
        assert t._total._value == TOTAL, "slots held beyond the deadline of the notice"
        assert [s._value for s in t._per.values()] == [PER_CONNECTION]
        time.sleep(0.3)  # returned only once: a second return would raise (BoundedSemaphore)
        assert t._total._value == TOTAL
    finally:
        up.close()
