"""Shared fixtures; the audit sweep (marker `audit_sweep`) runs last."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support import Gateway

# F-016: `tests/browser` runs in its own image (`make test-browser`), never in the gate.
collect_ignore = ["browser"]


@pytest.fixture
def gw(tmp_path: Path) -> Iterator[Gateway]:
    gateway = Gateway(tmp_path)
    yield gateway
    gateway.close()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """BR-066 (AC-06): the sweep rereads the databases of the whole suite, so it runs at the end."""
    items.sort(key=lambda item: item.get_closest_marker("audit_sweep") is not None)
