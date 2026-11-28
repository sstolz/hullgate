"""BR-171 (F-015 AC-4): the class of a tool is its manual classification when there is one,
otherwise what its MCP annotations declare, otherwise `destructive`. The annotation table of the
rule is written out row by row; manual classification takes priority over each row."""

from __future__ import annotations

from typing import Any

import pytest

from hullgate import policy

ABSENT = object()

# (readOnlyHint, destructiveHint) -> class from the annotations, None meaning source 3
TABLE: list[tuple[Any, Any, str | None]] = [
    (True, ABSENT, "read"),
    (True, False, "read"),
    (True, True, "destructive"),  # stricter than the MCP schema, deliberately
    (ABSENT, True, "destructive"),
    (False, True, "destructive"),
    (False, False, "write"),
    (ABSENT, False, "write"),
    (False, ABSENT, None),  # an absent destructiveHint is true in the schema: never `write`
    (ABSENT, ABSENT, None),
    ("yes", False, None),  # non-boolean values
    (True, "no", None),
    (1, ABSENT, None),
    (ABSENT, 0, None),
]


def _definition(read_only: Any, destructive: Any) -> dict[str, Any]:
    notes: dict[str, Any] = {}
    if read_only is not ABSENT:
        notes["readOnlyHint"] = read_only
    if destructive is not ABSENT:
        notes["destructiveHint"] = destructive
    return {"name": "t", "inputSchema": {"type": "object"}, "annotations": notes}


@pytest.mark.parametrize(("read_only", "destructive", "expected"), TABLE)
def test_br_171_annotation_row(read_only: Any, destructive: Any, expected: str | None) -> None:
    definition = _definition(read_only, destructive)
    assert policy.annotation_class(definition) == expected
    cls, source = policy.class_of(None, policy.annotation_class(definition))
    if expected is None:
        assert (cls, source) == ("destructive", "default")
    else:
        assert (cls, source) == (expected, "annotations")


@pytest.mark.parametrize(("read_only", "destructive", "expected"), TABLE)
@pytest.mark.parametrize("manual", ["read", "write", "destructive"])
def test_br_171_manual_wins_over_each_row(
    read_only: Any, destructive: Any, expected: str | None, manual: str
) -> None:
    annotated = policy.annotation_class(_definition(read_only, destructive))
    assert policy.class_of(manual, annotated) == (manual, "manual")


@pytest.mark.parametrize("annotations", [None, "x", [], 3])
def test_br_171_no_annotation_object_is_default(annotations: Any) -> None:
    definition: dict[str, Any] = {"name": "t"}
    if annotations is not None:
        definition["annotations"] = annotations
    assert policy.annotation_class(definition) is None
    assert policy.class_of(None, None) == ("destructive", "default")


def test_br_171_example_of_the_rule() -> None:
    """Given readOnlyHint and destructiveHint both true, without manual classification: the
    class is `destructive`, source `annotations`."""
    annotated = policy.annotation_class(_definition(True, True))
    assert policy.class_of(None, annotated) == ("destructive", "annotations")
