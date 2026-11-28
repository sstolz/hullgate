"""BR-172, BR-049 (F-015 AC-5, AC-10, D10): `tool classify` records one class for a (service,
upstream name), which applies to every connection of that service at the next request; `clear`
restores the source below; another service is untouched; an unknown tool is refused; a lowering
under a lock is refused with nothing written, a raising passes."""

from __future__ import annotations

import pytest

from tests.fake_upstream import NONE, READ, tool
from tests.policy_support import audits, catalogue, policy_rows
from tests.relay_bed import A, B, Bed, names
from tests.support import Gateway


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def _two_connections_one_service(bed: Bed) -> None:
    """`mpx/k1` and `mpx/k2` on the same service `sk1`, placed on A and B; `mpx/o` elsewhere."""
    gw = bed.gw
    up = bed.connection("k1", A)
    up.tools = [tool("plain", annotations=NONE), tool("look", annotations=READ)]
    gw.static("mpx/k2", "k2", "sk1")
    gw.ok("connection", "place", "mpx/k2", B)
    other = bed.connection("ot", A)
    other.tools = [tool("plain", annotations=NONE)]
    catalogue(gw, "mpx/k1", A)
    catalogue(gw, "mpx/ot", A)


def test_br_172_manual_class_binds_the_service_and_clear_restores(bed: Bed) -> None:
    gw = bed.gw
    _two_connections_one_service(bed)
    with bed.serve() as client:  # warm caches, then the class changes
        assert names(client, A, bed.token(A)) == ["k1__look"]
        assert names(client, B, bed.token(B)) == ["k2__look"]
        gw.ok("tool", "classify", "sk1", "plain", "read")
        assert names(client, A, bed.token(A)) == ["k1__plain", "k1__look"]
        assert names(client, B, bed.token(B)) == ["k2__plain", "k2__look"]
        gw.ok("tool", "classify", "sk1", "look", "destructive")
        assert names(client, A, bed.token(A)) == ["k1__plain"]
        gw.ok("tool", "classify", "sk1", "look", "clear")  # back to its annotations
        gw.ok("tool", "classify", "sk1", "plain", "clear")  # back to the default
        assert names(client, A, bed.token(A)) == ["k1__look"]
    out = gw.ok("tool", "queue", "--service", "sot", "--json")
    assert '"name": "plain"' in out  # other service untouched
    assert '"source": "default"' in out
    [first, *_] = audits(gw, "tool.classify")
    assert first["before"] == {"manual_class": None, "class": "destructive"}
    assert first["after"] == {"manual_class": "read", "class": "read"}


def test_br_172_unknown_tool_and_unknown_service_refused(bed: Bed) -> None:
    gw = bed.gw
    _two_connections_one_service(bed)
    before = policy_rows(gw)
    assert gw.cli("tool", "classify", "sk1", "nothing", "read").code == 2
    assert gw.cli("tool", "classify", "nope", "plain", "read").code == 2
    assert gw.cli("tool", "accept", "sk1", "nothing").code == 2
    assert policy_rows(gw) == before


def test_br_172_lowering_under_a_lock_refused_raising_passes(bed: Bed) -> None:
    gw = bed.gw
    _two_connections_one_service(bed)
    gw.ok("tree", "lock", "mpx/cairn/staging")
    before = policy_rows(gw)
    refused = gw.cli("tool", "classify", "sk1", "plain", "read")  # destructive → read: lowers
    assert refused.code == 2
    assert "locked by mpx/cairn/staging" in refused.err
    assert policy_rows(gw) == before
    gw.ok("tool", "classify", "sk1", "look", "write")  # read → write: raises, passes
    cleared = gw.cli("tool", "classify", "sk1", "look", "clear")  # write → read: lowers
    assert cleared.code == 2
    gw.ok("tool", "classify", "sk1", "look", "destructive")
    gw.ok("tree", "unlock", "mpx/cairn/staging")
    gw.ok("tool", "classify", "sk1", "plain", "read")
