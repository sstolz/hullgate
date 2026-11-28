"""SCR-11 history and SCR-12 import/export adapters; decisions live in the domain."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Any

from starlette.responses import Response

from hullgate import changes, export, importing
from hullgate.admin.base import ApiError, ApiRoute, Ctx, TextJSONResponse
from hullgate.admin.console import CLIENTS_NOTICE, ConsoleApi, _flag, _str
from hullgate.db import StoreUnavailable, connect


class HistoryApi:
    def __init__(self, core: ConsoleApi) -> None:
        self.core = core

    async def independent[T](
        self, ctx: Ctx, fn: Callable[[sqlite3.Connection], T], *, recheck: bool = False
    ) -> T:
        """Composite preparation may wait or exchange with providers; use an owned connection.

        Only the short database steps acquire SQLite's lock, never Database.call's process lock.
        The domain rechecks its snapshot in the final writing transaction.
        """

        def call() -> T:
            try:
                with closing(connect(self.core.db.path, create=False)) as conn:
                    return fn(conn)
            except sqlite3.Error as exc:
                raise StoreUnavailable(str(exc)) from exc

        return await self.core.act(ctx, call, recheck=recheck)

    def routes(self) -> list[ApiRoute]:
        return [
            ApiRoute("GET", "changes", self.change),
            ApiRoute("POST", "changes/undo", self.undo),
            ApiRoute("GET", "export", self.export),
            ApiRoute("POST", "import/preview", self.preview),
            ApiRoute("POST", "import/apply", self.apply),
        ]

    def seq(self, value: Any) -> int:
        try:
            if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
                raise ValueError
            seq = int(value)
            if not 0 < seq <= 2**63 - 1:
                raise ValueError
            return seq
        except (ValueError, TypeError):
            raise ApiError(
                422, "invalid_request", "seq: a positive SQLite integer is required"
            ) from None

    async def change(self, ctx: Ctx) -> Response:
        seq = self.seq(ctx.request.query_params.get("seq"))
        result = await self.core.read(ctx, lambda c: changes.plan(c, seq, clock=self.core.clock))
        return TextJSONResponse(result)

    async def undo(self, ctx: Ctx) -> Response:
        seq = self.seq(ctx.body.get("seq"))
        result = await self.independent(
            ctx,
            lambda c: changes.undo(
                changes.Operations(
                    c, self.core.key, self.core.outbound, self.core.upstream, self.core.clock
                ),
                seq,
                impact=ctx.body.get("impact"),
                confirm_share=_flag({"confirm_share": False, **ctx.body}, "confirm_share"),
            ),
            recheck=True,
        )
        return TextJSONResponse({**result, **CLIENTS_NOTICE})

    async def export(self, ctx: Ctx) -> Response:
        result = await self.core.read(ctx, export.export_yaml)
        return Response(
            result,
            media_type="application/yaml",
            headers={"Content-Disposition": 'attachment; filename="hullgate.yaml"'},
        )

    async def preview(self, ctx: Ctx) -> Response:
        document = _str(ctx, "document")
        result = await self.independent(
            ctx, lambda c: importing.preview(c, document, clock=self.core.clock)
        )
        return TextJSONResponse(result)

    async def apply(self, ctx: Ctx) -> Response:
        document, digest = _str(ctx, "document"), _str(ctx, "digest")
        result = await self.independent(
            ctx,
            lambda c: importing.apply(
                changes.Operations(
                    c, self.core.key, self.core.outbound, self.core.upstream, self.core.clock
                ),
                document,
                digest,
                apply_deletions=_flag({"apply_deletions": False, **ctx.body}, "apply_deletions"),
                confirm_share=_flag({"confirm_share": False, **ctx.body}, "confirm_share"),
            ),
            recheck=True,
        )
        return TextJSONResponse({**result, **CLIENTS_NOTICE})
