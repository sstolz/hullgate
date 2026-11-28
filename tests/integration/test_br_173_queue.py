"""BR-173 (F-015 AC-6): a tool seen for the first time for a service enters the catalogue and the
classification queue, leaves it by `tool classify` or `tool accept`, and the queue state plays no
part in the exposure decision; a tool no longer announced leaves the queue marked `removed`; the
name heuristic changes only the proposal."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fake_upstream import NONE, READ, tool
from tests.policy_support import audits, rows
from tests.relay_bed import A, Bed, S, names
from tests.support import Gateway


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def _queue(gw: Gateway) -> dict[str, dict[str, Any]]:
    return {q["name"]: q for q in json.loads(gw.ok("tool", "queue", "--json"))}


def _exposure(bed: Bed) -> tuple[list[str], list[str]]:
    with bed.serve() as client:
        return names(client, A, bed.token(A)), names(client, S, bed.token(S))


def test_br_173_new_tool_enters_queue_after_the_next_listing(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("qu", A, S, confirm=True)
    up.tools = [tool("look", annotations=READ)]
    _exposure(bed)
    assert set(_queue(gw)) == {"look"}
    up.tools.append(tool("delete_repo", annotations=NONE))
    up.tools.append(tool("comment"))
    gw.clock.advance(301)  # past the cache (BR-112)
    _exposure(bed)
    queued = _queue(gw)
    assert set(queued) == {"look", "delete_repo", "comment"}
    assert queued["delete_repo"]["proposal"] == "destructive"
    assert queued["delete_repo"]["class"] == "destructive"
    assert queued["delete_repo"]["source"] == "default"
    assert queued["comment"]["proposal"] is None
    assert queued["look"]["class"] == "read"
    discovered = audits(gw, "tool.discovered")
    assert sorted(a["detail"]["tool"] for a in discovered) == ["comment", "delete_repo", "look"]


def test_br_173_heuristic_proposes_never_imposes(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("qu", A)
    up.tools = [tool("delete_branch")]  # annotated `write`, name proposes `destructive`
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["qu__delete_branch"]
    [q] = _queue(gw).values()
    assert (q["class"], q["source"], q["proposal"]) == ("write", "annotations", "destructive")


@pytest.mark.parametrize("leave", ["classify", "accept"])
def test_br_173_queue_state_plays_no_part(bed: Bed, leave: str) -> None:
    """The exposure of a queued tool equals that of the same tool out of the queue, on a
    non-sensitive and a sensitive placement, under each list."""
    gw = bed.gw
    up = bed.connection("qu", A, S, confirm=True)
    up.tools = [tool("look", annotations=READ), tool("edit"), tool("raw", annotations=NONE)]
    _exposure(bed)
    states = []
    for setup in (
        (),
        (("allowlist", "look"),),
        (("open", "raw"),),
        (("deny", "edit"),),
    ):
        for which, name in setup:
            for role in (A, S):
                gw.ok("policy", "add", "mpx/qu", "--role", role, which, name)
        gw.clock.advance(1)
        queued = _exposure(bed)
        conn = gw.connect()
        conn.execute("UPDATE tool_catalog SET queued = 0")
        conn.commit()
        conn.close()
        out_of_queue = _exposure(bed)
        conn = gw.connect()
        conn.execute("UPDATE tool_catalog SET queued = 1")
        conn.commit()
        conn.close()
        assert queued == out_of_queue
        states.append(queued)
    assert len({json.dumps(s) for s in states}) > 1  # the setups did change the exposure
    if leave == "classify":
        gw.ok("tool", "classify", "squ", "look", "read")
    else:
        gw.ok("tool", "accept", "squ", "look")
        assert audits(gw, "tool.accept")[0]["after"] == {
            "queued": False, "class": "read", "source": "annotations",
        }  # fmt: skip
    assert "look" not in _queue(gw)
    assert gw.cli("tool", "accept", "squ", "look").code == 2  # not in the queue any more


def test_br_173_removed_tool_leaves_the_queue_marked(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("qu", A)
    up.tools = [tool("keep"), tool("gone")]
    gw.ok("connection", "tools", "mpx/qu", "--role", A)
    up.tools = [tool("keep")]
    gw.ok("connection", "tools", "mpx/qu", "--role", A)
    assert set(_queue(gw)) == {"keep"}
    assert rows(gw, "SELECT upstream_name, queued, removed FROM tool_catalog ORDER BY 1") == [
        ("gone", 0, 1),
        ("keep", 1, 0),
    ]


def test_br_173_classify_without_change_exits_the_queue_audited(bed: Bed) -> None:
    """Lot A review, N1: a `classify` that changes no manual class still takes the tool out of the
    queue, and that exit is audited like `tool accept`'s; on a tool out of the queue it is a no-op
    that writes nothing."""
    gw = bed.gw
    up = bed.connection("nq", A)
    up.tools = [tool("plain", annotations=READ)]
    gw.ok("connection", "tools", "mpx/nq", "--role", A)
    assert "plain" in _queue(gw)
    gw.ok("tool", "classify", "snq", "plain", "clear")  # no manual class to clear
    assert "plain" not in _queue(gw)
    [entry] = audits(gw, "tool.classify")
    assert entry["before"] == {"manual_class": None, "class": "read", "queued": True}
    assert entry["after"] == {"manual_class": None, "class": "read", "queued": False}
    before = rows(gw, "SELECT * FROM tool_catalog"), len(gw.audit())
    gw.ok("tool", "classify", "snq", "plain", "clear")
    assert (rows(gw, "SELECT * FROM tool_catalog"), len(gw.audit())) == before
