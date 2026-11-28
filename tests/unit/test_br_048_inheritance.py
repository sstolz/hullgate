"""BR-048 — settings are inherited, with their origin."""

from __future__ import annotations

import json

import pytest

from tests.support import Gateway, standard_tree


def _get(gw: Gateway, path: str) -> dict[str, tuple[str, str]]:
    data = json.loads(gw.ok("settings", "get", path, "--json"))
    return {item["key"]: (item["value"], item["origin"]) for item in data}


def test_br_048_defaults_come_from_organisation(gw: Gateway) -> None:
    standard_tree(gw)
    assert _get(gw, "mpx/cairn/prod/dev") == {
        "token_duration": ("30d", "/"),
        "lock": ("false", "/"),
        "mode": ("write", "/"),  # F-015 BR-048 clarification
        "pinning": ("auto", "/"),
        "response_cap": ("1MiB", "/"),
        "token_rate_limit": ("120/min", "/"),  # F-020 BR-048 clarification
        "connection_rate_limit": ("300/min", "/"),
        "connection_expiry_days": ("7", "/"),
    }


def test_br_048_nearest_override_wins_with_origin(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("settings", "set", "mpx", "token_duration", "10d")
    gw.ok("settings", "set", "mpx/cairn/staging", "token_duration", "2d")
    assert _get(gw, "mpx/cairn/staging/dev")["token_duration"] == ("2d", "mpx/cairn/staging")
    assert _get(gw, "mpx/cairn/prod/dev")["token_duration"] == ("10d", "mpx")
    assert _get(gw, "mpx")["token_duration"] == ("10d", "mpx")
    gw.ok("settings", "set", "/", "token_duration", "60d")
    assert _get(gw, "mpx/cairn/prod/dev")["token_duration"] == ("10d", "mpx")


def test_br_048_unset_restores_inheritance(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("settings", "set", "mpx/cairn", "token_duration", "5d")
    out = gw.ok("settings", "unset", "mpx/cairn", "token_duration")
    assert "token_duration = 30d  (origin: /)" in out
    assert _get(gw, "mpx/cairn/staging/dev")["token_duration"] == ("30d", "/")


def test_br_048_unset_on_organisation_refused(gw: Gateway) -> None:
    result = gw.cli("settings", "unset", "/", "token_duration")
    assert result.code == 2
    assert "the organisation always carries a value" in result.err


def test_br_048_unset_without_override_refused(gw: Gateway) -> None:
    standard_tree(gw)
    result = gw.cli("settings", "unset", "mpx", "token_duration")
    assert result.code == 2
    assert "no override" in result.err


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("mode", "reading", "invalid value for mode"),
        ("bogus", "x", "unknown setting"),
        ("response_cap", "2MiB", "response_cap out of bounds"),
        ("pinning", "yes", "invalid value for pinning"),
        ("token_duration", "30", "invalid duration"),
        ("token_duration", "0h", "invalid duration"),
        ("token_duration", "366d", "duration out of bounds"),
        ("token_duration", "30m", "invalid duration"),
        ("lock", "yes", "invalid value for lock"),
        ("token_rate_limit", "0/min", "invalid rate"),  # F-020: no "off" value
        ("token_rate_limit", "100001/min", "invalid rate"),
        ("token_rate_limit", "9" * 4301 + "/min", "invalid rate"),  # review 585, N1
        ("connection_rate_limit", "300", "invalid rate"),
        ("connection_rate_limit", "5/s", "invalid rate"),
    ],
)
def test_br_048_undeclared_key_or_bad_value_refused(
    gw: Gateway, key: str, value: str, message: str
) -> None:
    standard_tree(gw)
    before = len(gw.audit())
    result = gw.cli("settings", "set", "mpx", key, value)
    assert result.code == 2
    assert message in result.err
    assert len(gw.audit()) == before


@pytest.mark.parametrize("value", ["1h", "365d", "8760h"])
def test_br_048_duration_bounds_accepted(gw: Gateway, value: str) -> None:
    standard_tree(gw)
    gw.ok("settings", "set", "mpx", "token_duration", value)


def test_br_048_change_is_audited_with_before_and_after(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("settings", "set", "mpx", "token_duration", "10d")
    gw.ok("settings", "set", "mpx", "token_duration", "12d")
    gw.ok("settings", "unset", "mpx", "token_duration")
    entries = [e for e in gw.audit() if e["action"].startswith("settings.")]
    assert [(e["action"], json.loads(e["before"]), json.loads(e["after"])) for e in entries] == [
        ("settings.set", {"token_duration": None}, {"token_duration": "10d"}),
        ("settings.set", {"token_duration": "10d"}, {"token_duration": "12d"}),
        (
            "settings.unset",
            {"token_duration": "12d"},
            {"token_duration": None, "effective": "30d", "origin": "/"},
        ),
    ]
    assert all(e["actor"] == "operator-cli" and e["path"] == "mpx" for e in entries)


@pytest.mark.parametrize("key", ["token_rate_limit", "connection_rate_limit"])
def test_br_048_rate_limits_are_ordinary_inherited_settings(gw: Gateway, key: str) -> None:
    """F-020 (AC-06): set, inherited, locked and exported like the other settings."""
    standard_tree(gw)
    gw.ok("settings", "set", "mpx/cairn", key, "100000/min")
    assert _get(gw, "mpx/cairn/staging/dev")[key] == ("100000/min", "mpx/cairn")
    gw.ok("settings", "set", "mpx/cairn/staging", key, "1/min")
    assert _get(gw, "mpx/cairn/staging/dev")[key] == ("1/min", "mpx/cairn/staging")
    assert key in gw.ok("export")
    gw.ok("tree", "lock", "mpx")
    locked = gw.cli("settings", "set", "mpx/cairn", key, "5/min")
    assert locked.code == 2
