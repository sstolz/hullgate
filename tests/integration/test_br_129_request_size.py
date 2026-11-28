"""BR-129 (F-008 lot B, AC-33): a `tools/call` whose arguments, in compact JSON, exceed 1 MiB is
refused `request_too_large` before any `get_access` and any exchange, audited; the exact bound
passes; 32 refusals in parallel take no slot of BR-108."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from tests.relay_bed import A, Bed, call, gateway_error, names
from tests.support import Gateway

MIB = 1024 * 1024
OVERHEAD = len('{"x":""}')


def _args(size: int) -> dict[str, str]:
    args = {"x": "a" * (size - OVERHEAD)}
    assert len(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode()) == size
    return args


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_129_exact_bound_is_relayed(bed: Bed) -> None:
    up = bed.connection("rq", A)
    up.results["read"] = {"content": [{"type": "text", "text": "received"}], "isError": False}
    with bed.serve() as client:
        names(client, A, bed.token(A))
        response = call(client, A, bed.token(A), "rq__read", _args(MIB))
    assert gateway_error(response) is None
    assert len(up.calls("tools/call")) == 1


def test_br_129_one_byte_over_is_refused_before_anything(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    up = bed.connection("rq", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        posts = len(up.calls())
        accesses = bed.spy_access(monkeypatch)
        response = call(client, A, bed.token(A), "rq__read", _args(MIB + 1))
    assert gateway_error(response) == "request_too_large"
    assert accesses == []
    assert len(up.calls()) == posts
    entry = [a for a in bed.gw.audit() if a["action"] == "relay.tool_call"][-1]
    assert json.loads(entry["detail"])["issue"] == "request_too_large"


def test_br_129_parallel_oversize_calls_take_no_slot(bed: Bed) -> None:
    up = bed.connection("rq", A)
    relay = bed.gw.relay()
    transport = relay.client.transport
    peak: list[int] = []
    original = transport._acquire

    def counting(connection_id: str) -> Any:
        peak.append(1)
        return original(connection_id)

    transport._acquire = counting  # type: ignore[method-assign]
    with bed.serve(relay) as client:
        names(client, A, bed.token(A))
        peak.clear()
        token = bed.token(A)
        out: list[str | None] = []
        args = _args(MIB + 1)
        threads = [
            threading.Thread(
                target=lambda: out.append(gateway_error(call(client, A, token, "rq__read", args)))
            )
            for _ in range(32)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    assert out == ["request_too_large"] * 32
    assert peak == []
    assert up.calls("tools/call") == []
