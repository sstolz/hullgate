"""F-006 acceptance instrument on the production server (AC-OP).

Instrument: `make verify-connections CONNECTION=<name>`.

Run in the container (`python -m hullgate.verify_connections <client>/<name>`), it plays the F-006
checks only, none of F-004 or F-005. One line per check, with the exact pattern
`VERIFY-CONNECTIONS <check>: OK` or `VERIFY-CONNECTIONS <check>: KO <reason>`, then the banner
`VERIFY-CONNECTIONS: n/4 OK` on every path (F-010 §3.2, BR-131).

Checks: `state` (state `active`, account not empty), `token` (`get_access` gives a token valid for
>= 60 s, never printed), `renewal` (`connection renew` succeeds, expiry pushed back), `output` (no
secret value seen during the run in the instrument's output). Readings posted without threshold,
as `VERIFY-CONNECTIONS reading <label>: <value>`: `gitea-api` (HTTP status of
`GET <base URL>/api/v1/user` with the token given, through the bounded client of BR-084), observed
expiries, presence and rotation of the `refresh_token`, account, Gitea version.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Sequence

from hullgate import cli, config, connections, lifecycle
from hullgate.cipher import ConnectionsKey
from hullgate.outbound import Failure, Outbound
from hullgate.tree import Tree

CHECKS = ("state", "token", "renewal", "output")
PREFIX = "VERIFY-CONNECTIONS"


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.results: dict[str, str | None] = {}
        self.secrets: list[str] = []

    def check(self, name: str, failure: str | None) -> None:
        self.results[name] = failure
        self.lines.append(
            f"{PREFIX} {name}: OK" if failure is None else f"{PREFIX} {name}: KO {failure}"
        )

    def reading(self, name: str, value: object) -> None:
        self.lines.append(f"{PREFIX} reading {name}: {value}")

    def finish(self) -> int:
        for name in CHECKS[:-1]:
            if name not in self.results:
                self.check(name, "not run")
        leaked = any(s and s in line for s in self.secrets for line in self.lines)
        if leaked:
            self.check("output", "secret value in the output")
        elif self.results.get("token", "") is not None:
            self.check("output", "incomplete run (no token obtained)")
        else:
            self.check("output", None)
        if leaked:  # the output itself is not printed: only the safe lines are
            self.lines = [f"{PREFIX} {name}: KO output withheld" for name in CHECKS]
            self.results = {name: "output withheld" for name in CHECKS}
        passed = sum(1 for v in self.results.values() if v is None)
        for line in self.lines:
            print(line)
        print(f"{PREFIX}: {passed}/{len(CHECKS)} OK")
        return 0 if passed == len(CHECKS) else 1


def _tokens(
    conn: sqlite3.Connection, key: ConnectionsKey, connection_id: str
) -> dict[str, object] | None:
    blob = conn.execute(
        "SELECT tokens_enc FROM connection WHERE id = ?", (connection_id,)
    ).fetchone()
    if blob is None or blob[0] is None:
        return None
    data = json.loads(key.sealer(conn).open(blob[0], "connection", connection_id, "tokens"))
    return data if isinstance(data, dict) else None


def run(name: str, env: dict[str, str], out: Outbound | None = None) -> int:
    report = Report()
    try:
        cfg = config.load(env)
        conn = cli.open_store(cfg.db_path)
    except Exception as exc:  # configuration or database: no check can run
        for check in CHECKS[:-1]:
            report.check(check, f"database or configuration unavailable ({type(exc).__name__})")
        return report.finish()
    key = ConnectionsKey(cfg.connections_key_file)
    client = out or Outbound()
    runner = lifecycle.Direct(conn)
    try:
        try:
            [view] = connections.views(conn, key, name)
        except Exception as exc:
            report.check("state", f"connection unreadable ({exc})")
            return report.finish()
        report.reading("account", view.account)
        ok_state = view.state == "active" and view.account not in ("", "unknown")
        report.check("state", None if ok_state else f"state {view.state}, account {view.account}")
        tree = Tree.load(conn)
        row = connections._find(conn, tree, name)
        service = connections._service(conn, row.service_id)
        if not view.placements:
            report.check("token", "connection placed on no role (connection place)")
            return report.finish()
        role = tree.resolve(view.placements[0][0])
        try:
            access = lifecycle.access(runner, key, client, row.id, role.id)
        except connections.AccessError as exc:
            report.check("token", exc.code)
            return report.finish()
        token = access.authorization.removeprefix("Bearer ")
        report.secrets.append(token)
        before = _tokens(conn, key, row.id) or {}
        refresh = before.get("refresh_token")
        if isinstance(refresh, str):
            report.secrets.append(refresh)
        left = None if access.expires_at is None else access.expires_at - int(time.time())
        report.check("token", None if left is not None and left >= 60 else f"expiry {left}")
        report.reading("expiry before renewal (s)", left)
        report.reading("refresh_token", "present" if refresh else "absent")
        try:
            api = client.request(
                "GET",
                f"{service.base_url}/api/v1/user",
                headers={"Authorization": access.authorization},
                internal=connections.internal_of(service),
            )
            report.reading("gitea-api", api.status)
        except Failure as exc:
            report.reading("gitea-api", f"failed ({exc.reason})")
        try:
            version = client.request(
                "GET",
                f"{service.base_url}/api/v1/version",
                internal=connections.internal_of(service),
            )
            report.reading("version", version.json().get("version", "unknown"))
        except Failure as exc:
            report.reading("version", f"failed ({exc.reason})")
        try:
            _name, renewed = lifecycle.renew_now(runner, key, client, name)
        except Exception as exc:
            report.check("renewal", f"{type(exc).__name__} {exc}")
            return report.finish()
        report.secrets.append(renewed.authorization.removeprefix("Bearer "))
        after = _tokens(conn, key, row.id) or {}
        new_refresh = after.get("refresh_token")
        if isinstance(new_refresh, str):
            report.secrets.append(new_refresh)
        # The renewal is proven by the generation (+ 1 for each set written); the deadline, to
        # the second, may be equal when `token` already renewed within the same second
        # (review 419 m1): it must only not move back.
        pushed = (
            renewed.generation > access.generation
            and renewed.expires_at is not None
            and access.expires_at is not None
            and renewed.expires_at >= access.expires_at
        )
        report.check("renewal", None if pushed else "deadline not pushed back")
        left_after = None if renewed.expires_at is None else renewed.expires_at - int(time.time())
        report.reading("expiry after renewal (s)", left_after)
        rotated = "yes" if new_refresh and new_refresh != refresh else "no"
        report.reading("refresh_token rotated", rotated)
        return report.finish()
    finally:
        conn.close()


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1 or argv[0].count("/") != 1:
        report = Report()
        for check in CHECKS[:-1]:
            report.check(check, "CONNECTION absent or malformed (read back from connection list)")
        return report.finish()
    return run(argv[0], dict(os.environ))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
