"""BR-126 (F-008 lot B, AC-20): the result of a tool (success as well as `isError`) reaches the
client value for value; only the value of the connection's token is turned into `[masked]`; a
URL in the content stays."""

from __future__ import annotations

from typing import Any

import pytest

from tests.relay_bed import A, Bed, call, names, tool_result
from tests.support import Gateway

URL = "https://rr.mcp.test/repo/issues/42"


def _result(is_error: bool) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": f"ticket {URL}", "annotations": {"audience": ["user"]}},
            {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
            {"type": "resource_link", "uri": URL, "name": "ticket 42", "mimeType": "text/html"},
        ],
        "structuredContent": {"id": 42, "labels": ["a", "é"], "closed": None, "score": 0.5},
        "isError": is_error,
    }


@pytest.mark.parametrize("is_error", [False, True])
def test_br_126_result_is_relayed_value_for_value(gw: Gateway, is_error: bool) -> None:
    bed = Bed(gw)
    up = bed.connection("rr", A)
    up.results["read"] = _result(is_error)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        got = tool_result(call(client, A, bed.token(A), "rr__read"))
    expected = _result(is_error)
    for key in ("content", "structuredContent", "isError"):
        assert got[key] == expected[key], key


def test_br_126_only_the_connection_token_is_masked(gw: Gateway) -> None:
    bed = Bed(gw)
    up = bed.connection("rr", A)
    value = bed.values["rr"]
    up.results["read"] = {
        "content": [{"type": "text", "text": f"echo {value} and {URL}"}],
        "structuredContent": {"token": value, "other": "x" + value[1:]},
        "isError": False,
    }
    with bed.serve() as client:
        names(client, A, bed.token(A))
        response = call(client, A, bed.token(A), "rr__read")
    got = tool_result(response)
    assert got["content"][0]["text"] == f"echo [masked] and {URL}"
    assert got["structuredContent"] == {"token": "[masked]", "other": "x" + value[1:]}
    assert value not in response.text
