"""BR-314 — the limiter's memory is bounded: 10 000 keys, least recently touched evicted, stale
keys dropped, nothing persisted."""

from __future__ import annotations

from hullgate.limits import MAX_KEYS, Coalescer, Limits, Window


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_br_314_10001_keys_leave_10000_the_oldest_touched_gone() -> None:
    clock = Clock()
    w = Window(60.0, clock)
    for i in range(MAX_KEYS):
        w.admit(f"k{i}", 5)
    w.admit("k0", 5)  # touched again: k1 becomes the oldest touched
    w.admit("new", 5)
    assert len(w) == MAX_KEYS
    assert w.count("k0") == 2
    assert w.count("k1") == 0  # evicted: fails open for it
    assert w.count("new") == 1


def test_br_314_a_stale_key_is_dropped() -> None:
    clock = Clock()
    w = Window(60.0, clock)
    w.admit("old", 5)
    clock.now += 61
    w.admit("fresh", 5)
    assert len(w) == 1
    assert w.count("old") == 0


def test_br_314_coalescer_is_bounded() -> None:
    clock = Clock()
    c = Coalescer(60.0, clock)
    for i in range(MAX_KEYS + 1):
        c.refused("oauth", f"k{i}")
    assert len(c) == MAX_KEYS


def test_br_314_every_store_of_the_listener_is_bounded() -> None:
    limits = Limits(clock=Clock(), max_keys=3)
    for store in (limits.tokens, limits.connections, limits.oauth, limits.register,
                  limits.credentials):  # fmt: skip
        for i in range(5):
            store.admit(f"k{i}", 10)
        assert len(store) == 3
    for i in range(5):
        limits.coalescer.refused("credential", f"k{i}")
    assert len(limits.coalescer) == 3


def test_br_314_one_keys_credential_log_is_bounded_by_its_budget() -> None:
    """Review 585, N2: a flood of unknown credentials from one key keeps the last 30 entries,
    not one per presentation; the decisions are those of the unbounded log."""
    clock = Clock()
    limits = Limits(clock=clock)
    decisions = []
    for _ in range(2000):
        clock.now += 0.01
        decisions.append(limits.unknown_credential("k"))
    assert limits.credentials.count("k") == 30
    assert decisions == [True] * 30 + [False] * (2000 - 30)
    clock.now += 59.0  # the flood's last 30 are still in the window: refused still
    assert limits.unknown_credential("k") is False
    clock.now += 61.0
    assert limits.unknown_credential("k") is True
