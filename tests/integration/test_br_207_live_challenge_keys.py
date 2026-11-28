"""BR-207 v2, BR-301 (AC-02) — live challenges share the client address's limit key."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hullgate.admin import auth
from hullgate.db import Database, transaction


@pytest.mark.parametrize(
    ("addresses", "sixth", "other"),
    [
        pytest.param(
            [f"2001:db8:7:{i}::1" for i in range(5)],
            "2001:db8:7:ffff::9",
            "2001:db8:8::1",
            id="ipv6-48",
        ),
    ]
    + [
        pytest.param(
            ["192.0.2.1", "::ffff:192.0.2.1", "192.0.2.1", "::ffff:192.0.2.1", "192.0.2.1"],
            sixth,
            "192.0.2.2",
            id=name,
        )
        for sixth, name in (
            ("192.0.2.1", "mapped-to-ipv4"),
            ("::ffff:192.0.2.1", "ipv4-to-mapped"),
        )
    ],
)
def test_br_207_live_challenges_count_by_limit_key(
    tmp_path: Path, addresses: list[str], sixth: str, other: str
) -> None:
    db = Database(str(tmp_path / "challenges.db"))
    assert db.open()
    try:

        def run(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                # Fixed time keeps all five challenges live; none is consumed by an assertion.
                challenges = [
                    auth.new_challenge(conn, "sign_in", address, 1000) for address in addresses
                ]
                assert len(set(challenges)) == 5
                with pytest.raises(auth.TooManyChallenges):
                    auth.new_challenge(conn, "sign_in", sixth, 1000)
                assert auth.new_challenge(conn, "sign_in", other, 1000)

        db.call(run)
    finally:
        db.close()
