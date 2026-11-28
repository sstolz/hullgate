"""F-008 acceptance instrument on the production server (AC-OP): `make verify-relay ROLE=<path>`.

Run in the container (`python -m hullgate.verify_relay <role> [<sensitive role>] [<tool>]`), with
the operator tokens read on standard input (BR-086: line 1 for the role, line 2 for the sensitive
role when one is named), it plays the F-008 checks only, none of F-004 to F-006, against the
container's local access point. `ROLE` is a tree path; the access point's request path carries the
organisation's identifier read from the store (F-014 §3.3). One line per check, with the exact
pattern `VERIFY-RELAY <check>: OK` or `VERIFY-RELAY <check>: KO <reason>` (`SKIP` for
`sensitive` without a sensitive role), then the banner `VERIFY-RELAY: n/5 OK` on every path
(F-010 §3.2, BR-131).

Checks: `list` (at least one tool, each prefixed by the prefix of a connection placed on the role,
64 characters at most), `call` (the tool named by the operator, read from the `list` output,
returns a result without `isError`), `refusal` (`zz__unknown` returns "unknown tool"),
`sensitive` (the named sensitive role returns an empty list), `output` (neither an operator token
read, nor an `hg_[A-Za-z0-9_-]{20,}` string, nor `Bearer ` in the output). The value of a
connection's token is not read by the instrument: its absence from the output is a clause read by
hand. Readings posted without threshold, as `VERIFY-RELAY reading <label>: <value>`: number of
tools and time of `list`, time of the `call`, version negotiated with the upstream (audit), length
of the longest public name, resolved addresses of the upstreams.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from hullgate import cli, config, relay, routes
from hullgate.tree import Tree

CHECKS = ("list", "call", "refusal", "sensitive", "output")
PREFIX = "VERIFY-RELAY"
UNKNOWN = "zz__unknown"
VERSION = "2025-06-18"
# Anchored on the left: the short prefix must not match inside another word (F-011 §3.3).
_LEAK = re.compile(r"(?<![A-Za-z0-9_-])hg_[A-Za-z0-9_-]{20,}")

# (request path of the access point, token, JSON-RPC body) → (status, JSON document or None)
Post = Callable[[str, str, dict[str, Any]], tuple[int, Any]]


class Report:
    def __init__(self, tokens: Sequence[str]) -> None:
        self.lines: list[str] = []
        self.results: dict[str, str | None] = {}
        self.skipped: set[str] = set()
        self.tokens = [t for t in tokens if t]

    def check(self, name: str, failure: str | None) -> None:
        self.results[name] = failure
        self.lines.append(
            f"{PREFIX} {name}: OK" if failure is None else f"{PREFIX} {name}: KO {failure}"
        )

    def skip(self, name: str, why: str) -> None:
        self.skipped.add(name)
        self.lines.append(f"{PREFIX} {name}: SKIP {why}")

    def reading(self, name: str, value: object) -> None:
        self.lines.append(f"{PREFIX} reading {name}: {value}")

    def finish(self) -> int:
        for name in CHECKS[:-1]:
            if name not in self.results and name not in self.skipped:
                self.check(name, "not played")
        text = "\n".join(self.lines)
        leaked = (
            any(t in text for t in self.tokens) or _LEAK.search(text) is not None
        ) or "Bearer " in text
        if leaked:  # the output is not printed: only safe lines are
            self.lines = [f"{PREFIX} {name}: KO output withheld" for name in CHECKS[:-1]]
            self.results = {name: "output withheld" for name in CHECKS[:-1]}
            self.skipped.clear()
        self.check("output", "secret value in the output" if leaked else None)
        passed = sum(1 for v in self.results.values() if v is None)
        for line in self.lines:
            print(line)
        print(f"{PREFIX}: {passed}/{len(CHECKS)} OK")
        return 0 if passed + len(self.skipped) == len(CHECKS) else 1


def local_post(port: int) -> Post:
    """MCP request to the container's local endpoint (no call outside the container)."""

    def post(target: str, token: str, body: dict[str, Any]) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
        try:
            conn.request(
                "POST",
                target,
                json.dumps(body),
                {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": VERSION,
                },
            )
            response = conn.getresponse()
            data = response.read()
        finally:
            conn.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, None

    return post


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _tools(post: Post, target: str, token: str) -> list[str] | str:
    status, doc = post(target, token, _rpc("tools/list"))
    if status != 200 or not isinstance(doc, dict) or "result" not in doc:
        return f"HTTP {status}"
    return [str(t.get("name")) for t in doc["result"].get("tools", [])]


def _context(db_path: str, role: str) -> tuple[list[str], list[str], str | None, str]:
    """Prefixes and hosts of the connections placed on the role; last negotiated version read
    from the call audit; the organisation's identifier (BR-160)."""
    conn = cli.open_store(db_path)
    try:
        tree = Tree.load(conn)
        node = tree.find(role)
        found = relay.carried(conn, tree, node.id) if node is not None else []
        rows = conn.execute(
            "SELECT detail FROM audit WHERE action = ? ORDER BY seq DESC LIMIT 50",
            (relay.A_CALL,),
        ).fetchall()
    finally:
        conn.close()
    found_versions = (json.loads(r[0]).get("version") for r in rows if r[0])
    version = next((v for v in found_versions if v), None)
    hosts = sorted({urlsplit(c.url).hostname or "" for c in found if c.url})
    return [c.prefix for c in found], hosts, version, tree.public_id


def run(
    role: str,
    sensitive: str | None,
    tool: str | None,
    tokens: Sequence[str],
    env: dict[str, str],
    post: Post | None = None,
) -> int:
    report = Report(tokens)
    try:
        cfg = config.load(env)
        prefixes, hosts, _, org_id = _context(cfg.db_path, role)
    except Exception as exc:  # configuration or database: no check can be played
        for check in CHECKS[:-1]:
            report.check(check, f"database or configuration unavailable ({type(exc).__name__})")
        return report.finish()
    send = post or local_post(cfg.public_port)
    token = tokens[0] if tokens else ""
    if not token:
        for check in CHECKS[:-1]:
            report.check(check, "operator token absent from standard input (BR-086)")
        return report.finish()
    target = routes.resource_path(org_id, role)
    started = time.monotonic()
    listed = _tools(send, target, token)
    elapsed = time.monotonic() - started
    if isinstance(listed, str):
        report.check("list", listed)
    else:
        report.reading("number of tools", len(listed))
        report.reading("list time (s)", f"{elapsed:.2f}")
        report.reading("longest public name", max((len(n) for n in listed), default=0))
        bad = [n for n in listed if len(n) > 64 or n.split("__")[0] not in prefixes]
        if not listed:
            report.check("list", "no tool (placement, state, upstream, sensitive role?)")
        elif bad:
            report.check("list", f"{len(bad)} name(s) outside a placed prefix or longer than 64")
        else:
            report.check("list", None)
        for line in listed:
            report.reading("tool", line)
    if tool is None:
        report.check("call", 'TOOL absent (a read tool read back from the "reading tool" lines)')
    else:
        started = time.monotonic()
        status, doc = send(target, token, _rpc("tools/call", {"name": tool, "arguments": {}}))
        report.reading("call time (s)", f"{time.monotonic() - started:.2f}")
        result = doc.get("result") if isinstance(doc, dict) else None
        if status != 200 or not isinstance(result, dict):
            error = doc.get("error", {}) if isinstance(doc, dict) else {}
            report.check("call", f"HTTP {status}, error {error.get('code')}")
        elif result.get("isError"):
            text = str((result.get("content") or [{}])[0].get("text", ""))[:60]
            report.check("call", f"isError ({text})")
        else:
            report.check("call", None)
    status, doc = send(target, token, _rpc("tools/call", {"name": UNKNOWN, "arguments": {}}))
    code = doc.get("error", {}).get("code") if isinstance(doc, dict) else None
    report.check("refusal", None if status == 200 and code == -32602 else f"HTTP {status} {code}")
    if sensitive is None:
        report.skip("sensitive", "no sensitive role named")
    elif len(tokens) < 2 or not tokens[1]:
        report.check("sensitive", "sensitive role token absent (2nd line of standard input)")
    else:
        got = _tools(send, routes.resource_path(org_id, sensitive), tokens[1])
        if isinstance(got, str):
            report.check("sensitive", got)
        else:
            report.check("sensitive", None if not got else f"{len(got)} tool(s) announced")
    try:
        _, _, version, _ = _context(cfg.db_path, role)
    except Exception:
        version = None
    report.reading("version negotiated with the upstream (audit)", version or "not read")
    for host in hosts:
        try:
            found = sorted({a[4][0] for a in socket.getaddrinfo(host, None)})
        except OSError:
            found = ["resolution failed"]
        report.reading(f"addresses of {host}", ", ".join(str(a) for a in found))
    return report.finish()


def main(argv: Sequence[str]) -> int:
    lines = sys.stdin.read(64 * 1024).splitlines()
    tokens = [line.strip() for line in lines[:2]]
    args = [a for a in argv if a]
    role = args[0] if args else ""
    if role.count("/") != 3:
        report = Report(tokens)
        for check in CHECKS[:-1]:
            report.check(check, "ROLE absent or malformed (read back from hullgate tree show)")
        return report.finish()
    sensitive = args[1] if len(args) > 1 and args[1] != "-" else None
    tool = args[2] if len(args) > 2 and args[2] != "-" else None
    return run(role, sensitive, tool, tokens, dict(os.environ))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
