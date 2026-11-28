"""BR-032 — configuration by environment variables, refusal at startup."""

from __future__ import annotations

from pathlib import Path

import pytest

from hullgate import cipher, config
from hullgate.db import Database

BASE = {"PUBLIC_URL": "https://mcp.example.test", "ADMIN_URL": "http://localhost:14791"}


def test_br_032_defaults_apply() -> None:
    cfg = config.load(BASE)
    assert (cfg.public_port, cfg.admin_port, cfg.data_dir) == (4790, 4791, "/data")
    assert cfg.db_path == "/data/hullgate.db"


def test_br_032_explicit_values_are_read() -> None:
    cfg = config.load({**BASE, "PUBLIC_PORT": "8000", "ADMIN_PORT": "8001", "DATA_DIR": "/srv/x"})
    assert (cfg.public_port, cfg.admin_port, cfg.db_path) == (8000, 8001, "/srv/x/hullgate.db")


@pytest.mark.parametrize("variable", ["PUBLIC_URL", "ADMIN_URL"])
def test_br_032_required_url_missing_names_variable(variable: str) -> None:
    env = {k: v for k, v in BASE.items() if k != variable}
    with pytest.raises(config.ConfigError) as err:
        config.load(env)
    assert err.value.variable == variable
    assert variable in str(err.value)


@pytest.mark.parametrize("value", ["mcp.example.test", "/relative", "ftp://x.test", "https://"])
def test_br_032_url_must_be_absolute_http(value: str) -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, "PUBLIC_URL": value})
    assert err.value.variable == "PUBLIC_URL"


@pytest.mark.parametrize("value", ["0", "65536", "-1", "http", "80.5"])
@pytest.mark.parametrize("variable", ["PUBLIC_PORT", "ADMIN_PORT"])
def test_br_032_port_out_of_range_names_variable(variable: str, value: str) -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, variable: value})
    assert err.value.variable == variable


def test_br_032_equal_ports_refused() -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, "PUBLIC_PORT": "5000", "ADMIN_PORT": "5000"})
    assert err.value.variable == "ADMIN_PORT"


def test_br_032_data_dir_must_be_absolute() -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, "DATA_DIR": "data"})
    assert err.value.variable == "DATA_DIR"


def test_br_032_secret_files_default_to_mounted_secrets() -> None:
    cfg = config.load(BASE)
    assert cfg.signing_key_file == "/run/secrets/signing_key"
    assert cfg.totp_secret_file == "/run/secrets/authorize_totp"  # noqa: S105


def test_br_032_secret_files_explicit() -> None:
    cfg = config.load(
        {**BASE, "SIGNING_KEY_FILE": "/s/k.pem", "AUTHORIZE_TOTP_SECRET_FILE": "/s/t"}
    )
    assert (cfg.signing_key_file, cfg.totp_secret_file) == ("/s/k.pem", "/s/t")


@pytest.mark.parametrize("variable", ["SIGNING_KEY_FILE", "AUTHORIZE_TOTP_SECRET_FILE"])
def test_br_032_secret_file_must_be_absolute(variable: str) -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, variable: "relative/file"})
    assert err.value.variable == variable


def test_br_032_no_secret_read_from_plain_variable() -> None:
    """No variable carries a secret in clear: only file paths are read."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(config.Config)}
    assert not {"signing_key", "totp_secret", "authorize_secret"} & fields


@pytest.mark.parametrize(
    "value", ["https://h.test/gw", "https://h.test/gw/", "https://h.test/?a=1"]
)
def test_br_032_public_url_with_path_refused(value: str) -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, "PUBLIC_URL": value})
    assert err.value.variable == "PUBLIC_URL"


def test_br_032_public_url_trailing_slash_and_port_admitted() -> None:
    assert config.load({**BASE, "PUBLIC_URL": "https://h.test:8443/"}).public_url


def test_br_032_v3_connections_key_file_default_and_explicit() -> None:
    """F-006 (BR-032 v3): `CONNECTIONS_KEY_FILE`, default `/run/secrets/connections_key`."""
    assert config.load(BASE).connections_key_file == "/run/secrets/connections_key"
    cfg = config.load({**BASE, "CONNECTIONS_KEY_FILE": "/srv/keys/conn"})
    assert cfg.connections_key_file == "/srv/keys/conn"
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, "CONNECTIONS_KEY_FILE": "relative/key"})
    assert err.value.variable == "CONNECTIONS_KEY_FILE"


def test_br_032_v3_invalid_connections_key_does_not_fail_startup(tmp_path: Path) -> None:
    """Invalid key: the configuration loads and the gateway stays ready (BR-088)."""
    bad = tmp_path / "connections_key"
    bad.write_text("not-base64-of-32-bytes\n")
    cfg = config.load({**BASE, "CONNECTIONS_KEY_FILE": str(bad), "DATA_DIR": str(tmp_path)})
    db = Database(cfg.db_path)
    assert db.open()
    try:
        assert db.call(cipher.ConnectionsKey(cfg.connections_key_file).status) == "key_invalid"
        assert db.readiness().ok
    finally:
        db.close()


# -- BR-032 v4 (F-016 lot A): ADMIN_RP_ID, TRUSTED_PROXIES, ADMIN_HOST, all optional -------------


def test_br_032_v4_new_variables_default_empty() -> None:
    cfg = config.load(BASE)
    assert (cfg.admin_rp_id, cfg.trusted_proxies, cfg.admin_host) == ("", (), "")
    assert cfg.admin.rp_id == "localhost"
    assert cfg.admin.origin == "http://localhost:14791"


def test_br_032_v4_trusted_proxies_are_cidrs() -> None:
    cfg = config.load({**BASE, "TRUSTED_PROXIES": "172.18.0.0/16, 10.0.0.1 ,2001:db8::/32"})
    assert [str(n) for n in cfg.trusted_proxies] == [
        "172.18.0.0/16",
        "10.0.0.1/32",
        "2001:db8::/32",
    ]
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, "TRUSTED_PROXIES": "172.18.0.0/16,traefik"})
    assert err.value.variable == "TRUSTED_PROXIES"


@pytest.mark.parametrize("value", [",", " ", ", ,", "\t\n"])
def test_br_032_v4_nonempty_trusted_proxies_must_yield_a_network(value: str) -> None:
    with pytest.raises(config.ConfigError, match="TRUSTED_PROXIES") as err:
        config.load({**BASE, "TRUSTED_PROXIES": value})
    assert err.value.variable == "TRUSTED_PROXIES"


def test_br_032_v4_empty_trusted_proxies_remain_optional() -> None:
    assert config.load({**BASE, "TRUSTED_PROXIES": ""}).trusted_proxies == ()


def test_br_032_v4_admin_host_is_a_hostname() -> None:
    cfg = config.load(
        {
            **BASE,
            "ADMIN_HOST": "Admin.Example.test",
            "ADMIN_URL": "https://admin.example.test",
            "TRUSTED_PROXIES": "10.0.0.0/8",
        }
    )
    assert cfg.admin_host == "admin.example.test"
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, "ADMIN_HOST": "https://admin.example.test"})
    assert err.value.variable == "ADMIN_HOST"


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:14791",  # BR-202: an IP origin cannot hold a passkey
        "http://admin.example.test",  # BR-204: http off loopback
        "https://admin.example.test/console",  # an exact origin, no path
        "https://admin.example.test?x=1",
    ],
)
def test_br_032_v4_admin_url_refusals_count_as_invalid_values(value: str) -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load({**BASE, "ADMIN_URL": value})
    assert err.value.variable == "ADMIN_URL"


def test_br_032_v4_admin_origin_normalises_the_default_port() -> None:
    cfg = config.load({**BASE, "ADMIN_URL": "https://Admin.Example.test:443/"})
    assert (cfg.admin.origin, cfg.admin.port, cfg.admin.secure) == (
        "https://admin.example.test",
        443,
        True,
    )
