"""BR-115 (F-008 lot B, AC-18): the arguments reach the upstream of the connection named by the
public name intact, value for value; a `connection_id` of a connection of another role is an
ordinary value and opens no exchange with that connection."""

from __future__ import annotations

import pytest

from tests.relay_bed import A, B, Bed, call, echoed, names
from tests.support import Gateway


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_115_arguments_are_relayed_value_for_value(bed: Bed) -> None:
    ua = bed.connection("aa", A)
    ub = bed.connection("ab", B)
    foreign = bed.gw.connection_id("mpx/ab")
    args = {
        "connection_id": foreign,
        "nested": {"list": [1, 2.5, None, True, "é€😀"], "empty": {}},
        "n": -0.125,
        "null": None,
        "text": "line\nnext",
    }
    with bed.serve() as client:
        names(client, A, bed.token(A))
        response = call(client, A, bed.token(A), "aa__read", args)
    assert echoed(response) == args
    sent = ua.calls("tools/call")[0].body
    assert sent is not None
    assert sent["params"]["arguments"] == args
    assert ub.calls() == []  # the upstream of B receives nothing
