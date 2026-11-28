"""BR-067 — `serverInfo.version` has a single source: `version` of pyproject.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

import hullgate
from tests.support import Gateway, mcp, standard_tree

ROOT = Path(__file__).resolve().parents[2]


def test_br_067_version_single_source(gw: Gateway) -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert hullgate.__version__ == declared
    assert '__version__ = "' not in (ROOT / "src/hullgate/__init__.py").read_text()
    standard_tree(gw)
    _, token = gw.issue("mpx/cairn/staging/dev")
    params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    }
    with gw.client() as client:
        init = mcp(client, "mpx/cairn/staging/dev", token, "initialize", params)
    assert init.json()["result"]["serverInfo"]["version"] == declared
