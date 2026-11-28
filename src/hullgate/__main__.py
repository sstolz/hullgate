"""Entry point: `python -m hullgate` (or `hullgate`).

Without arguments: configuration, migrated database, two HTTP listeners. With arguments: the
administration CLI (`hullgate tree show`, `hullgate token create …`, SCR-02).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Generator
from pathlib import Path

import uvicorn

from hullgate import cli
from hullgate import config as config_mod
from hullgate.app import build_admin_app, build_public_app
from hullgate.cipher import ConnectionsKey
from hullgate.db import Database
from hullgate.keys import SigningKey, TotpSecret
from hullgate.vault import Vault

log = logging.getLogger("hullgate")

# Listens inside the container; exposure is set by compose (BR-034, BR-041).
LISTEN_HOST = "0.0.0.0"  # noqa: S104
# The console's static bundle, copied by the image's console build stage (BR-212).
CONSOLE_DIR = Path("/app/console")


class _Server(uvicorn.Server):
    """Uvicorn server that leaves signals to the process (two servers, one loop)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def configs(cfg: config_mod.Config, db: Database) -> list[uvicorn.Config]:
    """The two listeners. `proxy_headers=False` on both: uvicorn would otherwise rewrite the client
    address from forwarded headers before BR-208's function reads it. The admin listener writes no
    access log: its paths carry enrolment links (BR-203), later OAuth codes (BR-216)."""
    key = SigningKey(cfg.signing_key_file)
    totp = TotpSecret(cfg.totp_secret_file)
    vault = Vault(db, key, totp)
    return [
        uvicorn.Config(
            build_public_app(
                db,
                cfg.public_url,
                admin_origin=cfg.admin,
                key=key,
                totp=totp,
                connections_key=ConnectionsKey(cfg.connections_key_file),
                proxies=cfg.trusted_proxies,
            ),
            host=LISTEN_HOST,
            port=cfg.public_port,
            log_level="info",
            server_header=False,
            proxy_headers=False,
        ),
        uvicorn.Config(
            build_admin_app(
                db,
                cfg.admin,
                public_url=cfg.public_url,
                proxies=cfg.trusted_proxies,
                readiness=vault.readiness,
                static_dir=CONSOLE_DIR if CONSOLE_DIR.is_dir() else None,
                connections_key=ConnectionsKey(cfg.connections_key_file),
            ),
            host=LISTEN_HOST,
            port=cfg.admin_port,
            log_level="info",
            server_header=False,
            proxy_headers=False,
            access_log=False,
        ),
    ]


async def _serve(cfg: config_mod.Config, db: Database) -> None:
    servers = [_Server(c) for c in configs(cfg, db)]
    loop = asyncio.get_running_loop()

    def stop() -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        cfg = config_mod.load(os.environ)
    except config_mod.ConfigError as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    db = Database(cfg.db_path)
    db.open()
    log.info(
        "public listener %s (port %d), administration %s (port %d), database %s",
        cfg.public_url,
        cfg.public_port,
        cfg.admin_url,
        cfg.admin_port,
        cfg.db_path,
    )
    try:
        asyncio.run(_serve(cfg, db))
    finally:
        db.close()
    return 0


def entry() -> int:
    """`hullgate` script: the CLI when arguments are given, the gateway otherwise."""
    if len(sys.argv) > 1:
        return cli.run(sys.argv[1:], os.environ)
    return main()


if __name__ == "__main__":
    sys.exit(entry())
