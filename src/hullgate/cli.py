"""Administration CLI in the container: `hullgate <group> <command>` (SCR-02).

Audit actor: `operator-cli`. Exit code 0 on success, 2 on refusal (message naming the violated
constraint, nothing written), 1 on internal error or database unavailable (nothing written, no
token printed). `--json` for the read commands. The `admin` prefix is accepted
(`hullgate admin tree show`).

F-016 lot A: the `admin` group itself, host only by design (BR-211's exclusions): `admin bootstrap
<email>` (BR-201, prints `ENROLMENT-LINK: <url>` once), `admin list`, `admin attempts clear`
(BR-207), `admin allowlist clear` (BR-235). `hullgate admin <verb>` reaches it when `<verb>` is one
of these; any other verb keeps the historical prefix form.

F-020 (BR-310): `consent list` prints the pending authorisation requests, `consent approve
<reference>` approves one from the host; host only, like the `admin` group.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn, TextIO

from hullgate import (
    audit,
    changes,
    clients,
    config,
    connections,
    importing,
    lifecycle,
    migrations,
    oauth,
    pinning,
    policy,
    preview,
    relay,
    routes,
    tokens,
    tree,
)
from hullgate.admin import accounts
from hullgate.cipher import ConnectionsKey, KeyUnavailable
from hullgate.db import StoreUnavailable, connect
from hullgate.export import export_yaml
from hullgate.outbound import Outbound
from hullgate.tree import Clock, Refused, Tree
from hullgate.upstream_mcp import Transport, UpstreamClient, UpstreamError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


class _Parser(argparse.ArgumentParser):
    """Usage error: exit 2, like a refusal, without exiting the calling process.

    The message never echoes a value given as an argument (BR-086): a secret passed by mistake on
    the command line (`--token X`) is copied neither into the output nor anywhere else.
    """

    def error(self, message: str) -> NoReturn:
        if "unrecognized arguments" in message:
            message = (
                "unrecognized argument (value not echoed; a secret is read from a file or "
                "from standard input: --file, --secret-file, with - for standard input)"
            )
        message = re.sub(r"(invalid [^:]*: )'[^']*'", r"\1«value not echoed»", message)
        raise Refused(f"usage: {message}")


@functools.cache
def _parser() -> argparse.ArgumentParser:
    """The CLI parser, built once per process: building it (≈ 70 ms, nearly all of a call) depends
    on no argument; `parse_args` returns a fresh namespace on every call."""
    p = _Parser(prog="hullgate", description="Gateway administration (CLI).")
    groups = p.add_subparsers(dest="group", required=True, parser_class=_Parser)

    t = groups.add_parser("tree", help="tree").add_subparsers(dest="cmd", required=True)
    a = t.add_parser("add")
    a.add_argument("parent")
    a.add_argument("level")
    a.add_argument("name")
    a.add_argument("--slug")
    r = t.add_parser("rename")
    r.add_argument("path")
    r.add_argument("name")
    r.add_argument("--slug")
    m = t.add_parser("move")
    m.add_argument("path")
    m.add_argument("new_parent")
    m.add_argument("--confirm")
    d = t.add_parser("delete")
    d.add_argument("path")
    d.add_argument("--confirm")
    s = t.add_parser("show")
    s.add_argument("path", nargs="?", default="/")
    s.add_argument("--json", action="store_true")
    sv = t.add_parser("sensitive")
    sv.add_argument("path")
    sv.add_argument("state", choices=["on", "off"])
    for name in ("lock", "unlock"):
        t.add_parser(name).add_argument("path")

    st = groups.add_parser("settings", help="inherited settings").add_subparsers(
        dest="cmd", required=True
    )
    g = st.add_parser("get")
    g.add_argument("path")
    g.add_argument("--json", action="store_true")
    ss = st.add_parser("set")
    ss.add_argument("path")
    ss.add_argument("key")
    ss.add_argument("value")
    su = st.add_parser("unset")
    su.add_argument("path")
    su.add_argument("key")

    j = groups.add_parser("token", help="operator tokens").add_subparsers(dest="cmd", required=True)
    c = j.add_parser("create")
    c.add_argument("--path", dest="path", required=True)
    c.add_argument("--label", required=True)
    c.add_argument("--duration", dest="duration")
    c.add_argument("--confirm")
    c.add_argument("--json", action="store_true")
    li = j.add_parser("list")
    li.add_argument("path", nargs="?")
    li.add_argument("--client")
    li.add_argument("--json", action="store_true")
    rv = j.add_parser("revoke")
    rv.add_argument("id")

    au = groups.add_parser("audit", help="audit log").add_subparsers(dest="cmd", required=True)
    ta = au.add_parser("tail")
    ta.add_argument("-n", type=int, default=20)
    ta.add_argument("--json", action="store_true")

    cl = groups.add_parser("client", help="OAuth clients").add_subparsers(dest="cmd", required=True)
    ca = cl.add_parser("add")
    ca.add_argument("client_id")
    ca.add_argument("--path", dest="path", required=True)
    ca.add_argument("--redirect-uri", dest="redirect_uris", action="append", required=True)
    ca.add_argument("--name")
    cls = cl.add_parser("list")
    cls.add_argument("--json", action="store_true")
    cl.add_parser("remove").add_argument("client_id")
    cl.add_parser("dcr").add_argument("state", choices=["on", "off"])

    groups.add_parser("export", help="YAML export of the tree")
    imp = groups.add_parser("import", help="preview or atomically apply an exported YAML document")
    mode = imp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply", metavar="DIGEST")
    imp.add_argument("--apply-deletions", action="store_true")
    imp.add_argument("--confirm-share", action="store_true")
    imp.add_argument("file")
    undo = groups.add_parser("undo", help="preview or reverse one history entry")
    undo.add_argument("seq", type=int)
    undo.add_argument("--preview", action="store_true")
    undo.add_argument("--impact")
    undo.add_argument("--confirm-share", action="store_true")

    # F-006: connection library. No secret as an argument (BR-086).
    svc = groups.add_parser("service", help="upstream services").add_subparsers(
        dest="cmd", required=True
    )
    sd = svc.add_parser("declare")
    sd.add_argument("id")
    sd.add_argument("--server", required=True)
    sd.add_argument("--model", choices=list(connections.MODELS))
    sd.add_argument("--scopes")
    sd.add_argument("--internal-host", dest="internal", action="store_true")
    svc.add_parser("list").add_argument("--json", action="store_true")
    svc.add_parser("remove").add_argument("id")
    srd = svc.add_parser("rediscover")
    srd.add_argument("id")
    srd.add_argument("--confirm")
    # F-008: MCP upstream of a service (BR-106)
    sam = svc.add_parser("upstream")
    sam.add_argument("id")
    sam.add_argument("--type", dest="kind", choices=list(connections.UPSTREAM_TYPES))
    sam.add_argument("--mcp-url", dest="mcp_url")
    sam.add_argument("--mcp-internal-host", dest="mcp_internal", action="store_true")
    sam.add_argument("--remove", dest="remove", action="store_true")
    sam.add_argument("--confirm")

    ap = groups.add_parser("application", help="outbound OAuth applications").add_subparsers(
        dest="cmd", required=True
    )
    ad = ap.add_parser("declare")
    ad.add_argument("id")
    ad.add_argument("--service", required=True)
    ad.add_argument("--mode", choices=["declared", "dynamic"], required=True)
    ad.add_argument("--client-id", dest="client_id")
    ad.add_argument("--secret-file", dest="secret_file")
    ap.add_parser("list").add_argument("--json", action="store_true")
    ap.add_parser("remove").add_argument("id")
    are = ap.add_parser("reregister")
    are.add_argument("id")
    are.add_argument("--confirm")

    cx = groups.add_parser("connection", help="connections").add_subparsers(
        dest="cmd", required=True
    )
    ca = cx.add_parser("add")
    ca.add_argument("name")
    ca.add_argument("--service", required=True)
    ca.add_argument("--application")
    ca.add_argument("--provided", action="store_true")
    ca.add_argument("--file", dest="file")
    ca.add_argument("--prefix", dest="prefix", required=True)
    ca.add_argument("--account", dest="account")
    ca.add_argument("--expires-at")
    cau = cx.add_parser("authorize")
    cau.add_argument("name")
    cau.add_argument("--callback", dest="callback", required=True)
    cau.add_argument("--confirm-account", dest="confirm_account", action="store_true")
    cr = cx.add_parser("reauthorize")
    cr.add_argument("name")
    cr.add_argument("--file", dest="file")
    cr.add_argument("--account", dest="account")
    cr.add_argument("--expires-at")
    cx.add_parser("renew").add_argument("name")
    cx.add_parser("revoke").add_argument("name")
    cpx = cx.add_parser("prefix")
    cpx.add_argument("name")
    cpx.add_argument("prefix")
    cpl = cx.add_parser("place")
    cpl.add_argument("name")
    cpl.add_argument("role")
    cpl.add_argument("--confirm-share", dest="confirm_share", action="store_true")
    cup = cx.add_parser("unplace")
    cup.add_argument("name")
    cup.add_argument("role")
    ce = cx.add_parser("status")
    ce.add_argument("name", nargs="?")
    ce.add_argument("--json", action="store_true")
    cx.add_parser("list").add_argument("--json", action="store_true")
    crm = cx.add_parser("remove")
    crm.add_argument("name")
    crm.add_argument("--confirm")
    # F-008: diagnosis of a placed connection (§3.2)
    cto = cx.add_parser("tools")
    cto.add_argument("name")
    cto.add_argument("--role", required=True)
    cto.add_argument("--purge-names", dest="purge", action="store_true")
    cto.add_argument("--confirm")
    cto.add_argument("--json", action="store_true")
    # F-015: action policy per placement (SCR-01) and tool catalogue (SCR-02)
    po = groups.add_parser("policy", help="action policy").add_subparsers(dest="cmd", required=True)
    psh = po.add_parser("show")
    psh.add_argument("name")
    psh.add_argument("--role", required=True)
    psh.add_argument("--json", action="store_true")
    pmo = po.add_parser("mode")
    pmo.add_argument("name")
    pmo.add_argument("--role", required=True)
    pmo.add_argument("mode", choices=["read", "write", "destructive", "inherit"])
    for verb in ("add", "remove"):
        pl = po.add_parser(verb)
        pl.add_argument("name")
        pl.add_argument("--role", required=True)
        pl.add_argument("list", choices=["open", "deny", "allowlist"])
        pl.add_argument("tools", nargs="*")
        if verb == "add":
            pl.add_argument("--class", dest="by_class", choices=["read"])
    par = po.add_parser("arg")
    par.add_argument("name")
    par.add_argument("--role", required=True)
    par.add_argument("tool")
    par.add_argument("argument")
    par.add_argument("values", nargs="+")
    pam = po.add_parser("arg-remove")
    pam.add_argument("name")
    pam.add_argument("--role", required=True)
    pam.add_argument("tool")
    ppn = po.add_parser("pin")
    ppn.add_argument("name")
    ppn.add_argument("--role", required=True)
    ppn.add_argument("pin", choices=["on", "off", "inherit"])
    for verb in ("pin-tool", "unpin-tool"):
        ppt = po.add_parser(verb)
        ppt.add_argument("name")
        ppt.add_argument("--role", required=True)
        ppt.add_argument("tool")
    ppv = po.add_parser("preview")
    ppv.add_argument("role")
    ppv.add_argument("--json", action="store_true")
    to = groups.add_parser("tool", help="tool catalogue").add_subparsers(dest="cmd", required=True)
    tq = to.add_parser("queue")
    tq.add_argument("--service")
    tq.add_argument("--json", action="store_true")
    tc = to.add_parser("classify")
    tc.add_argument("service")
    tc.add_argument("tool")
    tc.add_argument("cls", metavar="class", choices=["read", "write", "destructive", "clear"])
    tac = to.add_parser("accept")
    tac.add_argument("service")
    tac.add_argument("tool")
    tdf = to.add_parser("diff")
    tdf.add_argument("path")
    tdf.add_argument("--json", action="store_true")
    tva = to.add_parser("validate")
    tva.add_argument("path")
    tva.add_argument("name")
    tva.add_argument("--tool")
    # F-016 lot A: the console's administrators, host only (BR-201, BR-207, BR-235)
    adm = groups.add_parser("admin", help="console administrators").add_subparsers(
        dest="cmd", required=True
    )
    adm.add_parser("recover").add_argument("email")
    ab = adm.add_parser("bootstrap")
    ab.add_argument("email")
    ab.add_argument("--name")
    adm.add_parser("list").add_argument("--json", action="store_true")
    adm.add_parser("attempts").add_argument("action", choices=["clear"])
    adm.add_parser("allowlist").add_argument("action", choices=["clear"])
    # F-020: pending authorisation requests, approved from the host (BR-310)
    co = groups.add_parser("consent", help="pending authorisation requests").add_subparsers(
        dest="cmd", required=True
    )
    co.add_parser("list").add_argument("--json", action="store_true")
    co.add_parser("approve").add_argument("reference")
    return p


# Verbs of the `admin` group; `hullgate admin <other>` keeps the prefix form (`admin tree show`).
ADMIN_VERBS = frozenset({"bootstrap", "recover", "list", "attempts", "allowlist"})


SECRET_MAX = connections.SECRET_MAX


def read_secret(source: str, inp: TextIO) -> str:
    """Value read from a file or from standard input (`-`), checked by `secret_value` (BR-086):
    final newline removed, refused when too long, empty or on several lines.

    Neither the path nor the value is echoed in a message: a value passed by mistake in place of
    the path is not copied.
    """
    try:
        if source == "-":
            data = inp.read(SECRET_MAX + 1)
        else:
            with open(source, encoding="utf-8") as f:
                data = f.read(SECRET_MAX + 1)
    except (OSError, UnicodeDecodeError):
        raise Refused("secret file unreadable (path not echoed)") from None
    return connections.secret_value(data)


def _state_label(state: str) -> str:
    """Text label of a connection state token; a key status token is printed as is."""
    return connections.STATE_LABELS.get(state, state)


def _same(a: str, b: str | None) -> bool:
    return b is not None and a.strip("/") == b.strip("/")


class Cli:
    def __init__(
        self,
        conn: sqlite3.Connection,
        public_url: str,
        out: TextIO,
        clock: Clock,
        key: ConnectionsKey | None = None,
        inp: TextIO | None = None,
        outbound: Outbound | None = None,
        upstream: UpstreamClient | None = None,
        err: TextIO | None = None,
    ):
        self.conn = conn
        self.err = sys.stderr if err is None else err
        self.public_url = public_url.rstrip("/")
        self.out = out
        self.clock = clock
        self.key = key if key is not None else ConnectionsKey(config.DEFAULT_CONNECTIONS_KEY_FILE)
        self.inp = sys.stdin if inp is None else inp
        self._outbound = outbound
        self._upstream = upstream
        self.run = lifecycle.Direct(conn)
        self.admin_origin = ""

    @property
    def outbound(self) -> Outbound:
        """Bounded outbound client (BR-084), created at the first call to a provider."""
        if self._outbound is None:
            self._outbound = Outbound()
        return self._outbound

    @property
    def upstream(self) -> UpstreamClient:
        """Bounded upstream MCP client (BR-107), created once at the first call (BR-125)."""
        if self._upstream is None:
            self._upstream = UpstreamClient(Transport())
        return self._upstream

    def web_redirect(self) -> str | None:
        """The console's return URI (BR-216), registered beside the loopback one by a dynamic
        registration; None when the CLI runs without an `ADMIN_URL`."""
        return connections.web_redirect_uri(self.admin_origin) if self.admin_origin else None

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def dump(self, data: object) -> None:
        print(json.dumps(data, ensure_ascii=False, indent=2), file=self.out)

    def url(self, path: str) -> str:
        """The access point's URL (BR-044 v2), from `PUBLIC_URL` and the stored identifier."""
        return routes.resource_url(self.public_url, tree.organisation_id(self.conn), path)

    # -- tree --------------------------------------------------------------------------------

    def tree(self, ns: argparse.Namespace) -> int:
        conn, clock = self.conn, self.clock
        if ns.cmd == "add":
            added = tree.add(conn, ns.parent, ns.level, ns.name, ns.slug, clock=clock)
            t = Tree.load(conn)
            for path in added.created:
                node = t.resolve(path)
                extra = f"  {self.url(path)}" if node.level == "role" else ""
                self.say(f"created: {path} ({tree.LEVEL_LABELS[node.level]}){extra}")
            return EXIT_OK
        if ns.cmd == "rename":
            renamed = tree.rename(conn, ns.path, ns.name, ns.slug, clock=clock)
            self.say(f"renamed: {renamed.old_path} -> {renamed.new_path}")
            if renamed.old_path != renamed.new_path:
                self.say(f"OAuth lineages revoked: {renamed.lineages_revoked}")
            return EXIT_OK
        if ns.cmd == "move":
            plan = tree.move_impact(conn, ns.path, ns.new_parent, clock=clock)
            self.say(f"move: {plan.path} -> {plan.new_path}")
            self.say(f"  access points whose URL changes: {plan.roles}")
            self.say(f"  active OAuth lineages revoked: {plan.lineages}")
            self.say(f"  active tokens of former ancestors that stop covering: {plan.tokens_lost}")
            self.say(f"  active tokens of new ancestors that cover: {plan.tokens_gained}")
            self.signal_shares(plan.shares)
            if not _same(plan.path, ns.confirm):
                raise Refused(f"confirmation required: --confirm {plan.path}")
            tree.move(conn, ns.path, ns.new_parent, clock=clock)
            self.say(f"moved: {plan.new_path}")
            return EXIT_OK
        if ns.cmd == "delete":
            impact = tree.delete_impact(conn, ns.path, clock=clock)
            self.say(f"deletion: {impact.path}")
            self.say(f"  descendants: {impact.descendants}")
            self.say(f"  active tokens revoked: {impact.active_tokens}")
            self.say(f"  URLs that stop answering: {impact.urls}")
            self.say(f"  connection placements removed: {impact.placements}")
            if not _same(impact.path, ns.confirm):
                raise Refused(f"confirmation required: --confirm {impact.path}")
            tree.delete(conn, ns.path, clock=clock)
            self.say(f"deleted: {impact.path}")
            return EXIT_OK
        if ns.cmd == "show":
            return self.show(ns.path, ns.json)
        if ns.cmd == "sensitive":
            on = ns.state == "on"
            change = tree.sensitive_impact(conn, ns.path, on, clock=clock)
            if not on:
                self.say(
                    f"active tokens on the ancestors, accepted again: {change.reopened_tokens}"
                )
            else:
                self.say(
                    f"active tokens on the environment, which will no longer pass: "
                    f"{change.env_tokens}"
                )
                self.say(
                    f"active role tokens and OAuth lineages valid more than 7 days "
                    f"(not revoked): "
                    f"{change.long_role_tokens}"
                )
            self.signal_shares(change.shares)
            tree.set_sensitive(conn, ns.path, on, clock=clock)
            self.say(f"{change.path}: {'sensitive' if on else 'non-sensitive'}")
            return EXIT_OK
        if ns.cmd == "lock":
            done = tree.lock(conn, ns.path, clock=clock)
            self.say(f"locked: {done.origin}")
            return EXIT_OK
        done = tree.unlock(conn, ns.path, clock=clock)
        self.say(f"unlocked: {ns.path.strip('/') or '/'}")
        return EXIT_OK

    def show(self, path: str, as_json: bool) -> int:
        t = Tree.load(self.conn)
        root = t.resolve(path)

        def describe(node: tree.Node) -> dict[str, Any]:
            p = t.path(node)
            locker = t.locked_by(node)
            item: dict[str, Any] = {
                "path": p,
                "level": node.level,
                "segment": node.slug,
                "name": node.name,
                "sensitive": t.is_sensitive(node),
                "locked_by": t.path(locker) if locker is not None else None,
            }
            if node.level == "role":
                item["url"] = self.url(p)
            if node.level == "org":
                item["public_id"] = t.public_id  # F-014 §3.3: the organisation's item only
            return item

        nodes = t.subtree(root)
        if as_json:
            self.dump([describe(n) for n in nodes])
            return EXIT_OK
        base = len(t.lineage_of(root))
        for node in nodes:
            item = describe(node)
            indent = "  " * (len(t.lineage_of(node)) - base)
            label = "/" if node.level == "org" else node.slug
            parts = [f"{indent}{label}", f"« {node.name} »", tree.LEVEL_LABELS[node.level]]
            if node.level in ("env", "role"):
                parts.append("sensitive" if item["sensitive"] else "non-sensitive")
            if item["locked_by"] is not None:
                parts.append(f"locked by {item['locked_by']}")
            if node.level == "role":
                parts.append(item["url"])
            if node.level == "org":
                parts.append(item["public_id"])
            self.say("  ".join(parts))
        return EXIT_OK

    # -- settings ----------------------------------------------------------------------------

    def settings(self, ns: argparse.Namespace) -> int:
        if ns.cmd == "get":
            values = tree.settings_get(self.conn, ns.path)
            if ns.json:
                self.dump([{"key": v.key, "value": v.value, "origin": v.origin} for v in values])
            else:
                for v in values:
                    self.say(f"{v.key} = {v.value}  (origin: {v.origin})")
            return EXIT_OK
        if ns.cmd == "set":
            done = tree.settings_set(self.conn, ns.path, ns.key, ns.value, clock=self.clock)
        else:
            done = tree.settings_unset(self.conn, ns.path, ns.key, clock=self.clock)
        self.say(f"{done.key} = {done.value}  (origin: {done.origin})")
        return EXIT_OK

    # -- tokens ------------------------------------------------------------------------------

    def tokens(self, ns: argparse.Namespace) -> int:
        cmd = ns.cmd
        if cmd == "create":
            issued = tokens.issue(
                self.conn, ns.path, ns.label, ns.duration, ns.confirm, clock=self.clock
            )
            if ns.json:
                self.dump(
                    {
                        "id": issued.id,
                        "value": issued.value,
                        "path": issued.path,
                        "label": issued.label,
                        "expires_at": audit.utc(issued.expires_at),
                        "uncovered_sensitive": list(issued.uncovered_sensitive),
                    }
                )
                return EXIT_OK
            self.say(f"id: {issued.id}")
            self.say(f"path: {issued.path}")
            self.say(f"expires: {audit.utc(issued.expires_at)}")
            if issued.capped:
                self.say("duration capped at 7 days (sensitive environment)")
            for env in issued.uncovered_sensitive:
                self.say(f"not covered (sensitive environment): {env}")
            self.say("value (shown once):")
            self.say(issued.value)
            return EXIT_OK
        if cmd == "list":
            listed = tokens.list_tokens(self.conn, ns.path, ns.client, clock=self.clock)
            if ns.json:
                self.dump([t.as_dict() for t in listed])
                return EXIT_OK
            for t in listed:
                d = t.as_dict()
                self.say(
                    f"{d['id']}  {d['type']}  {d['holder']}  {d['path']}  "
                    f"issued {d['issued_at']}  expires {d['expires_at']}  {d['state']}"
                )
            return EXIT_OK
        revoked = tokens.revoke(self.conn, ns.id, clock=self.clock)
        state = "already revoked" if revoked.already else "revoked"
        self.say(f"{state}: {revoked.id} ({revoked.kind})")
        return EXIT_OK

    # -- clients OAuth -----------------------------------------------------------------------

    def clients(self, ns: argparse.Namespace) -> int:
        if ns.cmd == "add":
            added = clients.add(
                self.conn, ns.client_id, ns.path, ns.redirect_uris, ns.name, clock=self.clock
            )
            self.say(f"client preregistered: {added.client_id} on {added.path}")
            for uri in added.redirect_uris:
                self.say(f"  redirect_uri: {uri}")
            return EXIT_OK
        if ns.cmd == "list":
            listed = clients.list_clients(self.conn)
            if ns.json:
                self.dump([c.as_dict() for c in listed])
                return EXIT_OK
            for c in listed:
                self.say(
                    f"{c.client_id}  {c.kind}  {c.path}  {c.state}  {' '.join(c.redirect_uris)}"
                )
            return EXIT_OK
        if ns.cmd == "remove":
            removed = clients.remove(self.conn, ns.client_id, clock=self.clock)
            self.say(f"client removed: {removed.client_id}")
            self.say(f"OAuth lineages revoked: {removed.lineages_revoked}")
            return EXIT_OK
        clients.set_dcr(self.conn, ns.state == "on", clock=self.clock)
        self.say(f"dynamic registration: {'on' if ns.state == 'on' else 'off'}")
        return EXIT_OK

    # -- connection library (F-006) ---------------------------------------------------------

    def signal_shares(self, shares: Sequence[connections.ShareSignal]) -> None:
        """BR-079, BR-047: connections the change makes shared, told before acting."""
        for signal in shares:
            self.say(signal.text())

    def services(self, ns: argparse.Namespace) -> int:
        if ns.cmd == "declare":
            declared = connections.service_declare(
                self.conn,
                self.outbound,
                ns.id,
                ns.server,
                ns.model,
                ns.scopes,
                ns.internal,
                clock=self.clock,
            )
            s = declared.service
            self.say(f"service declared: {s.id} ({s.model}) {s.base_url}")
            self.say(f"  requested scopes: {s.scopes or '-'}")
            if s.internal_host:
                self.say(
                    "  internal host: addresses admitted at declaration "
                    f"{', '.join(declared.addresses)} (rechecked on every call)"
                )
            self._endpoints(s)
            return EXIT_OK
        if ns.cmd == "rediscover":
            before, after = connections.service_rediscover(
                self.conn, self.outbound, ns.id, ns.confirm, clock=self.clock
            )
            self.say(f"service rediscovered: {after.id}")
            for name in sorted(set(before.endpoints) | set(after.endpoints)):
                old, new = before.endpoints.get(name, "-"), after.endpoints.get(name, "-")
                self.say(f"  {name}: {new}" + ("" if old == new else f" (before: {old})"))
            if before.resource != after.resource:
                self.say(f"  resource: {after.resource or '-'} (before: {before.resource or '-'})")
            return EXIT_OK
        if ns.cmd == "upstream":
            return self._upstream_cmd(ns)
        if ns.cmd == "list":
            listed = connections.service_list(self.conn)
            if ns.json:
                self.dump([s.as_dict() for s in listed])
                return EXIT_OK
            for s in listed:
                internal = "  internal host" if s.internal_host else ""
                self.say(f"{s.id}  {s.model}  {s.base_url}  scopes {s.scopes or '-'}{internal}")
                self._endpoints(s)
                # MCP upstream (F-008 §3.1), same line as `connection status` (relay runbook, R5)
                label = s.upstream_label()
                if label != connections.UPSTREAM_UNDECLARED:
                    label = f"upstream: {label}" + ("  internal host" if s.mcp_internal else "")
                self.say(f"  {label}")
            return EXIT_OK
        connections.service_remove(self.conn, ns.id, clock=self.clock)
        self.say(f"service removed: {ns.id}")
        return EXIT_OK

    def _upstream_cmd(self, ns: argparse.Namespace) -> int:
        """`service upstream` (BR-106): declare, change (`--confirm`) or remove the MCP upstream."""
        if ns.remove:
            if ns.kind is not None or ns.mcp_url is not None or ns.mcp_internal:
                raise Refused("--remove: no --type, no --mcp-url, no --mcp-internal-host")
            before = connections.service_upstream_remove(
                self.conn, ns.id, ns.confirm, clock=self.clock
            )
            self.say(f"upstream removed: {ns.id} (was {before.upstream_label()})")
            self.say("  its connections bring no tool any more")
            return EXIT_OK
        if ns.kind is None or ns.mcp_url is None:
            raise Refused("service upstream: --type and --mcp-url required (or --remove)")
        done = connections.service_upstream(
            self.conn,
            self.upstream.transport,
            ns.id,
            ns.kind,
            ns.mcp_url,
            ns.mcp_internal,
            ns.confirm,
            clock=self.clock,
        )
        s = done.service
        verb = "upstream declared" if done.changed else "upstream unchanged"
        self.say(f"{verb}: {s.id} {s.upstream_type} {s.mcp_url}")
        if s.mcp_internal:
            self.say(
                "  MCP internal host: addresses admitted at declaration "
                f"{', '.join(done.addresses)} (rechecked on every call)"
            )
        if done.changed and done.before.upstream_type is not None:
            self.say(f"  before: {done.before.upstream_type} {done.before.mcp_url}")
        return EXIT_OK

    def _endpoints(self, s: connections.Service) -> None:
        if not s.endpoints:
            self.say("  endpoints: not discovered (service rediscover)")
        for name, url in sorted(s.endpoints.items()):
            self.say(f"  {name}: {url}")
        if s.resource:
            self.say(f"  resource: {s.resource}")

    def applications(self, ns: argparse.Namespace) -> int:
        if ns.cmd == "declare" and ns.mode == "dynamic":
            if ns.client_id is not None or ns.secret_file is not None:
                raise Refused("--mode dynamic: neither --client-id nor --secret-file (BR-083)")
            reg = lifecycle.register(
                self.run,
                self.key,
                self.outbound,
                ns.id,
                ns.service,
                clock=self.clock,
                web_redirect=self.web_redirect(),
            )
            self.say(f"application registered: {reg.app_id} (service {ns.service}, dynamic)")
            self.say(f"  client_id: {reg.client_id}")
            self.say(f"  redirect_uri registered: {connections.REDIRECT_URI}")
            self.say(f"  secret: {'encrypted' if reg.has_secret else 'none (public client)'}")
            if reg.secret_expires_at:
                self.say(f"  secret expires: {audit.utc(reg.secret_expires_at)}")
            return EXIT_OK
        if ns.cmd == "reregister":
            reg = lifecycle.reregister(
                self.run,
                self.key,
                self.outbound,
                ns.id,
                ns.confirm,
                clock=self.clock,
                web_redirect=self.web_redirect(),
            )
            self.say(f"application reregistered: {reg.app_id}, client_id {reg.client_id}")
            for name, outcome in reg.upstream:
                self.say(
                    f"  {name}: reauthorization required (revocation with the former client:"
                    f" {outcome})"
                )
            return EXIT_OK
        if ns.cmd == "declare":
            if ns.client_id is None:
                raise Refused("--mode declared: --client-id required")
            secret = None if ns.secret_file is None else read_secret(ns.secret_file, self.inp)
            app = connections.application_declare(
                self.conn,
                self.key,
                ns.id,
                ns.service,
                ns.client_id,
                secret,
                clock=self.clock,
            )
            self.say(f"application declared: {app.id} (service {app.service_id}, {app.mode})")
            self.say(f"  client_id: {app.client_id}")
            self.say(f"  redirect_uri to enter at the provider: {app.redirect_uri}")
            self.say(f"  secret: {'encrypted' if app.has_secret else 'none (public client)'}")
            return EXIT_OK
        if ns.cmd == "list":
            listed = connections.application_list(self.conn)
            if ns.json:
                self.dump([a.as_dict() for a in listed])
                return EXIT_OK
            for a in listed:
                d = a.as_dict()
                expiry = (
                    f"  secret expires {d['secret_expires_at']}" if d["secret_expires_at"] else ""
                )
                self.say(
                    f"{a.id}  service {a.service_id}  {a.mode}  client_id {a.client_id}  "
                    f"secret {d['secret'] or 'none'}{expiry}"
                )
            return EXIT_OK
        connections.application_remove(self.conn, ns.id, clock=self.clock)
        self.say(f"application removed: {ns.id}")
        return EXIT_OK

    def connection(self, ns: argparse.Namespace) -> int:
        conn, clock, key = self.conn, self.clock, self.key
        if ns.cmd == "add":
            if ns.provided:
                if ns.application is not None:
                    raise Refused("--provided and --application are mutually exclusive")
                if ns.file is None:
                    raise Refused("--provided: the value is read through --file <f|-> (BR-086)")
                value = read_secret(ns.file, self.inp)
                added = connections.add_static(
                    conn,
                    key,
                    ns.name,
                    ns.service,
                    value,
                    ns.prefix,
                    ns.account,
                    clock=clock,
                    expires_at=ns.expires_at,
                )
            else:
                if ns.application is None:
                    raise Refused("--application <id> or --provided required")
                if ns.expires_at is not None:
                    raise Refused("--expires-at: reserved for a provided token")
                if ns.file is not None or ns.account is not None:
                    raise Refused("--file and --account: reserved for --provided")
                added = connections.add_oauth(
                    conn, key, ns.name, ns.service, ns.application, ns.prefix, clock=clock
                )
            self.say(
                f"connection created: {added.name} ({connections.KIND_LABELS[added.kind]}), "
                f"state {connections.STATE_LABELS[added.state]}"
            )
            if added.url is not None:
                self._authorization_url(added.name, added.url)
            return EXIT_OK
        if ns.cmd == "authorize":
            if ns.callback != "-" and any(m in ns.callback for m in ("://", "?", "=", "&")):
                raise Refused(
                    "--callback: a file path or -, never the callback URL itself "
                    "(value not echoed, BR-086)"
                )
            callback = read_secret(ns.callback, self.inp)
            done = lifecycle.authorize(
                self.run, key, self.outbound, ns.name, callback, ns.confirm_account, clock=clock
            )
            expiry = audit.utc(done.expires_at) + (" (estimated)" if done.estimated else "")
            self.say(
                f"authorized: {done.name}, account {done.account or 'unknown'}, "
                f"scopes {done.scopes or '-'}, expires {expiry}"
            )
            if done.upstream != "no_token":
                self.say(f"  former tokens: revocation {done.upstream}")
            return EXIT_OK
        if ns.cmd == "reauthorize":
            if ns.file is None:
                if ns.expires_at is not None:
                    raise Refused("--expires-at: reserved for a provided token")
                if ns.account is not None:
                    raise Refused("--account: reserved for reauthorize --file (provided token)")
                opened = connections.reopen(conn, key, ns.name, clock=clock)
                self.say(
                    f"authorization opened: {opened.name}, state "
                    f"{connections.STATE_LABELS[opened.state]}"
                )
                self._authorization_url(opened.name, opened.url)
                return EXIT_OK
            value = read_secret(ns.file, self.inp)
            row = connections.replace_static(
                conn, key, ns.name, value, ns.account, clock=clock, expires_at=ns.expires_at
            )
            self.say(
                f"value replaced: {ns.name.strip('/')}, state active (generation {row.generation})"
            )
            return EXIT_OK
        if ns.cmd == "prefix":
            prefix = connections.set_prefix(conn, ns.name, ns.prefix, clock=clock)
            self.say(f"prefix: {ns.name.strip('/')} -> {prefix}")
            return EXIT_OK
        if ns.cmd == "place":
            placed = connections.place(conn, ns.name, ns.role, ns.confirm_share, clock=clock)
            label = "sensitive" if placed.sensitive else "non-sensitive"
            self.say(f"placed: {placed.connection} on {placed.role} ({label})")
            if placed.confirmed:
                self.say(f"sharing confirmed and audited: {', '.join(placed.confirmed)}")
            return EXIT_OK
        if ns.cmd == "unplace":
            role = connections.unplace(conn, ns.name, ns.role, clock=clock)
            self.say(f"placement removed: {ns.name.strip('/')} from {role}")
            return EXIT_OK
        if ns.cmd in ("status", "list"):
            listed = connections.views(
                conn, key, ns.name if ns.cmd == "status" else None, clock=clock
            )
            if ns.json:
                self.dump([v.as_dict() for v in listed])
                return EXIT_OK
            for v in listed:
                if ns.cmd == "list":
                    self.say(
                        f"{v.name}  service {v.service}  prefix {v.prefix}  "
                        f"{connections.KIND_LABELS[v.kind]}  state {_state_label(v.state)}"
                    )
                    continue
                self.say(self._state_line(v))
                for path, sensitive in v.placements:
                    self.say(
                        f"  placed on {path} ({'sensitive' if sensitive else 'non-sensitive'})"
                    )
            if ns.cmd == "status" and ns.name is None:
                # §12 (key forgotten at deployment): readable even without any connection.
                reason = key.status(conn)
                self.say(f"connections key: {reason or 'usable'}")
            return EXIT_OK
        if ns.cmd == "renew":
            name, got = lifecycle.renew_now(self.run, key, self.outbound, ns.name, clock=clock)
            expiry = "-" if got.expires_at is None else audit.utc(got.expires_at)
            self.say(f"renewed: {name}, generation {got.generation}, expires {expiry}")
            return EXIT_OK
        if ns.cmd == "tools":
            return self._tools(ns)
        if ns.cmd == "revoke":
            revoked = lifecycle.revoke(self.run, key, self.outbound, ns.name, clock=clock)
            self.say(f"connection revoked: {revoked.connection}, reauthorization required")
            self.say(f"  revocation at the provider: {revoked.upstream}")
            return EXIT_OK
        removed = lifecycle.remove(self.run, key, self.outbound, ns.name, ns.confirm, clock=clock)
        self.say(f"connection removed: {removed.connection}")
        self.say(f"  placements removed: {removed.placements}")
        self.say(f"  revocation at the provider: {removed.upstream}")
        return EXIT_OK

    def _tools(self, ns: argparse.Namespace) -> int:
        """`connection tools <c> --role <path> [--purge-names --confirm <c>]` (§3.2)."""
        try:
            listed = relay.tools(
                self.run,
                self.key,
                self.outbound,
                self.upstream,
                ns.name,
                ns.role,
                ns.purge,
                ns.confirm,
                clock=self.clock,
            )
        except UpstreamError as exc:
            raise lifecycle.Unavailable(f"MCP upstream: {exc.code} ({exc.reason})") from None
        if ns.json:
            self.dump(
                {
                    "connection": listed.connection,
                    "role": listed.role,
                    "version": listed.version,
                    "tools": [{"name": p, "upstream_name": u} for p, u in listed.tools],
                    "invalid_names": listed.invalid,
                    "purged_names": listed.purged,
                }
            )
            return EXIT_OK
        self.say(
            f"tools of {listed.connection} on {listed.role} (version {listed.version}): "
            f"{len(listed.tools)}"
        )
        for public, upstream in listed.tools:
            self.say(f"  {public}  ({upstream})")
        if listed.invalid:
            self.say(f"  upstream names ignored (invalid name): {listed.invalid}")
        if listed.purged is not None:
            self.say(f"  names purged from the table: {listed.purged}")
        return EXIT_OK

    # -- action policy (F-015) ---------------------------------------------------------------

    def policies(self, ns: argparse.Namespace) -> int:
        conn, clock = self.conn, self.clock
        if ns.cmd == "show":
            shown = policy.show(conn, ns.name, ns.role)
            if ns.json:
                self.dump(shown.as_dict())
                return EXIT_OK
            mark = " (sensitive)" if shown.sensitive else ""
            self.say(f"policy of {shown.connection} on {shown.role}{mark}")
            self.say(f"  mode: {shown.mode}  (origin: {shown.mode_origin})")
            self.say(f"  pin: {shown.pin}")
            for which in policy.LISTS:
                self.say(f"  {which}: {' '.join(shown.lists[which]) or '-'}")
            self.say(f"  pinned tools: {' '.join(shown.pinned_tools) or '-'}")
            for tool_name, rule in shown.arguments.items():
                values = " ".join(rule["values"])
                self.say(f"  argument rule: {tool_name} {rule['argument']} {values}")
            return EXIT_OK
        if ns.cmd == "mode":
            mode = policy.set_mode(conn, ns.name, ns.role, ns.mode, clock=clock)
            origin = " (inherited)" if ns.mode == "inherit" else ""
            self.say(f"mode of {ns.name.strip('/')} on {ns.role.strip('/')}: {mode}{origin}")
            return EXIT_OK
        if ns.cmd == "add":
            added = policy.add_to_list(
                conn, ns.name, ns.role, ns.list, ns.tools, ns.by_class, clock=clock
            )
            self.say(f"added to {ns.list}: {len(added)}")
            for name in added:
                self.say(f"  {relay.safe_label(name, 256)}")
            return EXIT_OK
        if ns.cmd == "remove":
            removed = policy.list_remove(conn, ns.name, ns.role, ns.list, ns.tools, clock=clock)
            self.say(f"removed from {ns.list}: {len(removed)}")
            return EXIT_OK
        if ns.cmd == "arg":
            recorded = relay.argument_at_entry(
                self.run, self.key, self.outbound, self.upstream, ns.name, ns.role, ns.tool,
                ns.argument, ns.values, clock=clock,
            )  # fmt: skip
            label = relay.safe_label(ns.tool, 256)
            self.say(f"argument rule: {label} {recorded.argument} {' '.join(recorded.values)}")
            return EXIT_OK
        if ns.cmd == "arg-remove":
            policy.argument_remove(conn, ns.name, ns.role, ns.tool, clock=clock)
            self.say(f"argument rule removed: {relay.safe_label(ns.tool, 256)}")
            return EXIT_OK
        if ns.cmd == "pin":
            policy.set_pin(conn, ns.name, ns.role, ns.pin, clock=clock)
            self.say(f"pin of {ns.name.strip('/')} on {ns.role.strip('/')}: {ns.pin}")
            return EXIT_OK
        if ns.cmd == "pin-tool":
            fresh = policy.pin_tool(conn, ns.name, ns.role, ns.tool, clock=clock)
            state = "pinned" if fresh else "already pinned"
            self.say(f"tool {state}: {relay.safe_label(ns.tool, 256)}")
            return EXIT_OK
        if ns.cmd == "unpin-tool":
            policy.unpin_tool(conn, ns.name, ns.role, ns.tool, clock=clock)
            self.say(f"tool pin removed: {relay.safe_label(ns.tool, 256)}")
            return EXIT_OK
        return self._preview(ns)

    def _preview(self, ns: argparse.Namespace) -> int:
        """`policy preview <role>` (SCR-03, BR-183); exit 1 when the evaluator raised (BR-185)."""
        seen = preview.preview(
            self.run, self.key, self.outbound, self.upstream, ns.role, clock=self.clock
        )
        if ns.json:
            self.dump(
                {
                    "role": seen.role,
                    "tools": [t.as_dict() for t in seen.tools],
                    "unavailable": [
                        {"connection": u.connection, "code": u.code} for u in seen.unavailable
                    ],
                    "policy_errors": list(seen.errors),
                }
            )
        else:
            self._preview_text(seen)
        for name in seen.errors:
            print(f"policy error: {name} (its tools are hidden)", file=self.err)
        return EXIT_ERROR if seen.errors else EXIT_OK

    def _preview_text(self, seen: preview.Preview) -> None:
        if not seen.tools and not seen.unavailable and not seen.errors:
            self.say(f"rights of {seen.role}: no connection")
            return
        exposed = sum(1 for t in seen.tools if t.exposed)
        self.say(f"rights of {seen.role}: {exposed} exposed, {len(seen.tools) - exposed} hidden")
        for t in seen.tools:
            fields = [
                "exposed" if t.exposed else "hidden",
                t.public_name,
                f"({t.upstream_name})",
                f"class {t.cls} ({t.source})",
            ]
            if t.reasons:
                fields.append(f"reasons {','.join(t.reasons)}")
            if t.opened:
                fields.append("opened")
            if t.pinned:
                fields.append("pinned")
            if t.grouped:
                fields.append(f"grouped {json.dumps(t.grouped, sort_keys=True)}")
            if t.argument_rule is not None:
                values = json.dumps(t.argument_rule["values"])
                fields.append(f"argument {t.argument_rule['argument']} {values}")
            if t.sensitive_via:
                fields.append(f"sensitive via {', '.join(t.sensitive_via)}")
            self.say("  " + "  ".join(fields))
        for u in seen.unavailable:
            self.say(f"  unavailable  {u.connection}  {u.code}")

    def tools_catalogue(self, ns: argparse.Namespace) -> int:
        """`tool queue|classify|accept` (SCR-02, BR-172, BR-173)."""
        conn, clock = self.conn, self.clock
        if ns.cmd == "queue":
            waiting = policy.queue(conn, ns.service)
            if ns.json:
                self.dump([q.as_dict() for q in waiting])
                return EXIT_OK
            self.say(f"tools to classify: {len(waiting)}")
            for q in waiting:
                fields = [q.service, relay.safe_label(q.name, 256), f"class {q.cls} ({q.source})"]
                if q.proposal is not None:
                    fields.append(f"proposal {q.proposal}")
                if q.removed:
                    fields.append("removed")
                self.say("  " + "  ".join(fields))
            return EXIT_OK
        if ns.cmd == "diff":
            return self._diff(ns)
        if ns.cmd == "validate":
            return self._validate(ns)
        if ns.cmd == "classify":
            done = policy.classify(conn, ns.service, ns.tool, ns.cls, clock=clock)
        else:
            done = policy.accept(conn, ns.service, ns.tool, clock=clock)
        label = relay.safe_label(done.name, 256)
        self.say(f"{done.service} {label}: class {done.cls} ({done.source})")
        return EXIT_OK

    def _diff(self, ns: argparse.Namespace) -> int:
        """`tool diff <node> [--json]` (BR-182): upstream text escaped, in both forms."""
        path, changes = pinning.diff(
            self.run, self.key, self.outbound, self.upstream, ns.path, clock=self.clock
        )
        if ns.json:
            # every non-ASCII and control character escaped, DEL included: inert in a terminal
            text = json.dumps({"node": path, "changes": [c.as_dict() for c in changes]},
                              ensure_ascii=True, indent=2)  # fmt: skip
            print(text.replace("\x7f", "\\u007f"), file=self.out)
            return EXIT_OK
        self.say(f"pinned changes under {path}: {len(changes)}")
        for c in changes:
            self.say(f"  {c.state}  {pinning.escaped(c.tool)}  {c.connection} on {c.role}")
            for f in c.fields:
                self.say(f"    {f.name}")
                self.say(f"      - {pinning.shown(f.old)}")
                self.say(f"      + {pinning.shown(f.new)}")
        return EXIT_OK

    def _validate(self, ns: argparse.Namespace) -> int:
        """`tool validate <node> <connection> [--tool <tool>]` (BR-182): prints the hashes."""
        done = pinning.validate(
            self.run, self.key, self.outbound, self.upstream, ns.path, ns.name, ns.tool,
            clock=self.clock,
        )  # fmt: skip
        self.say(f"validated: {len(done)}")
        for v in done:
            after = v.after if v.after is not None else "dropped (removed upstream)"
            self.say(f"  {relay.safe_label(v.tool, 256)}  {v.connection} on {v.role}  {after}")
        return EXIT_OK

    def _authorization_url(self, name: str, url: str) -> None:
        """SCR-02.pending: the authorization URL (never a secret nor the PKCE verifier)."""
        self.say("authorization URL, to open in the browser (valid 10 minutes):")
        self.say(url)
        self.say(
            "then copy the URL of the address bar (connection error expected) into a "
            f"file and: hullgate connection authorize {name} --callback - < file"
        )

    @staticmethod
    def _state_line(v: connections.View) -> str:
        d = v.as_dict()
        parts = [v.name, f"service {v.service}", f"account {v.account}"]
        if v.scopes:
            parts.append(f"scopes {' '.join(v.scopes)}")
        parts.append(f"state {_state_label(v.state)}")
        if d["expires_at"]:
            parts.append(f"expires {d['expires_at']}{' (estimated)' if v.expiry_estimated else ''}")
        if d["not_before"]:
            parts.append(f"not before {d['not_before']}")
        if v.last_outcome:
            parts.append(f"last outcome {v.last_outcome}")
        if v.redeclare:
            parts.append(f"application {v.redeclare} to redeclare or reregister")
        if v.refusal:
            parts.append(v.refusal)
        parts.append(
            v.upstream
            if v.upstream == connections.UPSTREAM_UNDECLARED
            else f"upstream: {v.upstream}"
        )
        return "   ".join(parts)

    # -- audit, export -----------------------------------------------------------------------

    def audit(self, ns: argparse.Namespace) -> int:
        entries = audit.tail(self.conn, ns.n)
        if ns.json:
            self.dump([e.as_dict() for e in entries])
            return EXIT_OK
        for e in entries:
            fields = [e.at, e.actor, e.action, e.result, e.path or "-"]
            if e.reason:
                fields.append(f"reason={e.reason}")
            if e.before:
                fields.append(f"before={e.before}")
            if e.after:
                fields.append(f"after={e.after}")
            if e.detail:
                fields.append(e.detail)
            self.say("  ".join(fields))
        return EXIT_OK

    def export(self, _: argparse.Namespace) -> int:
        self.out.write(export_yaml(self.conn))
        return EXIT_OK

    def import_document(self, ns: argparse.Namespace) -> int:
        try:
            with open(ns.file, "rb") as source:
                data = source.read(importing.MAX_BYTES + 1)
            if len(data) > importing.MAX_BYTES:
                raise Refused("document: maximum size is 1 MiB")
            text = data.decode("utf-8")
        except (OSError, UnicodeError):
            raise Refused("document: cannot read UTF-8 YAML file") from None
        if ns.preview:
            result = importing.preview(self.conn, text, clock=self.clock)
        else:
            result = importing.apply(
                self.change_operations(),
                text,
                ns.apply,
                apply_deletions=ns.apply_deletions,
                confirm_share=ns.confirm_share,
            )
        self.dump(result)
        return EXIT_OK

    def change_operations(self) -> changes.Operations:
        return changes.Operations(self.conn, self.key, self.outbound, self.upstream, self.clock)

    def undo(self, ns: argparse.Namespace) -> int:
        result = (
            changes.plan(self.conn, ns.seq, clock=self.clock)
            if ns.preview
            else changes.undo(
                self.change_operations(), ns.seq, impact=ns.impact, confirm_share=ns.confirm_share
            )
        )
        self.dump(result)
        return EXIT_OK

    # -- administrators of the console (F-016 lot A) ---------------------------------------------

    def consent(self, ns: argparse.Namespace) -> int:
        """BR-310: the operator compares the reference shown on their own barrier page with this
        list (role, client, redirect host) before approving it."""
        now = int(self.clock())
        if ns.cmd == "list":
            listed = oauth.pending_requests(self.conn, now)
            if ns.json:
                self.dump([p.as_dict() for p in listed])
                return EXIT_OK
            if not listed:
                self.say("no pending request")
            for p in listed:
                state = "  approved" if p.approved else ""
                self.say(
                    f"{p.reference}  {p.role}  {pinning.escaped(p.client_id)}  "
                    f"{pinning.escaped(p.redirect_host)}  expires {audit.utc(p.expires_at)}{state}"
                )
            return EXIT_OK
        approved = oauth.approve_request(self.conn, ns.reference, now)
        self.say(f"approved: {approved.reference} ({approved.role}, "
                 f"{pinning.escaped(approved.client_id)})")  # fmt: skip
        self.say("press Authorize on the barrier page, with no code, before the request expires")
        return EXIT_OK

    def administrators(self, ns: argparse.Namespace) -> int:
        now = int(self.clock())
        if ns.cmd in ("bootstrap", "recover"):
            admin, token = (
                accounts.recover(self.conn, ns.email, now)
                if ns.cmd == "recover"
                else accounts.bootstrap(self.conn, ns.email, ns.name, now)
            )
            self.say(f"administrator {admin.state}: {admin.id} {admin.email}")
            self.say("the link below is shown once and expires in 30 minutes")
            self.say(accounts.LINK_PREFIX + accounts.link_url(self.admin_origin, token))
            return EXIT_OK
        if ns.cmd == "list":
            listed = accounts.list_administrators(self.conn)
            counts = {a.id: len(accounts.passkeys(self.conn, a.id)) for a in listed}
            if ns.json:
                self.dump([{**a.as_dict(), "passkeys": counts[a.id]} for a in listed])
                return EXIT_OK
            for a in listed:
                self.say(f"{a.id}  {a.email}  {a.state}  passkeys: {counts[a.id]}  {a.name}")
            return EXIT_OK
        if ns.cmd == "attempts":
            cleared = accounts.clear_attempts(self.conn, now)
            self.say(f"sign-in attempts cleared: {cleared}")
            return EXIT_OK
        before = accounts.clear_allowlist(self.conn, now)
        self.say(f"allow-list cleared: {len(before)} entries removed")
        return EXIT_OK


def open_store(db_path: str) -> sqlite3.Connection:
    """Existing database at the code's schema, else `StoreUnavailable` (BR-069: no migration)."""
    conn = connect(db_path, create=False)
    try:
        version = migrations.user_version(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise StoreUnavailable(str(exc)) from exc
    if version != migrations.SCHEMA_VERSION:
        conn.close()
        raise StoreUnavailable(
            f"database at version {version}, code at version {migrations.SCHEMA_VERSION}"
        )
    return conn


def run(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    clock: Clock = time.time,
    inp: TextIO | None = None,
    outbound: Outbound | None = None,
    upstream: UpstreamClient | None = None,
) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = list(argv)
    if args[:1] == ["admin"] and args[1:2] and args[1] not in ADMIN_VERBS:
        args = args[1:]
    try:
        ns = _parser().parse_args(args)
    except Refused as exc:
        print(f"refused: {exc}", file=err)
        return EXIT_REFUSED
    try:
        cfg = config.load(env)
    except config.ConfigError as exc:
        print(f"invalid configuration: {exc}", file=err)
        return EXIT_ERROR
    try:
        conn = open_store(cfg.db_path)
    except (StoreUnavailable, sqlite3.Error) as exc:
        print(f"database unavailable: {exc}", file=err)
        return EXIT_ERROR
    cli = Cli(
        conn,
        cfg.public_url,
        out,
        clock,
        ConnectionsKey(cfg.connections_key_file),
        inp,
        outbound,
        upstream,
        err,
    )
    cli.admin_origin = cfg.admin.origin
    handlers = {
        "tree": cli.tree,
        "settings": cli.settings,
        "token": cli.tokens,
        "audit": cli.audit,
        "client": cli.clients,
        "export": cli.export,
        "import": cli.import_document,
        "undo": cli.undo,
        "service": cli.services,
        "application": cli.applications,
        "connection": cli.connection,
        "policy": cli.policies,
        "tool": cli.tools_catalogue,
        "admin": cli.administrators,
        "consent": cli.consent,
    }
    try:
        return handlers[ns.group](ns)
    except connections.ShareRefused as exc:
        print(f"refused: {exc}: add --confirm-share", file=err)
        return EXIT_REFUSED
    except Refused as exc:
        print(f"refused: {exc}", file=err)
        return EXIT_REFUSED
    except KeyUnavailable as exc:
        # BR-088: a command of the "refused" list without a usable key; nothing is written.
        print(f"connections key unavailable: {exc.reason} (nothing was written)", file=err)
        return EXIT_ERROR
    except connections.KeyUnavailableError as exc:
        print(f"connections key unavailable: {exc.reason} (nothing was written)", file=err)
        return EXIT_ERROR
    except lifecycle.Unavailable as exc:
        # BR-092, BR-093: transient or uncertain outcome, or anomaly; no token written.
        print(f"provider unavailable: {exc.reason}", file=err)
        return EXIT_ERROR
    except connections.AccessError as exc:
        print(f"connection unavailable: {exc.code}", file=err)
        return EXIT_ERROR
    except (StoreUnavailable, sqlite3.Error) as exc:
        print(f"database unavailable: {exc}", file=err)
        return EXIT_ERROR
    except policy.StateError as exc:
        # BR-181, BR-185: the policy state could not be read or written; nothing is shown
        named = f"{pinning.escaped(exc.connection)}: " if exc.connection else ""
        print(f"policy error: {named}policy state unavailable (nothing shown)", file=err)
        return EXIT_ERROR
    finally:
        conn.close()
