"""Fixtures of `tests/browser`; the helpers live in `browser_support` and `console_support`.

The files share one gateway and one operator, so they run in a fixed order: test_br_202 first
(fresh instance, enrolment, signing key), the empty library before any seeding, the slice 2 acts,
the captures of AC-21, slice 3's policy screens (their own seed, after slice 2's states), and
test_br_212 last, so that its checks cover every page the run opened.
"""

from typing import Any

from browser_support import browser, operator, watches

__all__ = ["browser", "operator", "watches"]

ORDER = (
    "test_br_202_",
    "test_ac_21_empty",
    "test_br_229_links",
    "test_br_078_",
    "test_br_216_",
    "test_br_232_",
    "test_ac_21_captures",
    "test_ac_21_policy",
    "test_ac_21_organisation",
    "test_br_060_passkey",
    "test_ac_21_history_import",
    "test_br_212_",
)


def pytest_collection_modifyitems(items: list[Any]) -> None:
    def rank(item: Any) -> int:
        if item.name == "test_br_229_empty_links_and_direct_targets":
            return 1
        if item.name == "test_ac_21_setup_guide":
            return 2
        name = item.path.name
        return next((i * 3 for i, p in enumerate(ORDER) if name.startswith(p)), len(ORDER) - 1)

    items.sort(key=rank)  # stable: test order within a file
