"""BR-174, BR-175 (F-015 AC-7, §3.2): exposure is decided in one order. The full table, class ×
mode × list membership × sensitivity × suspension, is checked against an oracle written from the
text of §3.2, independently of the evaluator; the first reason is the one the audit keeps and the
preview shows them all (BR-185, BR-183)."""

from __future__ import annotations

import itertools

import pytest

from hullgate import policy

ORDER = {"read": 0, "write": 1, "destructive": 2}


def oracle(
    cls: str,
    mode: str,
    sensitive: bool,
    opened: bool,
    denied: bool,
    listed: bool,
    others_listed: bool,
    suspended: bool,
) -> tuple[bool, list[str]]:
    """§3.2 read literally: (exposed, every applicable reason in table order)."""
    in_force = sensitive or listed or others_listed
    reasons = []
    if suspended:
        reasons.append("suspended")
    if denied:
        reasons.append("denied")
    if not opened:
        if in_force and not listed:
            reasons.append("not_allowlisted")
        if sensitive and cls != "read":
            reasons.append("write_closed")
        if not sensitive and ORDER[cls] > ORDER[mode]:
            reasons.append("above_mode")
    return not reasons, reasons


CASES = list(
    itertools.product(
        ("read", "write", "destructive"),
        ("read", "write", "destructive"),
        (False, True),  # sensitive
        (False, True),  # open
        (False, True),  # deny
        (False, True),  # allowlist (this tool)
        (False, True),  # allowlist (another tool only)
        (False, True),  # suspended
    )
)


@pytest.mark.parametrize("case", CASES, ids=lambda c: "-".join(str(int(x)) if isinstance(x, bool)
                                                              else x for x in c))  # fmt: skip
def test_br_174_table(case: tuple[str, str, bool, bool, bool, bool, bool, bool]) -> None:
    cls, mode, sensitive, opened, denied, listed, others, suspended = case
    facts = policy.Facts(
        cls=cls,
        mode=mode,
        sensitive=sensitive,
        opened=opened,
        denied=denied,
        allowlisted=listed,
        allowlist_in_force=sensitive or listed or others,
        suspended=suspended,
    )
    exposed, reasons = oracle(*case)
    verdict = policy.evaluate(facts)
    assert verdict.exposed is exposed
    assert list(verdict.reasons) == reasons
    # `opened`: exposed only thanks to `open`
    alone, _ = oracle(cls, mode, sensitive, False, denied, listed, others, suspended)
    assert verdict.opened is (exposed and opened and not alone)


def test_br_174_case_count() -> None:
    assert len(CASES) == 3 * 3 * 2**6


def test_br_174_deny_beats_open_and_suspension_beats_both() -> None:
    both = policy.Facts("read", "destructive", False, opened=True, denied=True)
    assert policy.evaluate(both).reasons == ("denied",)
    all_three = policy.Facts("read", "destructive", False, opened=True, denied=True,
                             suspended=True)  # fmt: skip
    assert policy.evaluate(all_three).reasons == ("suspended", "denied")


def test_br_175_sensitive_blank_placement_exposes_nothing() -> None:
    for cls in ("read", "write", "destructive"):
        v = policy.evaluate(policy.Facts(cls, "destructive", True, allowlist_in_force=True))
        assert not v.exposed
        assert v.reasons[0] == "not_allowlisted"


def test_br_175_listed_read_shows_listed_write_does_not() -> None:
    read = policy.Facts("read", "read", True, allowlisted=True, allowlist_in_force=True)
    write = policy.Facts("write", "destructive", True, allowlisted=True, allowlist_in_force=True)
    assert policy.evaluate(read).exposed
    assert policy.evaluate(write).reasons == ("write_closed",)
    opened = policy.Facts("destructive", "read", True, opened=True, allowlist_in_force=True)
    assert policy.evaluate(opened).exposed
    assert policy.evaluate(opened).opened


def test_br_174_listed_tool_stays_bounded_by_the_mode() -> None:
    """M9: on a non-sensitive placement an allowlist narrows; only `open` goes beyond the mode."""
    listed = policy.Facts("destructive", "write", False, allowlisted=True, allowlist_in_force=True)
    assert policy.evaluate(listed).reasons == ("above_mode",)
