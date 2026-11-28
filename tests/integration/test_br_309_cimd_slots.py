"""BR-309 (AC-10) — a limit key holds at most two live CIMD fetches: a third gets `cimd_busy`,
audited, without taking one of the 16 slots, while another key's fetch proceeds; a document in
cache takes no slot; a worker outliving its deadline stays counted."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from hullgate import cimd
from hullgate.cimd import CimdError, Fetcher
from tests.support import Gateway, authorize_params, pkce, standard_tree, start_authorization

ROLE = "mpx/cairn/staging/dev"


@pytest.fixture
def stuck(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fetcher]:
    """A fetcher whose name resolution blocks: each fetch times out and its worker stays alive."""
    monkeypatch.setattr(cimd, "TIMEOUT_S", 0.2)
    release = threading.Event()

    def resolve(_host: str, _port: int) -> list[str]:
        release.wait(10)
        return ["127.0.0.1"]  # refused by the default filter: no connection is ever made

    fetcher = Fetcher(resolve=resolve)
    try:
        yield fetcher
    finally:
        release.set()


def _reason(fetcher: Fetcher, client_id: str, key: str) -> str:
    with pytest.raises(CimdError) as err:
        fetcher.document(client_id, key)
    return err.value.reason


def test_br_309_third_live_fetch_of_a_key_is_busy(stuck: Fetcher) -> None:
    assert _reason(stuck, "https://a.example.test/1.json", "192.0.2.1") == "cimd_timeout"
    assert _reason(stuck, "https://a.example.test/2.json", "192.0.2.1") == "cimd_timeout"
    start = time.monotonic()
    assert _reason(stuck, "https://a.example.test/3.json", "192.0.2.1") == "cimd_busy"
    assert time.monotonic() - start < 0.1  # refused at once, never a wait
    # the busy refusal took none of the 16 slots: two are held
    assert stuck._workers._value == cimd.MAX_WORKERS - 2
    # another key proceeds to its own fetch
    assert _reason(stuck, "https://b.example.test/1.json", "192.0.2.2") == "cimd_timeout"


def test_br_309_a_cached_document_takes_no_slot(stuck: Fetcher) -> None:
    cached = "https://c.example.test/doc.json"
    stuck._cache[cached] = (time.monotonic() + 600, {"client_id": cached, "redirect_uris": []})
    for i in range(2):
        _reason(stuck, f"https://a.example.test/{i}.json", "192.0.2.1")
    assert stuck.document(cached, "192.0.2.1")["client_id"] == cached


def test_br_309_busy_is_audited_at_authorize(gw: Gateway, stuck: Fetcher) -> None:
    standard_tree(gw)
    gw.fetcher = stuck
    with gw.client(peer="198.51.100.7") as client:
        for i in range(3):
            _, challenge = pkce()
            params = authorize_params(ROLE, challenge, f"https://a.example.test/{i}.json")
            assert start_authorization(client, ROLE, params).status_code == 400
    reasons = [(e["reason"], e["detail"]) for e in gw.audit() if e["action"] == "oauth.authorize"]
    assert [r for r, _ in reasons] == ["cimd_timeout", "cimd_timeout", "cimd_busy"]
    assert all('"address": "198.51.100.7"' in str(d) for _, d in reasons)  # BR-313
