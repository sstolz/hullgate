"""BR-170 (F-015 AC-4): a tool has one class among `read`, `write`, `destructive`, ordered in that
sequence; a mode admits its own class and the lower ones. The admission table over the 9 (class,
mode) pairs is written out, never derived from the code."""

from __future__ import annotations

import pytest

from hullgate import policy, tree

ADMISSION = {  # (class, mode) -> admitted
    ("read", "read"): True,
    ("read", "write"): True,
    ("read", "destructive"): True,
    ("write", "read"): False,
    ("write", "write"): True,
    ("write", "destructive"): True,
    ("destructive", "read"): False,
    ("destructive", "write"): False,
    ("destructive", "destructive"): True,
}


def test_br_170_three_classes_in_order() -> None:
    assert policy.CLASSES == ("read", "write", "destructive")
    assert tree.MODES == policy.CLASSES
    assert [policy.RANK[c] for c in ("read", "write", "destructive")] == [0, 1, 2]


@pytest.mark.parametrize(("cls", "mode"), sorted(ADMISSION))
def test_br_170_admission(cls: str, mode: str) -> None:
    assert policy.admits(mode, cls) is ADMISSION[(cls, mode)]


def test_br_170_table_is_the_nine_pairs() -> None:
    assert len(ADMISSION) == 9
    assert {c for c, _ in ADMISSION} == {m for _, m in ADMISSION} == set(policy.CLASSES)


@pytest.mark.parametrize(("cls", "mode"), sorted(ADMISSION))
def test_br_170_evaluator_bound_is_the_admission(cls: str, mode: str) -> None:
    """On a non-sensitive placement with no list, the evaluator exposes exactly what the mode
    admits (BR-174 step 5b reads BR-170)."""
    verdict = policy.evaluate(policy.Facts(cls=cls, mode=mode, sensitive=False))
    assert verdict.exposed is ADMISSION[(cls, mode)]
    assert verdict.reasons == (() if ADMISSION[(cls, mode)] else ("above_mode",))
