"""Gateway configuration: environment variables and mounted secrets (BR-032 v3).

F-016 lot A (BR-032 v4): `ADMIN_RP_ID`, `TRUSTED_PROXIES` and `ADMIN_HOST` join, all optional;
`ADMIN_URL` is the exact origin of the console, and BR-202's and BR-204's refusals count as invalid
values (an IP origin, an RP id that is neither its host nor a parent domain, `http` off loopback).

Secrets are never read from a plain variable: the `*_FILE` variables name mounted files (signing
key, TOTP secret of the authorization barrier), read by `hullgate.keys`, and the connections key
(`CONNECTIONS_KEY_FILE`, F-006), read by `hullgate.cipher`. A connections key that is absent,
invalid or different does not prevent startup (BR-088).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlsplit

DEFAULT_PUBLIC_PORT = 4790
DEFAULT_ADMIN_PORT = 4791
DEFAULT_DATA_DIR = "/data"
DB_FILENAME = "hullgate.db"
DEFAULT_SIGNING_KEY_FILE = "/run/secrets/signing_key"
DEFAULT_TOTP_SECRET_FILE = "/run/secrets/authorize_totp"  # noqa: S105 (a path)
DEFAULT_CONNECTIONS_KEY_FILE = "/run/secrets/connections_key"


class ConfigError(ValueError):
    """Required variable absent or invalid; `variable` names the variable at fault."""

    def __init__(self, variable: str, reason: str) -> None:
        super().__init__(f"{variable}: {reason}")
        self.variable = variable
        self.reason = reason


type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class Config:
    public_url: str
    admin_url: str
    public_port: int
    admin_port: int
    data_dir: str
    signing_key_file: str = DEFAULT_SIGNING_KEY_FILE
    totp_secret_file: str = DEFAULT_TOTP_SECRET_FILE
    connections_key_file: str = DEFAULT_CONNECTIONS_KEY_FILE
    admin_rp_id: str = ""
    trusted_proxies: tuple[Network, ...] = field(default=())
    admin_host: str = ""

    @property
    def db_path(self) -> str:
        return str(PurePosixPath(self.data_dir) / DB_FILENAME)

    @property
    def admin(self) -> AdminOrigin:
        return admin_origin(self.admin_url, self.admin_rp_id)


@dataclass(frozen=True, slots=True)
class AdminOrigin:
    """The console's origin as WebAuthn and the cookie see it (BR-202, BR-204, BR-206)."""

    origin: str  # scheme://host[:port], no path, no trailing slash
    host: str  # lower-case hostname
    port: int  # explicit or the scheme's default
    secure: bool  # https
    rp_id: str

    @property
    def loopback(self) -> bool:
        return is_loopback_name(self.host)


def is_loopback_name(host: str) -> bool:
    """`localhost` and its subdomains: the names WebAuthn treats as a secure context over http."""
    return host == "localhost" or host.endswith(".localhost")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def admin_origin(admin_url: str, rp_id: str = "") -> AdminOrigin:
    """Origin of `ADMIN_URL` with its RP id; `ConfigError` on BR-202's and BR-204's refusals."""
    parts = urlsplit(admin_url)
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ConfigError("ADMIN_URL", "exact origin expected (scheme://host[:port]), no path")
    host = (parts.hostname or "").lower()
    if not host:
        raise ConfigError("ADMIN_URL", "absolute URL expected (http:// or https://)")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ConfigError("ADMIN_URL", "port out of range") from exc
    secure = parts.scheme == "https"
    if _is_ip(host):
        raise ConfigError(
            "ADMIN_URL", "a hostname is expected, not an IP address (WebAuthn, BR-202)"
        )
    if not secure and not is_loopback_name(host):
        raise ConfigError("ADMIN_URL", "https is required except on localhost (BR-204)")
    rp = (rp_id.strip() or host).lower()
    if _is_ip(rp):
        raise ConfigError("ADMIN_RP_ID", "a domain is expected, not an IP address (BR-202)")
    if not (host == rp or host.endswith("." + rp)):
        raise ConfigError(
            "ADMIN_RP_ID", "must be the host of ADMIN_URL or one of its parent domains (BR-202)"
        )
    default_port = 443 if secure else 80
    origin = f"{parts.scheme}://{host}" + ("" if port == default_port else f":{port}")
    return AdminOrigin(origin=origin, host=host, port=port, secure=secure, rp_id=rp)


def _proxies(env: Mapping[str, str]) -> tuple[Network, ...]:
    raw = env.get("TRUSTED_PROXIES", "")
    if not raw:
        return ()
    networks: list[Network] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ConfigError("TRUSTED_PROXIES", "comma-separated CIDRs expected") from exc
    if not networks:
        raise ConfigError("TRUSTED_PROXIES", "at least one CIDR expected")
    return tuple(networks)


_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")


def _admin_host(env: Mapping[str, str]) -> str:
    raw = env.get("ADMIN_HOST", "").strip().lower()
    if raw and not _HOSTNAME.match(raw):
        raise ConfigError("ADMIN_HOST", "a hostname is expected")
    return raw


def _url(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(name, "required variable absent")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(name, "absolute URL expected (http:// or https://)")
    if name == "PUBLIC_URL" and (parts.path not in ("", "/") or parts.query or parts.fragment):
        # BR-032 v2 (S14): the RFC 8414 / 9728 path insertion would become ambiguous.
        raise ConfigError(name, "URL without a path expected (https://host[:port])")
    return value


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise ConfigError(name, "integer between 1 and 65535 expected")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ConfigError(name, "integer between 1 and 65535 expected")
    return port


def _data_dir(env: Mapping[str, str]) -> str:
    raw = env.get("DATA_DIR", "").strip() or DEFAULT_DATA_DIR
    if not raw.startswith("/"):
        raise ConfigError("DATA_DIR", "absolute path expected")
    return raw


def _secret_file(env: Mapping[str, str], name: str, default: str) -> str:
    raw = env.get(name, "").strip() or default
    if not raw.startswith("/"):
        raise ConfigError(name, "absolute path expected (mounted secret file)")
    return raw


def load(env: Mapping[str, str]) -> Config:
    """Builds the configuration; raises `ConfigError` at the first faulty variable."""
    public_url = _url(env, "PUBLIC_URL")
    admin_url = _url(env, "ADMIN_URL")
    public_port = _port(env, "PUBLIC_PORT", DEFAULT_PUBLIC_PORT)
    admin_port = _port(env, "ADMIN_PORT", DEFAULT_ADMIN_PORT)
    if public_port == admin_port:
        raise ConfigError("ADMIN_PORT", "must differ from PUBLIC_PORT")
    rp_id = env.get("ADMIN_RP_ID", "").strip()
    admin_origin(admin_url, rp_id)  # BR-202, BR-204: refused at startup
    host = _admin_host(env)
    proxies = _proxies(env)
    if host:
        if admin_url != f"https://{host}":
            raise ConfigError("ADMIN_URL", "must equal https://${ADMIN_HOST} for public routing")
        if not proxies:
            raise ConfigError("TRUSTED_PROXIES", "required when ADMIN_HOST opens the public route")
    return Config(
        public_url=public_url,
        admin_url=admin_url,
        public_port=public_port,
        admin_port=admin_port,
        data_dir=_data_dir(env),
        signing_key_file=_secret_file(env, "SIGNING_KEY_FILE", DEFAULT_SIGNING_KEY_FILE),
        totp_secret_file=_secret_file(env, "AUTHORIZE_TOTP_SECRET_FILE", DEFAULT_TOTP_SECRET_FILE),
        connections_key_file=_secret_file(
            env, "CONNECTIONS_KEY_FILE", DEFAULT_CONNECTIONS_KEY_FILE
        ),
        admin_rp_id=rp_id,
        trusted_proxies=proxies,
        admin_host=host,
    )
