"""AC-21 (§3.7) — SCR-07.empty, the Library before any service is declared: captured right after
test_br_202, before the slice 2 files seed the run."""

from __future__ import annotations

from browser_support import Watched
from console_support import both_themes, tree_page


def test_ac_21_empty_library(operator: Watched) -> None:
    tree_page(operator)
    operator.page.locator("[data-state='SCR-07.empty']").wait_for()
    both_themes(operator, "SCR-07.empty")
