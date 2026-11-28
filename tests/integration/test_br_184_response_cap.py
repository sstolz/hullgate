"""BR-184, BR-126 (F-015 lot C, AC-18): a `tools/call` result above the role's effective
`response_cap` is replaced by `response_too_large`, never truncated, never repeated; at the cap it
is served. The size is the compact JSON of the upstream result, audited as `response_bytes`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hullgate.relay import WITHHELD
from tests.fake_upstream import Fault
from tests.relay_bed import A, B, Bed, call, gateway_error, names, tool_result, unknown
from tests.support import Gateway

KIB = 1024


def _compact(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _sized(size: int, is_error: bool) -> dict[str, Any]:
    """A tool result whose compact JSON is exactly `size` bytes."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": ""}], "isError": is_error}
    result["content"][0]["text"] = "a" * (size - _compact(result))
    assert _compact(result) == size
    return result


def _calls(bed: Bed, prefix: str) -> int:
    return len(bed.upstream(prefix).calls("tools/call"))


def _last_call(gw: Gateway) -> dict[str, Any]:
    entry = [a for a in gw.audit() if a["action"] == "relay.tool_call"][-1]
    detail: dict[str, Any] = json.loads(entry["detail"])
    return {"result": entry["result"], **detail}


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("is_error", [False, True], ids=["success", "is_error"])
def test_br_184_at_the_cap_served_one_byte_above_replaced(
    gw: Gateway, is_error: bool, sse: bool
) -> None:
    bed = Bed(gw)
    up = bed.connection("rc", A)
    gw.ok("settings", "set", A, "response_cap", "1KiB")
    with bed.serve() as client:
        names(client, A, bed.token(A))
        up.results["read"] = _sized(KIB, is_error)
        if sse:
            up.faults.append(Fault("sse", on="tools/call"))
        served = call(client, A, bed.token(A), "rc__read")
        assert tool_result(served)["content"] == _sized(KIB, is_error)["content"]
        assert _last_call(gw)["response_bytes"] == KIB

        up.results["read"] = _sized(KIB + 1, is_error)
        if sse:
            up.faults.append(Fault("sse", on="tools/call"))
        before = _calls(bed, "rc")
        replaced = call(client, A, bed.token(A), "rc__read")
    assert gateway_error(replaced) == "response_too_large"
    result = tool_result(replaced)
    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": WITHHELD}]
    assert "a" * 64 not in replaced.text  # not truncated: nothing of the result goes out
    assert _calls(bed, "rc") == before + 1  # the call is not repeated (BR-117)
    audited = _last_call(gw)
    assert audited["issue"] == "response_too_large"
    assert audited["response_bytes"] == KIB + 1  # the real size
    assert audited["response_cap"] == KIB


def test_br_184_withheld_text_says_the_tool_ran() -> None:
    assert WITHHELD.split(" ")[0] == "response_too_large"
    assert "the tool ran" in WITHHELD
    assert "withheld" in WITHHELD


def test_br_184_cap_is_the_roles_effective_setting(gw: Gateway) -> None:
    """Inherited from above, overridden per role, read at the next request (BR-048, BR-112)."""
    bed = Bed(gw)
    up = bed.connection("rc", A, B)
    up.results["read"] = _sized(3 * KIB, False)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        names(client, B, bed.token(B))
        assert gateway_error(call(client, A, bed.token(A), "rc__read")) is None  # 1MiB default
        gw.ok("settings", "set", "mpx/cairn/staging", "response_cap", "2KiB")  # warm cache
        assert gateway_error(call(client, A, bed.token(A), "rc__read")) == "response_too_large"
        assert gateway_error(call(client, B, bed.token(B), "rc__read")) == "response_too_large"
        gw.ok("settings", "set", B, "response_cap", "4KiB")
        assert gateway_error(call(client, B, bed.token(B), "rc__read")) is None
        assert gateway_error(call(client, A, bed.token(A), "rc__read")) == "response_too_large"
        gw.ok("settings", "unset", "mpx/cairn/staging", "response_cap")
        assert gateway_error(call(client, A, bed.token(A), "rc__read")) is None


@pytest.mark.parametrize("bad", ["1025KiB", "2MiB", "0KiB", "1023", "512B", "1.5MiB", "-1KiB"])
def test_br_184_setting_refuses_more_than_one_mib(gw: Gateway, bad: str) -> None:
    before = gw.ok("settings", "get", "/")
    assert gw.cli("settings", "set", "/", "response_cap", bad).code == 2
    assert gw.ok("settings", "get", "/") == before


@pytest.mark.parametrize("good", ["1KiB", "1024KiB", "1MiB", "64KiB"])
def test_br_184_setting_admits_one_kib_to_one_mib(gw: Gateway, good: str) -> None:
    gw.ok("settings", "set", "/", "response_cap", good)
    assert good in gw.ok("settings", "get", "/")


def test_br_184_cap_covers_tool_results_not_tools_list(gw: Gateway) -> None:
    bed = Bed(gw)
    up = bed.connection("rc", A)
    up.tools[0]["description"] = "d" * (2 * KIB)
    gw.ok("settings", "set", A, "response_cap", "1KiB")
    with bed.serve() as client:
        assert "rc__read" in names(client, A, bed.token(A))


def test_br_184_an_unreadable_cap_refuses_the_call(gw: Gateway) -> None:
    """A stored `response_cap` that cannot be parsed fails closed: `policy_error`, 0 call."""
    bed = Bed(gw)
    up = bed.connection("rc", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        conn = gw.connect()
        try:
            conn.execute("UPDATE setting SET value = 'lots' WHERE key = 'response_cap'")
            conn.commit()
        finally:
            conn.close()
        before = _calls(bed, "rc")
        response = call(client, A, bed.token(A), "rc__read")
    assert unknown(response)
    assert _calls(bed, "rc") == before
    entry = [a for a in gw.audit() if a["action"] == "relay.tool_call"][-1]
    assert entry["reason"] == "policy_error"
    assert up.calls("tools/call") == []
