"""Organisation and setup reads; existing domain operations own every ordinary write."""

from __future__ import annotations

import sqlite3
from typing import Any

from starlette.responses import JSONResponse, Response

from hullgate import connections
from hullgate.admin import accounts
from hullgate.admin.base import ApiRoute, Ctx
from hullgate.admin.console import ConsoleApi
from hullgate.db import transaction
from hullgate.tree import Tree


class OrganisationApi:
    def __init__(self, core: ConsoleApi) -> None:
        self.core = core

    def routes(self) -> list[ApiRoute]:
        return [
            ApiRoute("GET", "organisation", self.organisation),
            ApiRoute("GET", "setup", self.setup),
            ApiRoute("POST", "setup/dismiss", self.dismiss),
        ]

    async def organisation(self, ctx: Ctx) -> Response:
        def read(conn: sqlite3.Connection) -> dict[str, Any]:
            tree = Tree.load(conn)
            last = conn.execute(
                "SELECT max(at) FROM audit WHERE actor = ?"
                " AND action = 'admin.sign_in' AND result = 'ok'",
                (accounts.actor(ctx.administrator_id),),
            ).fetchone()[0]
            admin = accounts.get(conn, ctx.administrator_id)
            return {
                "name": tree.org.name,
                "administrator": admin.as_dict() if admin else None,
                "passkeys": len(accounts.passkeys(conn, ctx.administrator_id)),
                "last_sign_in": last,
                "public_url": self.core.public_url,
                "admin_url": self.core.origin.origin,
                "rp_id": self.core.origin.rp_id,
                "connection_expiry_days": int(
                    tree.effective(tree.org, "connection_expiry_days")[0]
                ),
                "applications": [a.as_dict() for a in connections.application_list(conn)],
                "web_redirect_uri": self.core.web_redirect,
            }

        return JSONResponse(await self.core.read(ctx, read))

    async def setup(self, ctx: Ctx) -> Response:
        def read(conn: sqlite3.Connection) -> dict[str, Any]:
            # Existence, not validity or a second workflow. A revoked issued credential still
            # counts as issued; deleting a client makes that step incomplete again.
            checks = (
                (
                    "client",
                    "SELECT 1 FROM node WHERE level = 'client' AND deleted = 0",
                    "/admin/tree?setup=client",
                ),
                ("service", "SELECT 1 FROM service", "/admin/tree?setup=service"),
                (
                    "connection",
                    "SELECT 1 FROM connection WHERE removed = 0",
                    "/admin/tree?setup=connection",
                ),
                ("placement", "SELECT 1 FROM placement", "/admin/tree?setup=placement"),
                (
                    "credential",
                    "SELECT 1 FROM operator_token UNION ALL SELECT 1 FROM lineage",
                    "/admin/tokens?setup=credential",
                ),
            )
            steps = [
                {"id": name, "done": conn.execute(sql).fetchone() is not None, "url": url}
                for name, sql, url in checks
            ]
            dismissed = bool(
                conn.execute("SELECT dismissed FROM admin_setup WHERE id = 1").fetchone()[0]
            )
            return {
                "steps": steps,
                "dismissed": dismissed,
                "show": not dismissed and not all(s["done"] for s in steps),
                "redirect_uris": [connections.REDIRECT_URI, self.core.web_redirect],
                "services": [
                    {"id": s.id, "kind": s.model, "scopes": s.scopes.split()}
                    for s in connections.service_list(conn)
                ],
                "kinds": ["gitea", "generic"],
            }

        return JSONResponse(await self.core.read(ctx, read))

    async def dismiss(self, ctx: Ctx) -> Response:
        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute("UPDATE admin_setup SET dismissed = 1 WHERE id = 1")

        await self.core.db_act(ctx, write)
        return JSONResponse({"dismissed": True})
