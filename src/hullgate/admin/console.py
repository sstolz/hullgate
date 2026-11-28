"""The console's routes of `/admin/api/v1` (F-016 slice 2): tree, settings, tokens, OAuth clients,
the library (services, applications, connections, placements) and the audit.

Every write calls the domain operation the CLI calls for the same verb, as the administrator
(`audit.acting_as`, actor `admin:<id>`), and returns its refusal as it is (BR-211). An act that
deletes, revokes or changes a path or a sensitivity quotes the digest of the impact it was shown;
the digest is recomputed when the write arrives, in the write's own transaction where the act is
a single transaction (BR-214). Session, CSRF and the security headers are applied to these routes
by `hullgate.admin.app`, from their declaration (BR-068 v2, BR-205, BR-213).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, TypeVar
from urllib.parse import urlencode

from anyio import to_thread
from starlette.responses import JSONResponse, Response

from hullgate import (
    audit,
    changes,
    clients,
    config,
    connections,
    lifecycle,
    oauth,
    policy,
    relay,
    routes,
    tokens,
    tree,
)
from hullgate.admin import accounts
from hullgate.admin.base import ApiError, ApiRoute, Ctx
from hullgate.cipher import ConnectionsKey, KeyUnavailable
from hullgate.config import AdminOrigin
from hullgate.db import Database, admitted_operation, transaction
from hullgate.outbound import Outbound
from hullgate.tree import Clock, Refused, Tree
from hullgate.upstream_mcp import Transport, UpstreamClient, UpstreamError

T = TypeVar("T")

EXPORT_MAX = 100_000  # BR-218
PAGE_DEFAULT = 100
PAGE_MAX = 500
SEQ_MAX = 2**63 - 1  # SQLite's integer range (BR-218's `before`)
IMPACT_ACTS = (
    "tree.delete",
    "tree.rename",
    "tree.move",
    "tree.sensitive",
    "connection.revoke",
    "connection.remove",
    "client.remove",
    "application.remove",
    "service.remove",
    "application.reregister",
)  # BR-214's population: every API write that deletes, revokes or changes a path or sensitivity
IMPACT_CHANGED = "The impact changed since it was shown. Read it again before acting."
CLIENTS_NOTICE = {"tools_notice": True}  # BR-215: clients see the change at their next tools/list


def _digest(act: str, target: str, impact: Mapping[str, Any], shares: list[Any]) -> str:
    document = {"act": act, "target": target, "impact": dict(impact), "shares": shares}
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def impact_answer(
    act: str, target: str, impact: Mapping[str, Any], shares: list[Any]
) -> dict[str, Any]:
    return {
        "act": act,
        "target": target,
        "impact": dict(impact),
        "shares": shares,
        "digest": _digest(act, target, impact, shares),
    }


class ConsoleApi:
    """Handlers of the console's resource groups; every write is a domain operation of the CLI."""

    def __init__(
        self,
        db: Database,
        origin: AdminOrigin,
        public_url: str,
        clock: Clock,
        *,
        key: ConnectionsKey | None = None,
        outbound: Outbound | None = None,
        upstream: UpstreamClient | None = None,
    ) -> None:
        self.db = db
        self.origin = origin
        self.public_url = public_url.rstrip("/")
        self.clock = clock
        self.key = key if key is not None else ConnectionsKey(config.DEFAULT_CONNECTIONS_KEY_FILE)
        self._outbound = outbound
        self._upstream = upstream
        self.web_redirect = connections.web_redirect_uri(origin.origin)

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

    # -- running domain operations as the administrator -------------------------------------------

    async def act(
        self, ctx: Ctx, fn: Callable[[], T], *, admit: bool = True, recheck: bool = False
    ) -> T:
        """Run a domain operation as the administrator, mapping refusals (BR-211).

        Provider operations are admitted before starting; their outcome transactions must
        complete even if the session ends at the provider. Database-only operations instead
        check authorization inside their own mutation transaction (`db_act`). Routes that
        decide mutations after preparation retain these checks with `recheck=True`.
        """
        who = accounts.actor(ctx.administrator_id)

        def call() -> T:
            admission = (
                admitted_operation(self.db, recheck=recheck) if admit else contextlib.nullcontext()
            )
            with audit.acting_as(who), admission:
                return fn()

        try:
            return await to_thread.run_sync(call)
        except ApiError:
            raise
        except connections.ShareRefused as exc:
            raise ApiError(
                409, "share_confirmation_required", str(exc), reasons=list(exc.reasons)
            ) from None
        except changes.Refusal as exc:
            raise ApiError(exc.status, exc.code, str(exc), **exc.extra) from None
        except Refused as exc:
            raise ApiError(409, "refused", str(exc)) from None
        except KeyUnavailable as exc:
            raise ApiError(503, "key_unavailable", _key_text(exc.reason)) from None
        except connections.KeyUnavailableError as exc:
            raise ApiError(503, "key_unavailable", _key_text(exc.reason)) from None
        except lifecycle.Unavailable as exc:
            raise ApiError(
                503, "provider_unavailable", f"provider unavailable: {exc.reason}"
            ) from None
        except connections.AccessError as exc:
            raise ApiError(
                503, "connection_unavailable", f"connection unavailable: {exc.code}"
            ) from None
        except policy.StateError:
            raise ApiError(
                503, "policy_unavailable", "policy state unavailable (nothing done)"
            ) from None

    async def db_act(
        self,
        ctx: Ctx,
        fn: Callable[[sqlite3.Connection], T],
        *,
        admit: bool = False,
        recheck: bool = False,
    ) -> T:
        return await self.act(ctx, lambda: self.db.call(fn), admit=admit, recheck=recheck)

    async def read(self, ctx: Ctx, fn: Callable[[sqlite3.Connection], T]) -> T:
        """A read: no actor needed, the same refusal mapping (an unknown path is a refusal)."""
        return await self.db_act(ctx, fn)

    def routes(self) -> list[ApiRoute]:
        return [
            ApiRoute("GET", "impacts", self.impacts_get),
            # tree and settings
            ApiRoute("GET", "nodes", self.nodes_get),
            ApiRoute("POST", "nodes", self.nodes_add),
            ApiRoute("POST", "nodes/rename", self.nodes_rename),
            ApiRoute("POST", "nodes/move", self.nodes_move),
            ApiRoute("POST", "nodes/delete", self.nodes_delete),
            ApiRoute("POST", "nodes/sensitive", self.nodes_sensitive),
            ApiRoute("POST", "nodes/lock", self.nodes_lock),
            ApiRoute("POST", "nodes/unlock", self.nodes_unlock),
            ApiRoute("GET", "settings", self.settings_get),
            ApiRoute("PUT", "settings", self.settings_set),
            ApiRoute("POST", "settings/unset", self.settings_unset),
            # tokens and OAuth clients
            ApiRoute("GET", "tokens", self.tokens_get),
            ApiRoute("POST", "tokens", self.tokens_issue),
            ApiRoute("POST", "tokens/revoke", self.tokens_revoke),
            ApiRoute("GET", "clients", self.clients_get),
            ApiRoute("POST", "clients", self.clients_add),
            ApiRoute("POST", "clients/remove", self.clients_remove),
            ApiRoute("PUT", "clients/dcr", self.clients_dcr),
            # library
            ApiRoute("GET", "services", self.services_get),
            ApiRoute("POST", "services", self.services_declare),
            ApiRoute("POST", "services/remove", self.services_remove),
            ApiRoute("POST", "services/rediscover", self.services_rediscover),
            ApiRoute("POST", "services/upstream", self.services_upstream),
            ApiRoute("GET", "applications", self.applications_get),
            ApiRoute("POST", "applications", self.applications_declare),
            ApiRoute("POST", "applications/remove", self.applications_remove),
            ApiRoute("POST", "applications/reregister", self.applications_reregister),
            ApiRoute("PUT", "applications/web-return", self.applications_web_return),
            ApiRoute("GET", "connections", self.connections_get),
            ApiRoute("POST", "connections", self.connections_add),
            ApiRoute("POST", "connections/reauthorize", self.connections_reauthorize),
            ApiRoute("POST", "connections/authorize", self.connections_authorize),
            ApiRoute("POST", "connections/callback", self.connections_callback),
            ApiRoute("POST", "connections/renew", self.connections_renew),
            ApiRoute("POST", "connections/revoke", self.connections_revoke),
            ApiRoute("POST", "connections/remove", self.connections_remove),
            ApiRoute("PUT", "connections/prefix", self.connections_prefix),
            ApiRoute("POST", "connections/tools", self.connections_tools),
            ApiRoute("POST", "placements", self.placements_add),
            ApiRoute("POST", "placements/remove", self.placements_remove),
            # audit
            ApiRoute("GET", "audit", self.audit_get),
            ApiRoute("POST", "audit/export", self.audit_export),
        ]

    # -- impacts (BR-214) -------------------------------------------------------------------------

    def impact(
        self, conn: sqlite3.Connection, act: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The impact of `act` on `params` at this moment, with its digest."""
        now = self.clock()
        if act == "tree.delete":
            d = tree.delete_impact(conn, _param(params, "path"), clock=lambda: now)
            return impact_answer(act, d.path, {
                "descendants": d.descendants, "tokens_revoked": d.active_tokens,
                "urls": d.urls, "placements_removed": d.placements,
            }, [])  # fmt: skip
        if act == "tree.rename":
            segment = params.get("segment") or None
            r = tree.rename_impact(conn, _param(params, "path"), segment, clock=lambda: now)
            return impact_answer(act, r.path, {
                "new_path": r.new_path, "urls": r.urls, "lineages_revoked": r.lineages,
            }, [])  # fmt: skip
        if act == "tree.move":
            m = tree.move_impact(
                conn, _param(params, "path"), _param(params, "parent"), clock=lambda: now
            )
            return impact_answer(act, m.path, {
                "new_path": m.new_path, "urls": m.roles, "lineages_revoked": m.lineages,
                "tokens_lost": m.tokens_lost, "tokens_gained": m.tokens_gained,
            }, [s.as_dict() for s in m.shares])  # fmt: skip
        if act == "tree.sensitive":
            on = _flag(params, "sensitive")
            c = tree.sensitive_impact(conn, _param(params, "path"), on, clock=lambda: now)
            return impact_answer(act, c.path, {
                "sensitive": on, "reopened_tokens": c.reopened_tokens,
                "env_tokens": c.env_tokens, "long_role_tokens": c.long_role_tokens,
            }, [s.as_dict() for s in c.shares])  # fmt: skip
        if act in ("connection.revoke", "connection.remove"):
            view = connections.views(conn, self.key, _param(params, "name"))[0]
            roles = [p for p, _ in view.placements]
            count = "placements" if act == "connection.revoke" else "placements_removed"
            return impact_answer(act, view.name, {count: len(roles), "roles": roles}, [])
        if act == "client.remove":
            client_id = _param(params, "client_id")
            if not any(c.client_id == client_id for c in clients.list_clients(conn)):
                raise Refused(f"unknown client: {client_id}")
            (lineages,) = conn.execute(
                "SELECT count(*) FROM lineage WHERE client_id = ? AND revoked = 0", (client_id,)
            ).fetchone()
            return impact_answer(act, client_id, {"lineages_revoked": int(lineages)}, [])
        if act == "application.remove":
            app_id = _param(params, "id")
            if not any(a.id == app_id for a in connections.application_list(conn)):
                raise Refused(f"unknown application: {app_id}")
            users = [v.name for v in connections.views(conn, self.key) if v.application == app_id]
            return impact_answer(act, app_id, {"connections": len(users)}, [])
        if act == "service.remove":
            service_id = _param(params, "id")
            if not any(s.id == service_id for s in connections.service_list(conn)):
                raise Refused(f"unknown service: {service_id}")
            apps = [a.id for a in connections.application_list(conn) if a.service_id == service_id]
            users = [v.name for v in connections.views(conn, self.key) if v.service == service_id]
            return impact_answer(
                act, service_id, {"applications": len(apps), "connections": len(users)}, []
            )
        if act == "application.reregister":
            app_id = _param(params, "id")
            if not any(a.id == app_id for a in connections.application_list(conn)):
                raise Refused(f"unknown application: {app_id}")
            # what the CLI's refusal lists: the connections moved to reauthentication required
            moved = sorted(
                v.name for v in connections.views(conn, self.key) if v.application == app_id
            )
            return impact_answer(act, app_id, {"reauth_required": moved}, [])
        raise ApiError(
            422, "invalid_request", f"unknown act: {act} (acts: {', '.join(IMPACT_ACTS)})"
        )

    def checked(
        self, ctx: Ctx, act: str, params: Mapping[str, Any], op: Callable[[sqlite3.Connection], T]
    ) -> Callable[[sqlite3.Connection], T]:
        """The act `op`, run in one transaction with the comparison of the quoted digest."""
        quoted = ctx.body.get("impact")

        def run(conn: sqlite3.Connection) -> T:
            with transaction(conn):
                current = self.impact(conn, act, params)
                if not isinstance(quoted, str) or quoted != current["digest"]:
                    raise ApiError(409, "impact_changed", IMPACT_CHANGED, impact=current)
                return op(conn)

        return run

    def quoted(
        self, ctx: Ctx, act: str, params: Mapping[str, Any]
    ) -> Callable[[sqlite3.Connection], None]:
        """The digest comparison alone, handed to the acts that reach a provider (connection
        revoke and remove, application reregister, BR-097) as their `precondition`: the domain
        runs it in the transaction that erases or revokes, after any wait for a renewal."""
        quoted = ctx.body.get("impact")

        def run(conn: sqlite3.Connection) -> None:
            current = self.impact(conn, act, params)
            if not isinstance(quoted, str) or quoted != current["digest"]:
                raise ApiError(409, "impact_changed", IMPACT_CHANGED, impact=current)

        return run

    async def impacts_get(self, ctx: Ctx) -> Response:
        params = dict(ctx.request.query_params)
        act = params.pop("act", "")
        if act not in IMPACT_ACTS:
            raise ApiError(422, "invalid_request", f"act: one of {', '.join(IMPACT_ACTS)}")
        return JSONResponse(await self.read(ctx, lambda conn: self.impact(conn, act, params)))

    # -- tree and settings (CLI `tree`, `settings`) -----------------------------------------------

    def _node(self, t: Tree, node: tree.Node) -> dict[str, Any]:
        path = t.path(node)
        locker = t.locked_by(node)
        return {
            "id": node.id,
            "path": path,
            "parent": t.path(t.nodes[node.parent_id]) if node.parent_id is not None else None,
            "level": node.level,
            "segment": node.slug,
            "name": node.name,
            "sensitive": t.is_sensitive(node),
            "marked": node.sensitive if node.level == "env" else False,
            "locked_by": t.path(locker) if locker is not None else None,
            "locked_here": locker is node,
            # the access point's URL, as `tree show` gives it (BR-211: the console computes none)
            "url": (
                routes.resource_url(self.public_url, t.public_id, path)
                if node.level == "role"
                else None
            ),
        }

    async def nodes_get(self, ctx: Ctx) -> Response:
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            t = Tree.load(conn)
            return [self._node(t, n) for n in t.subtree(t.org)]

        return JSONResponse({"nodes": await self.read(ctx, op)})

    async def nodes_add(self, ctx: Ctx) -> Response:
        parent, level, name = _str(ctx, "parent"), _str(ctx, "level"), _str(ctx, "name")
        segment = _opt_str(ctx, "segment")

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            added = tree.add(conn, parent, level, name, segment, clock=self.clock)
            t = Tree.load(conn)
            return [self._node(t, t.resolve(p)) for p in added.created]

        return JSONResponse(
            {"created": await self.db_act(ctx, op), **CLIENTS_NOTICE}, status_code=201
        )

    async def nodes_rename(self, ctx: Ctx) -> Response:
        path, name, segment = _str(ctx, "path"), _str(ctx, "name"), _opt_str(ctx, "segment")
        params = {"path": path, "segment": segment or ""}
        done = await self.db_act(ctx, self.checked(
            ctx, "tree.rename", params,
            lambda conn: tree.rename(conn, path, name, segment, clock=self.clock),
        ))  # fmt: skip
        return JSONResponse({
            "old_path": done.old_path, "new_path": done.new_path,
            "lineages_revoked": done.lineages_revoked, **CLIENTS_NOTICE,
        })  # fmt: skip

    async def nodes_move(self, ctx: Ctx) -> Response:
        path, parent = _str(ctx, "path"), _str(ctx, "parent")
        done = await self.db_act(ctx, self.checked(
            ctx, "tree.move", {"path": path, "parent": parent},
            lambda conn: tree.move(conn, path, parent, clock=self.clock),
        ))  # fmt: skip
        return JSONResponse({"path": done.path, "new_path": done.new_path, **CLIENTS_NOTICE})

    async def nodes_delete(self, ctx: Ctx) -> Response:
        path = _str(ctx, "path")
        done = await self.db_act(ctx, self.checked(
            ctx, "tree.delete", {"path": path},
            lambda conn: tree.delete(conn, path, clock=self.clock),
        ))  # fmt: skip
        return JSONResponse({"path": done.path, "descendants": done.descendants, **CLIENTS_NOTICE})

    async def nodes_sensitive(self, ctx: Ctx) -> Response:
        path, on = _str(ctx, "path"), _bool(ctx, "sensitive")
        done = await self.db_act(ctx, self.checked(
            ctx, "tree.sensitive", {"path": path, "sensitive": "true" if on else "false"},
            lambda conn: tree.set_sensitive(conn, path, on, clock=self.clock),
        ))  # fmt: skip
        return JSONResponse({"path": done.path, "sensitive": on, **CLIENTS_NOTICE})

    async def _locking(self, ctx: Ctx, locking: bool) -> Response:
        path = _str(ctx, "path")

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            if locking:
                tree.lock(conn, path, clock=self.clock)
            else:
                tree.unlock(conn, path, clock=self.clock)
            t = Tree.load(conn)
            node = self._node(t, t.resolve(path))
            return {"path": node["path"], "locked_by": node["locked_by"]}

        return JSONResponse(await self.db_act(ctx, op))

    async def nodes_lock(self, ctx: Ctx) -> Response:
        return await self._locking(ctx, True)

    async def nodes_unlock(self, ctx: Ctx) -> Response:
        return await self._locking(ctx, False)

    async def settings_get(self, ctx: Ctx) -> Response:
        path = ctx.request.query_params.get("path", "/")

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            t = Tree.load(conn)
            here = t.path(t.resolve(path))
            values = tree.settings_get(conn, path)
            return {
                "path": here,
                "settings": [
                    {"key": v.key, "value": v.value, "origin": v.origin, "here": v.origin == here}
                    for v in values
                ],
            }

        return JSONResponse(await self.read(ctx, op))

    async def settings_set(self, ctx: Ctx) -> Response:
        path, key = _str(ctx, "path"), _str(ctx, "key")
        value = _given_str(ctx, "value")
        if value is None:
            raise ApiError(422, "invalid_request", "value: a string")
        done = await self.db_act(
            ctx, lambda conn: tree.settings_set(conn, path, key, value, clock=self.clock)
        )
        return JSONResponse(
            {"key": done.key, "value": done.value, "origin": done.origin, **CLIENTS_NOTICE}
        )

    async def settings_unset(self, ctx: Ctx) -> Response:
        path, key = _str(ctx, "path"), _str(ctx, "key")
        done = await self.db_act(
            ctx, lambda conn: tree.settings_unset(conn, path, key, clock=self.clock)
        )
        return JSONResponse(
            {"key": done.key, "value": done.value, "origin": done.origin, **CLIENTS_NOTICE}
        )

    # -- tokens and OAuth clients (CLI `token`, `client`) -----------------------------------------

    async def tokens_get(self, ctx: Ctx) -> Response:
        query = ctx.request.query_params
        path, client = query.get("path") or None, query.get("client") or None
        listed = await self.read(
            ctx, lambda conn: tokens.list_tokens(conn, path, client, clock=self.clock)
        )
        return JSONResponse({"tokens": [t.as_dict() for t in listed]})

    async def tokens_issue(self, ctx: Ctx) -> Response:
        path, label = _str(ctx, "path"), _str(ctx, "label")
        duration, confirm = _opt_str(ctx, "duration"), _opt_str(ctx, "confirm")
        issued = await self.db_act(
            ctx, lambda conn: tokens.issue(conn, path, label, duration, confirm, clock=self.clock)
        )
        # BR-053 v3, BR-213: the one response carrying a secret, once
        return JSONResponse(
            {
                "id": issued.id,
                "value": issued.value,
                "path": issued.path,
                "label": issued.label,
                "expires_at": audit.utc(issued.expires_at),
                "capped": issued.capped,
                "uncovered_sensitive": list(issued.uncovered_sensitive),
            },
            status_code=201,
        )

    async def tokens_revoke(self, ctx: Ctx) -> Response:
        token_id = _str(ctx, "id")
        done = await self.db_act(ctx, lambda conn: tokens.revoke(conn, token_id, clock=self.clock))
        return JSONResponse({"id": done.id, "type": done.kind, "already": done.already})

    async def clients_get(self, ctx: Ctx) -> Response:
        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            return {
                "clients": [c.as_dict() for c in clients.list_clients(conn)],
                "dcr": oauth.dcr_enabled(conn),
            }

        return JSONResponse(await self.read(ctx, op))

    async def clients_add(self, ctx: Ctx) -> Response:
        client_id, path = _str(ctx, "client_id"), _str(ctx, "path")
        uris, name = _str_list(ctx, "redirect_uris"), _opt_str(ctx, "name")
        added = await self.db_act(
            ctx, lambda conn: clients.add(conn, client_id, path, uris, name, clock=self.clock)
        )
        return JSONResponse(
            {
                "client_id": added.client_id,
                "path": added.path,
                "redirect_uris": list(added.redirect_uris),
            },
            status_code=201,
        )

    async def clients_remove(self, ctx: Ctx) -> Response:
        client_id = _str(ctx, "client_id")
        done = await self.db_act(ctx, self.checked(
            ctx, "client.remove", {"client_id": client_id},
            lambda conn: clients.remove(conn, client_id, clock=self.clock),
        ))  # fmt: skip
        return JSONResponse(
            {"client_id": done.client_id, "lineages_revoked": done.lineages_revoked}
        )

    async def clients_dcr(self, ctx: Ctx) -> Response:
        on = _bool(ctx, "on")
        await self.db_act(ctx, lambda conn: clients.set_dcr(conn, on, clock=self.clock))
        return JSONResponse({"dcr": on})

    # -- services (CLI `service`) ------------------------------------------------------------------

    async def services_get(self, ctx: Ctx) -> Response:
        listed = await self.read(ctx, connections.service_list)
        return JSONResponse({"services": [s.as_dict() for s in listed]})

    async def services_declare(self, ctx: Ctx) -> Response:
        service_id, server = _str(ctx, "id"), _str(ctx, "server")
        model = _opt_str(ctx, "model") or connections.DEFAULT_MODEL
        scopes, internal = _opt_str(ctx, "scopes"), _opt_bool(ctx, "internal_host")
        declared = await self.db_act(ctx, lambda conn: connections.service_declare(
            conn, self.outbound, service_id, server, model, scopes, internal, clock=self.clock
        ), admit=True, recheck=True)  # fmt: skip
        return JSONResponse(
            {"service": declared.service.as_dict(), "addresses": list(declared.addresses)},
            status_code=201,
        )

    async def services_remove(self, ctx: Ctx) -> Response:
        service_id = _str(ctx, "id")
        await self.db_act(ctx, self.checked(
            ctx, "service.remove", {"id": service_id},
            lambda conn: connections.service_remove(conn, service_id, clock=self.clock),
        ))  # fmt: skip
        return JSONResponse({"removed": service_id})

    async def services_rediscover(self, ctx: Ctx) -> Response:
        service_id, confirm = _str(ctx, "id"), _opt_str(ctx, "confirm")
        before, after = await self.db_act(ctx, lambda conn: connections.service_rediscover(
            conn, self.outbound, service_id, confirm, clock=self.clock
        ), admit=True, recheck=True)  # fmt: skip
        return JSONResponse({"before": before.as_dict(), "after": after.as_dict()})

    async def services_upstream(self, ctx: Ctx) -> Response:
        service_id, confirm = _str(ctx, "id"), _opt_str(ctx, "confirm")
        if _opt_bool(ctx, "remove"):
            if any(k in ctx.body for k in ("type", "mcp_url", "mcp_internal_host")):
                raise ApiError(
                    422, "invalid_request", "remove: no type, mcp_url or mcp_internal_host"
                )
            await self.db_act(ctx, lambda conn: connections.service_upstream_remove(
                conn, service_id, confirm, clock=self.clock
            ))  # fmt: skip
            removed = await self.read(ctx, connections.service_list)
            service = next(s for s in removed if s.id == service_id)
            return JSONResponse({"service": service.as_dict(), "changed": True, **CLIENTS_NOTICE})
        kind, url = _str(ctx, "type"), _str(ctx, "mcp_url")
        internal = _opt_bool(ctx, "mcp_internal_host")
        done = await self.db_act(ctx, lambda conn: connections.service_upstream(
            conn, self.upstream.transport, service_id, kind, url, internal, confirm,
            clock=self.clock,
        ))  # fmt: skip
        return JSONResponse(
            {"service": done.service.as_dict(), "changed": done.changed, **CLIENTS_NOTICE}
        )

    # -- applications (CLI `application`) ----------------------------------------------------------

    def _application(self, app: connections.Application) -> dict[str, Any]:
        return {**app.as_dict(), "web_return": app.web_return}

    async def applications_get(self, ctx: Ctx) -> Response:
        listed = await self.read(ctx, connections.application_list)
        return JSONResponse(
            {
                "applications": [self._application(a) for a in listed],
                "web_redirect_uri": self.web_redirect,
            }
        )

    async def applications_declare(self, ctx: Ctx) -> Response:
        app_id, service_id, mode = _str(ctx, "id"), _str(ctx, "service"), _str(ctx, "mode")
        if mode == "dynamic":
            if "client_id" in ctx.body or "secret" in ctx.body:
                raise ApiError(
                    422, "invalid_request", "mode dynamic: neither client_id nor secret (BR-083)"
                )
            await self.act(ctx, lambda: lifecycle.register(
                self.db.call, self.key, self.outbound, app_id, service_id, clock=self.clock,
                web_redirect=self.web_redirect,
            ))  # fmt: skip
        elif mode == "declared":
            client_id, secret = _str(ctx, "client_id"), _given_str(ctx, "secret")
            # a secret given, even empty, passes the CLI reader's checks (BR-211, round 2)
            secret = None if secret is None else _secret(secret)
            await self.db_act(ctx, lambda conn: connections.application_declare(
                conn, self.key, app_id, service_id, client_id, secret, clock=self.clock
            ))  # fmt: skip
        else:
            raise ApiError(422, "invalid_request", "mode: declared or dynamic")
        listed = await self.read(ctx, connections.application_list)
        app = next(a for a in listed if a.id == app_id)
        return JSONResponse({"application": self._application(app)}, status_code=201)

    async def applications_remove(self, ctx: Ctx) -> Response:
        app_id = _str(ctx, "id")
        await self.db_act(ctx, self.checked(
            ctx, "application.remove", {"id": app_id},
            lambda conn: connections.application_remove(conn, app_id, clock=self.clock),
        ))  # fmt: skip
        return JSONResponse({"removed": app_id})

    async def applications_reregister(self, ctx: Ctx) -> Response:
        app_id, confirm = _str(ctx, "id"), _opt_str(ctx, "confirm")
        check = self.quoted(ctx, "application.reregister", {"id": app_id})
        reg = await self.act(ctx, lambda: lifecycle.reregister(
            self.db.call, self.key, self.outbound, app_id, confirm, clock=self.clock,
            web_redirect=self.web_redirect, precondition=check,
        ))  # fmt: skip
        return JSONResponse(
            {
                "id": reg.app_id,
                "client_id": reg.client_id,
                "upstream": [{"connection": n, "revocation": o} for n, o in reg.upstream],
                **CLIENTS_NOTICE,
            }
        )

    async def applications_web_return(self, ctx: Ctx) -> Response:
        app_id, registered = _str(ctx, "id"), _bool(ctx, "registered")
        app = await self.db_act(ctx, lambda conn: connections.application_web_return(
            conn, app_id, registered, clock=self.clock
        ))  # fmt: skip
        return JSONResponse({"id": app.id, "web_return": app.web_return})

    # -- connections and placements (CLI `connection`) --------------------------------------------

    async def connections_get(self, ctx: Ctx) -> Response:
        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            apps = {a.id: a for a in connections.application_list(conn)}
            listed = connections.views(conn, self.key, clock=self.clock)
            return {
                "connections": [
                    {
                        **v.as_dict(),
                        "web_return": bool(
                            v.application in apps and apps[v.application].web_return
                        ),
                    }
                    for v in listed
                ],
                "key": self.key.status(conn),
            }

        return JSONResponse(await self.read(ctx, op))

    def _provided(self, ctx: Ctx) -> tuple[str, str | None, str | None] | None:
        provided = ctx.body.get("provided")
        if provided is None:
            return None
        if not isinstance(provided, dict) or not isinstance(provided.get("value"), str):
            raise ApiError(422, "invalid_request", "provided: an object with a value")
        account = provided.get("account")
        if account is not None and not isinstance(account, str):
            raise ApiError(422, "invalid_request", "provided.account: a string")
        expiry = provided.get("expires_at")
        if expiry is not None and not isinstance(expiry, str):
            raise ApiError(422, "invalid_request", "provided.expires_at: a string")
        return _secret(str(provided["value"])), account, expiry

    async def connections_add(self, ctx: Ctx) -> Response:
        name, service_id, prefix = _str(ctx, "name"), _str(ctx, "service"), _str(ctx, "prefix")
        provided = self._provided(ctx)
        application = _opt_str(ctx, "application")
        if provided is not None:
            if application is not None:
                raise ApiError(
                    422, "invalid_request", "provided and application are mutually exclusive"
                )
            value, account, expiry = provided
            added = await self.db_act(ctx, lambda conn: connections.add_static(
                conn, self.key, name, service_id, value, prefix, account,
                clock=self.clock, expires_at=expiry
            ))  # fmt: skip
        else:
            if application is None:
                raise ApiError(422, "invalid_request", "application or provided required")
            added = await self.db_act(ctx, lambda conn: connections.add_oauth(
                conn, self.key, name, service_id, application, prefix, clock=self.clock,
                web_redirect=self.web_redirect,
            ))  # fmt: skip
        return JSONResponse(
            {
                "name": added.name,
                "type": added.kind,
                "state": added.state,
                "authorization_url": added.url,
                "redirect": added.redirect,
            },
            status_code=201,
        )

    async def connections_reauthorize(self, ctx: Ctx) -> Response:
        name = _str(ctx, "name")
        provided = self._provided(ctx)
        if provided is not None:
            value, account, expiry = provided
            row = await self.db_act(ctx, lambda conn: connections.replace_static(
                conn, self.key, name, value, account, clock=self.clock, expires_at=expiry
            ))  # fmt: skip
            return JSONResponse(
                {
                    "name": name.strip("/"),
                    "state": "active",
                    "authorization_url": None,
                    "redirect": None,
                    "generation": row.generation,
                }
            )
        opened = await self.db_act(ctx, lambda conn: connections.reopen(
            conn, self.key, name, clock=self.clock, web_redirect=self.web_redirect
        ))  # fmt: skip
        return JSONResponse(
            {
                "name": opened.name,
                "state": opened.state,
                "authorization_url": opened.url,
                "redirect": opened.redirect,
            }
        )

    async def _authorize(self, ctx: Ctx, name: str, callback: str) -> Response:
        confirm_account = _opt_bool(ctx, "confirm_account")
        done = await self.act(ctx, lambda: lifecycle.authorize(
            self.db.call, self.key, self.outbound, name, callback, confirm_account,
            clock=self.clock,
        ))  # fmt: skip
        return JSONResponse(
            {
                "name": done.name,
                "account": done.account,
                "scopes": done.scopes,
                "expires_at": audit.utc(done.expires_at),
                "expiry_estimated": done.estimated,
                **CLIENTS_NOTICE,
            }
        )

    async def connections_authorize(self, ctx: Ctx) -> Response:
        """The paste path (F-006): the return URL the provider showed, pasted in the console."""
        return await self._authorize(ctx, _str(ctx, "name"), _str(ctx, "callback"))

    async def connections_callback(self, ctx: Ctx) -> Response:
        """BR-216: the console's return route posts `state` and `code` under the session; the
        connection is the one whose open authorisation carries that `state`."""
        state = _str(ctx, "state")
        code, error = _opt_str(ctx, "code"), _opt_str(ctx, "error")
        name = await self.read(ctx, lambda conn: connections.connection_for_state(conn, state))
        if name is None:
            raise ApiError(409, "refused", "unknown or used state: open the authorisation again")
        query = {
            "state": state,
            **({"code": code} if code else {}),
            **({"error": error} if error else {}),
        }
        return await self._authorize(ctx, name, f"{self.web_redirect}?{urlencode(query)}")

    async def connections_renew(self, ctx: Ctx) -> Response:
        name = _str(ctx, "name")
        renamed, got = await self.act(ctx, lambda: lifecycle.renew_now(
            self.db.call, self.key, self.outbound, name, clock=self.clock
        ))  # fmt: skip
        return JSONResponse(
            {
                "name": renamed,
                "generation": got.generation,
                "expires_at": None if got.expires_at is None else audit.utc(got.expires_at),
            }
        )

    async def connections_revoke(self, ctx: Ctx) -> Response:
        name = _str(ctx, "name")
        check = self.quoted(ctx, "connection.revoke", {"name": name})
        done = await self.act(ctx, lambda: lifecycle.revoke(
            self.db.call, self.key, self.outbound, name, clock=self.clock, precondition=check
        ), recheck=True)  # fmt: skip
        return JSONResponse({"name": done.connection, "upstream": done.upstream, **CLIENTS_NOTICE})

    async def connections_remove(self, ctx: Ctx) -> Response:
        name = _str(ctx, "name")
        check = self.quoted(ctx, "connection.remove", {"name": name})
        done = await self.act(ctx, lambda: lifecycle.remove(
            self.db.call, self.key, self.outbound, name, name.strip("/"), clock=self.clock,
            precondition=check,
        ), recheck=True)  # fmt: skip
        return JSONResponse(
            {
                "name": done.connection,
                "placements": done.placements,
                "upstream": done.upstream,
                **CLIENTS_NOTICE,
            }
        )

    async def connections_prefix(self, ctx: Ctx) -> Response:
        name, prefix = _str(ctx, "name"), _str(ctx, "prefix")
        done = await self.db_act(
            ctx, lambda conn: connections.set_prefix(conn, name, prefix, clock=self.clock)
        )
        return JSONResponse({"name": name.strip("/"), "prefix": done, **CLIENTS_NOTICE})

    async def connections_tools(self, ctx: Ctx) -> Response:
        name, role = _str(ctx, "name"), _str(ctx, "role")
        purge, confirm = _opt_bool(ctx, "purge_names"), _opt_str(ctx, "confirm")

        def op() -> relay.Listed:
            try:
                return relay.tools(
                    self.db.call, self.key, self.outbound, self.upstream, name, role, purge,
                    confirm, clock=self.clock,
                )  # fmt: skip
            except UpstreamError as exc:
                raise lifecycle.Unavailable(f"MCP upstream: {exc.code} ({exc.reason})") from None

        listed = await self.act(ctx, op, recheck=purge)
        return JSONResponse(
            {
                "connection": listed.connection,
                "role": listed.role,
                "version": listed.version,
                "tools": [{"name": p, "upstream_name": u} for p, u in listed.tools],
                "invalid_names": listed.invalid,
                "purged_names": listed.purged,
            }
        )

    async def placements_add(self, ctx: Ctx) -> Response:
        name, role = _str(ctx, "connection"), _str(ctx, "role")
        confirm = _opt_bool(ctx, "confirm_share")
        placed = await self.db_act(
            ctx, lambda conn: connections.place(conn, name, role, confirm, clock=self.clock)
        )
        return JSONResponse(
            {
                "connection": placed.connection,
                "role": placed.role,
                "sensitive": placed.sensitive,
                "confirmed": list(placed.confirmed),
                **CLIENTS_NOTICE,
            },
            status_code=201,
        )

    async def placements_remove(self, ctx: Ctx) -> Response:
        name, role = _str(ctx, "connection"), _str(ctx, "role")
        done = await self.db_act(
            ctx, lambda conn: connections.unplace(conn, name, role, clock=self.clock)
        )
        return JSONResponse({"connection": name.strip("/"), "role": done, **CLIENTS_NOTICE})

    # -- audit (BR-218) ----------------------------------------------------------------------------

    async def audit_get(self, ctx: Ctx) -> Response:
        query = ctx.request.query_params
        where = _audit_filter({k: query.get(k) for k in audit.FILTER_KEYS})
        limit = _limit(query.get("limit"))
        before = _seq(query.get("before"))
        entries, following = await self.read(
            ctx, lambda conn: audit.query(conn, where, before=before, limit=limit)
        )
        return JSONResponse(
            {
                "entries": [
                    {**e.as_dict(), **changes.eligibility(changes.decoded(e))} for e in entries
                ],
                "next": following,
            }
        )

    async def audit_export(self, ctx: Ctx) -> Response:
        where = _audit_filter({k: ctx.body.get(k) for k in audit.FILTER_KEYS})

        def op(conn: sqlite3.Connection) -> list[audit.Entry]:
            with transaction(conn):
                entries, _ = audit.query(conn, where, limit=EXPORT_MAX)
                audit.write(
                    conn,
                    actor=audit.actor(),
                    action="audit.export",
                    result="ok",
                    detail={"filter": where.as_dict(), "entries": len(entries)},
                    now=ctx.now,
                )
                return entries

        entries = await self.db_act(ctx, op)
        lines = "".join(
            json.dumps(e.as_dict(), ensure_ascii=False, sort_keys=True) + "\n" for e in entries
        )
        return Response(
            lines,
            media_type="application/x-ndjson",
            headers={"content-disposition": 'attachment; filename="audit.jsonl"'},
        )


# -- body and query readers ------------------------------------------------------------------------


def _key_text(reason: str) -> str:
    return f"connections key unavailable: {reason} (nothing was written)"


def _secret(value: str) -> str:
    """The CLI reader's checks on a provided token or an application secret (BR-086, BR-211)."""
    try:
        return connections.secret_value(value)
    except Refused as exc:
        raise ApiError(409, "refused", str(exc)) from None


def _str(ctx: Ctx, name: str) -> str:
    value = ctx.body.get(name)
    if not isinstance(value, str) or not value:
        raise ApiError(422, "invalid_request", f"{name}: a non-empty string is expected")
    return value


def _opt_str(ctx: Ctx, name: str) -> str | None:
    value = ctx.body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(422, "invalid_request", f"{name}: a string is expected")
    return value or None


def _given_str(ctx: Ctx, name: str) -> str | None:
    """An optional string kept as given: absent is None, an empty string stays empty (a secret
    the CLI would read from an empty file, refused by `secret_value`)."""
    value = ctx.body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(422, "invalid_request", f"{name}: a string is expected")
    return value


def _str_list(ctx: Ctx, name: str) -> list[str]:
    value = ctx.body.get(name)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ApiError(422, "invalid_request", f"{name}: a list of strings is expected")
    return [str(v) for v in value]


def _bool(ctx: Ctx, name: str) -> bool:
    value = ctx.body.get(name)
    if not isinstance(value, bool):
        raise ApiError(422, "invalid_request", f"{name}: true or false is expected")
    return value


def _opt_bool(ctx: Ctx, name: str) -> bool:
    value = ctx.body.get(name, False)
    if not isinstance(value, bool):
        raise ApiError(422, "invalid_request", f"{name}: true or false is expected")
    return value


def _param(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise ApiError(422, "invalid_request", f"{name}: required")
    return value


def _flag(params: Mapping[str, Any], name: str) -> bool:
    value = params.get(name)
    if value in ("true", True):
        return True
    if value in ("false", False):
        return False
    raise ApiError(422, "invalid_request", f"{name}: true or false")


def _limit(text: str | None) -> int:
    if text is None or text == "":
        return PAGE_DEFAULT
    if not (text.isascii() and text.isdigit() and len(text) <= 3) or not 1 <= int(text) <= PAGE_MAX:
        raise ApiError(422, "invalid_request", f"limit: 1 to {PAGE_MAX}")
    return int(text)


def _seq(text: str | None) -> int | None:
    if text is None or text == "":
        return None
    if not (text.isascii() and text.isdigit() and len(text) <= 19) or int(text) > SEQ_MAX:
        raise ApiError(422, "invalid_request", "before: a sequence number")
    return int(text)


def _audit_filter(values: Mapping[str, Any]) -> audit.Filter:
    clean: dict[str, str | None] = {}
    for key, value in values.items():
        if value is not None and not isinstance(value, str):
            raise ApiError(422, "invalid_request", f"{key}: a string is expected")
        clean[key] = value or None
    try:
        return audit.Filter(**clean)
    except ValueError as exc:
        raise ApiError(422, "invalid_request", str(exc)) from None
