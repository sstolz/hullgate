"""BR-220's closed population and explanatory refusals."""

import pytest

from hullgate.changes import UNDOABLE, eligibility


@pytest.mark.parametrize("action", sorted(UNDOABLE))
def test_br_220_closed_list(action: str) -> None:
    assert eligibility(
        {
            "action": action,
            "result": "ok",
            "before": {"value": 1},
            "after": {"value": 2},
            "detail": {},
        }
    )["undoable"]


@pytest.mark.parametrize(
    "action",
    [
        "tree.add",
        "tree.delete",
        "tree.rename",
        "tree.move",
        "token.issue",
        "token.revoke",
        "connection.authorize",
        "connection.revoke",
        "connection.remove",
        "admin.create",
        "admin.recover",
        "tool.classify",
        "tool.validate",
        "import.apply",
        "unknown",
    ],
)
def test_br_220_nonundoable_reason(action: str) -> None:
    result = eligibility({"action": action, "result": "ok", "detail": {}})
    assert not result["undoable"]
    assert result["undo_reason"]


@pytest.mark.parametrize("action", sorted(UNDOABLE))
def test_br_220_import_takes_precedence(action: str) -> None:
    result = eligibility({"action": action, "result": "ok", "detail": {"import_id": "batch"}})
    assert result == {"undoable": False, "undo_reason": "part_of_import"}
