"""BR-301 — an address is limited by one key: IPv4 (mapped forms folded), IPv6 by /48."""

from __future__ import annotations

from hullgate.admin import accounts
from hullgate.db import Database
from hullgate.limits import limit_key


def test_br_301_two_addresses_of_one_48_share_a_key() -> None:
    assert limit_key("2001:db8:1:2::1") == limit_key("2001:db8:1:ffff::9") == "2001:db8:1::/48"


def test_br_301_two_48s_do_not_share_a_key() -> None:
    assert limit_key("2001:db8:1::1") != limit_key("2001:db8:2::1")


def test_br_301_mapped_ipv4_is_folded() -> None:
    assert limit_key("::ffff:192.0.2.1") == limit_key("192.0.2.1") == "192.0.2.1"


def test_br_301_ipv4_is_its_own_key() -> None:
    assert limit_key("192.0.2.1") != limit_key("192.0.2.2")


def test_br_301_a_non_address_is_its_own_key() -> None:
    assert limit_key("testclient") == "testclient"


def test_br_301_sign_in_window_counts_by_the_key(tmp_path: object) -> None:
    """BR-207 v2: F-016's sign-in window counts two addresses of one /48 together."""
    db = Database(f"{tmp_path}/k.db")
    assert db.open()
    try:

        def run(conn: object) -> bool:
            import sqlite3

            assert isinstance(conn, sqlite3.Connection)
            for i in range(accounts.ATTEMPT_LIMIT):
                accounts.record_failure(conn, f"2001:db8:7::{i + 1}", 1000)
            return accounts.limited(conn, "2001:db8:7:9::1", 1001)

        assert db.call(run) is True
        assert db.call(lambda c: accounts.limited(c, "2001:db8:8::1", 1001)) is False
    finally:
        db.close()
