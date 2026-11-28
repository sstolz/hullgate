"""BR-300 — one reader of the client address: a sweep of `src/hullgate` for the socket peer and
the forwarded header finds them in the address module alone; uvicorn's own handling is off on
both listeners."""

from __future__ import annotations

import re
from pathlib import Path

import hullgate
from hullgate import __main__ as entry
from hullgate import config

SRC = Path(hullgate.__file__).parent
ADDRESS_MODULE = SRC / "admin" / "client_address.py"
PATTERNS = (
    re.compile(r"""scope\s*(\[\s*["']client["']\s*\]|\.get\(\s*["']client["'])"""),
    re.compile(r"request\.client\b"),
    re.compile(r"x-forwarded", re.IGNORECASE),
)


def _readers() -> set[Path]:
    found: set[Path] = set()
    for path in SRC.rglob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            code = line.split("#", 1)[0]
            if any(p.search(code) for p in PATTERNS):
                found.add(path)
    return found


def test_br_300_sweep_finds_the_address_module_alone() -> None:
    readers = _readers()
    # docstrings of the module itself name the header; code elsewhere must not read it
    assert readers == {ADDRESS_MODULE}, sorted(str(p.relative_to(SRC)) for p in readers)


def test_br_300_sweep_would_catch_a_second_reader(tmp_path: Path) -> None:
    sample = 'peer = scope["client"][0]\nh = headers.get("X-Forwarded-For")\nrequest.client.host\n'
    hits = [p.pattern for p in PATTERNS if any(p.search(line) for line in sample.splitlines())]
    assert len(hits) == len(PATTERNS)


def test_br_300_uvicorn_proxy_headers_off_on_both_listeners(tmp_path: Path) -> None:
    env = {
        "PUBLIC_URL": "https://mcp.example.test",
        "ADMIN_URL": "http://localhost:14791",
        "DATA_DIR": str(tmp_path),
        "CONNECTIONS_KEY_FILE": str(tmp_path / "connections_key"),
        "TRUSTED_PROXIES": "172.30.0.2/32",
    }
    cfg = config.load(env)
    from hullgate.db import Database

    db = Database(str(tmp_path / "hullgate.db"))
    assert db.open()
    try:
        listeners = entry.configs(cfg, db)
    finally:
        db.close()
    assert len(listeners) == 2
    assert all(c.proxy_headers is False for c in listeners)
