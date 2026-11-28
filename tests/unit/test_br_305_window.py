"""BR-305 — a limit counts admitted requests in a sliding window (injected clock)."""

from __future__ import annotations

from hullgate.limits import Window, whole_seconds


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_br_305_budget_is_exact_over_any_window() -> None:
    clock = Clock()
    w = Window(60.0, clock)
    for _ in range(5):
        assert w.admit("k", 5) is None
        clock.now += 10  # t = 1000, 1010, …, 1040
    assert w.admit("k", 5) is not None  # five in the last 60 s
    clock.now = 1060.0  # the first (1000) leaves at exactly 1060
    assert w.admit("k", 5) is None
    assert w.admit("k", 5) is not None


def test_br_305_refused_request_does_not_extend_the_wait() -> None:
    clock = Clock()
    w = Window(60.0, clock)
    for _ in range(3):
        assert w.admit("k", 3) is None
    first = w.admit("k", 3)
    assert first == 60.0
    for _ in range(50):
        clock.now += 1
        assert w.admit("k", 3) is not None  # refused, not counted
    clock.now = 1060.0
    assert w.admit("k", 3) is None


def test_br_305_delay_is_exact_and_rounded_up_to_the_second() -> None:
    clock = Clock()
    w = Window(60.0, clock)
    assert w.admit("k", 1) is None
    clock.now += 12.3
    delay = w.admit("k", 1)
    assert delay is not None
    assert abs(delay - 47.7) < 1e-6
    assert whole_seconds(delay) == 48


def test_br_305_delay_with_a_count_above_a_lowered_value() -> None:
    """Count 6, value lowered to 3: wait until the (6 − 3 + 1) = 4th oldest leaves."""
    clock = Clock()
    w = Window(60.0, clock)
    for _ in range(6):
        assert w.admit("k", 10) is None
        clock.now += 5  # entries at 1000, 1005, …, 1025
    delay = w.admit("k", 3)
    assert delay is not None
    assert abs(delay - (1015 + 60 - clock.now)) < 1e-6  # 45 s, not the oldest's 30 s
    clock.now = 1074.9  # 1015 still counted: three in the window, not below the value
    assert w.admit("k", 3) is not None
    clock.now = 1075.0  # 1015 leaves: two left (1020, 1025), below the value
    assert w.admit("k", 3) is None


def test_br_305_mixed_values_share_one_count() -> None:
    """One count read against two roles' values (BR-303, BR-304)."""
    clock = Clock()
    w = Window(60.0, clock)
    for _ in range(4):
        assert w.admit("token", 10) is None
    assert w.admit("token", 4) is not None  # the role at 4/min is refused
    assert w.admit("token", 10) is None  # the role at 10/min still passes


def test_br_305_counting_refused_requests_bars_recovery() -> None:
    """BR-308's exception: every presented unknown credential counts."""
    clock = Clock()
    w = Window(60.0, clock)
    for _ in range(3):
        assert w.admit("k", 3, count_refused=True) is None
    for _ in range(100):
        clock.now += 1
        assert w.admit("k", 3, count_refused=True) is not None
