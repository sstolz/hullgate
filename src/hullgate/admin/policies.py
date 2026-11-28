"""The console's policy surfaces of `/admin/api/v1` (F-016 slice 3): the rights preview of a role
or of a token on a role (BR-217), the policy of a placement (mode, lists, argument rules, pin) and
the tool catalogue (queue, classify, accept, diff, validate), all on F-015's operations.

Every write calls the domain operation the CLI calls for the same verb, as the administrator, and
returns its refusal as it is (BR-211); the console computes no right, class or verdict. The rights
preview and `tools/diff` are `POST`: their fresh upstream listing records the catalogue and may
suspend a pinned tool (F-015 BR-173, BR-181), which a `GET` may not do (BR-234).
"""

from __future__ import annotations

import math
from typing import Any

from starlette.responses import Response

from hullgate import pinning, policy, preview, relay
from hullgate.admin.base import ApiError, ApiRoute, Ctx, TextJSONResponse
from hullgate.admin.console import (
    CLIENTS_NOTICE,
    ConsoleApi,
    _given_str,
    _limit,
    _opt_str,
    _str,
    _str_list,
)


def _policy_doc(shown: policy.Shown) -> dict[str, Any]:
    """`policy show --json`'s document, plus `names`: the upstream names behind its labels, which
    the console sends back to act on an entry (a label may differ from its name)."""
    return {**shown.as_dict(), "names": shown.names}


def nonfinite(value: Any) -> bool:
    return isinstance(value, float) and not math.isfinite(value)


def _change_doc(change: pinning.Change) -> dict[str, Any]:
    doc = change.as_dict()
    for field, values in zip(change.fields, doc["fields"], strict=True):
        marks = []
        stack = [("", field.old, field.new)]
        while stack:
            path, old, new = stack.pop()
            if (nonfinite(old) and isinstance(new, str)) or (
                isinstance(old, str) and nonfinite(new)
            ):
                marks.append(
                    {
                        "path": path,
                        "old": "number" if nonfinite(old) else "string",
                        "new": "number" if nonfinite(new) else "string",
                    }
                )
            elif isinstance(old, dict) and isinstance(new, dict):
                for key in sorted(old.keys() & new.keys()):
                    segment = str(key).replace("~", "~0").replace("/", "~1")
                    stack.append((path + "/" + segment, old[key], new[key]))
            elif isinstance(old, (list, tuple)) and isinstance(new, (list, tuple)):
                stack.extend(
                    (path + "/" + str(i), a, b)
                    for i, (a, b) in enumerate(zip(old, new, strict=False))
                )
        if marks:
            values["type_marks"] = marks
    return doc


class PolicyApi:
    """Handlers of the `rights`, `placements/…` policy and `tools` routes."""

    def __init__(self, core: ConsoleApi) -> None:
        self.core = core

    def routes(self) -> list[ApiRoute]:
        return [
            ApiRoute("POST", "rights", self.rights),
            ApiRoute("GET", "placements/policy", self.policy_get),
            ApiRoute("PUT", "placements/mode", self.policy_mode),
            ApiRoute("POST", "placements/lists", self.policy_add),
            ApiRoute("POST", "placements/lists/remove", self.policy_remove),
            ApiRoute("PUT", "placements/arguments", self.policy_arg),
            ApiRoute("POST", "placements/arguments/remove", self.policy_arg_remove),
            ApiRoute("PUT", "placements/pin", self.policy_pin),
            ApiRoute("POST", "placements/tool-pins", self.policy_pin_tool),
            ApiRoute("POST", "placements/tool-pins/remove", self.policy_unpin_tool),
            ApiRoute("GET", "tools/queue", self.tools_queue),
            ApiRoute("GET", "tools/catalogue", self.tools_catalogue),
            ApiRoute("POST", "tools/classify", self.tools_classify),
            ApiRoute("POST", "tools/accept", self.tools_accept),
            ApiRoute("POST", "tools/diff", self.tools_diff),
            ApiRoute("POST", "tools/validate", self.tools_validate),
        ]

    # -- rights preview (CLI `policy preview`, BR-217) --------------------------------------------

    async def rights(self, ctx: Ctx) -> Response:
        """The role's preview, from the evaluator `tools/list` uses; with `token`, first whether
        that token reaches the role and why (steps 4 to 7 of BR-055, without its value, writing
        nothing), then the role's preview."""
        core, role = self.core, _str(ctx, "role")
        # an empty identifier is an identifier, refused as unknown, never "no token" (review 579 N4)
        token = _given_str(ctx, "token")
        reach: dict[str, Any] | None = None
        if token is not None:
            found = await core.read(ctx, lambda conn: preview.reach(
                conn, token, role, core.public_url, core.clock()
            ))  # fmt: skip
            reach = found.as_dict()
        seen = await core.act(ctx, lambda: preview.preview(
            core.db.call, core.key, core.outbound, core.upstream, role, clock=core.clock
        ))  # fmt: skip
        exposed = [t.public_name for t in seen.tools if t.exposed]
        return TextJSONResponse(
            {
                "role": seen.role,
                "token": reach,
                "exposed": exposed,
                "tools": [t.as_dict() for t in seen.tools],
                # BR-217: an upstream that could not be read is unknown, never absent
                "unavailable": [
                    {"connection": u.connection, "code": u.code} for u in seen.unavailable
                ],
                "policy_errors": list(seen.errors),
            }
        )

    # -- placement policy (CLI `policy show|mode|add|remove|arg|arg-remove|pin|…`) ----------------

    async def policy_get(self, ctx: Ctx) -> Response:
        """`policy show` of one placement (`connection` and `role`), or of every placement on a
        role under `path` (default `/`). A selector given empty is a name, refused as the CLI
        refuses it, never "every placement" (review 581 N5)."""
        query = ctx.request.query_params
        name, role = query.get("connection"), query.get("role")
        if (name is None) != (role is None):
            raise ApiError(422, "invalid_request", "connection and role: both or neither")
        if name is not None and role is not None:
            one = await self.core.read(ctx, lambda conn: policy.show(conn, name, role))
            return TextJSONResponse({"placements": [_policy_doc(one)]})
        path = query.get("path") or "/"
        shown = await self.core.read(ctx, lambda conn: policy.shown_under(conn, path))
        return TextJSONResponse({"placements": [_policy_doc(s) for s in shown]})

    def _placement(self, ctx: Ctx) -> tuple[str, str]:
        return _str(ctx, "connection"), _str(ctx, "role")

    async def _shown(self, ctx: Ctx, name: str, role: str, **extra: Any) -> Response:
        """The placement's policy after a write, with the BR-215 notice."""
        shown = await self.core.read(ctx, lambda conn: policy.show(conn, name, role))
        return TextJSONResponse({**_policy_doc(shown), **extra, **CLIENTS_NOTICE})

    async def policy_mode(self, ctx: Ctx) -> Response:
        (name, role), mode = self._placement(ctx), _str(ctx, "mode")
        await self.core.db_act(ctx, lambda conn: policy.set_mode(
            conn, name, role, mode, clock=self.core.clock
        ))  # fmt: skip
        return await self._shown(ctx, name, role)

    async def policy_add(self, ctx: Ctx) -> Response:
        (name, role), which = self._placement(ctx), _str(ctx, "list")
        by_class = _opt_str(ctx, "class")
        names = _str_list(ctx, "tools") if "tools" in ctx.body else []
        added = await self.core.db_act(ctx, lambda conn: policy.add_to_list(
            conn, name, role, which, names, by_class, clock=self.core.clock
        ))  # fmt: skip
        return await self._shown(ctx, name, role, added=added)

    async def policy_remove(self, ctx: Ctx) -> Response:
        (name, role), which = self._placement(ctx), _str(ctx, "list")
        names = _str_list(ctx, "tools")
        removed = await self.core.db_act(ctx, lambda conn: policy.list_remove(
            conn, name, role, which, names, clock=self.core.clock
        ))  # fmt: skip
        return await self._shown(ctx, name, role, removed=removed)

    async def policy_arg(self, ctx: Ctx) -> Response:
        (name, role), tool = self._placement(ctx), _str(ctx, "tool")
        argument, values = _str(ctx, "argument"), _str_list(ctx, "values")
        core = self.core
        await core.act(ctx, lambda: relay.argument_at_entry(
            core.db.call, core.key, core.outbound, core.upstream, name, role, tool, argument,
            values, clock=core.clock,
        ), recheck=True)  # fmt: skip
        return await self._shown(ctx, name, role)

    async def policy_arg_remove(self, ctx: Ctx) -> Response:
        (name, role), tool = self._placement(ctx), _str(ctx, "tool")
        await self.core.db_act(ctx, lambda conn: policy.argument_remove(
            conn, name, role, tool, clock=self.core.clock
        ))  # fmt: skip
        return await self._shown(ctx, name, role)

    async def policy_pin(self, ctx: Ctx) -> Response:
        (name, role), pin = self._placement(ctx), _str(ctx, "pin")
        await self.core.db_act(ctx, lambda conn: policy.set_pin(
            conn, name, role, pin, clock=self.core.clock
        ))  # fmt: skip
        return await self._shown(ctx, name, role)

    async def policy_pin_tool(self, ctx: Ctx) -> Response:
        (name, role), tool = self._placement(ctx), _str(ctx, "tool")
        fresh = await self.core.db_act(ctx, lambda conn: policy.pin_tool(
            conn, name, role, tool, clock=self.core.clock
        ))  # fmt: skip
        return await self._shown(ctx, name, role, already=not fresh)

    async def policy_unpin_tool(self, ctx: Ctx) -> Response:
        (name, role), tool = self._placement(ctx), _str(ctx, "tool")
        await self.core.db_act(ctx, lambda conn: policy.unpin_tool(
            conn, name, role, tool, clock=self.core.clock
        ))  # fmt: skip
        return await self._shown(ctx, name, role)

    # -- tool catalogue (CLI `tool queue|classify|accept|diff|validate`) --------------------------

    async def tools_queue(self, ctx: Ctx) -> Response:
        # `?service=` given empty is an unknown service, never "every service" (review 581 N5)
        service = ctx.request.query_params.get("service")
        waiting = await self.core.read(ctx, lambda conn: policy.queue(conn, service))
        return TextJSONResponse({"queue": [q.as_dict() for q in waiting]})

    async def tools_catalogue(self, ctx: Ctx) -> Response:
        query = ctx.request.query_params
        service = query.get("service")
        sid, name = query.get("after_service"), query.get("after_name")
        if (sid is None) != (name is None):
            raise ApiError(422, "invalid_request", "after_service and after_name: both or neither")
        after = (sid, name) if sid is not None and name is not None else None
        limit = _limit(query.get("limit"))
        rows = await self.core.read(
            ctx, lambda conn: policy.catalogue(conn, service, after=after, limit=limit + 1)
        )
        page = rows[:limit]
        cursor = (
            {"after_service": page[-1].service, "after_name": page[-1].name}
            if len(rows) > limit
            else None
        )
        return TextJSONResponse({"catalogue": [r.as_dict() for r in page], "next": cursor})

    def _classified(self, done: policy.Classified) -> Response:
        return TextJSONResponse(
            {
                "service": done.service,
                "tool": done.name,
                "class": done.cls,
                "source": done.source,
                **CLIENTS_NOTICE,
            }
        )

    async def tools_classify(self, ctx: Ctx) -> Response:
        service, tool, cls = _str(ctx, "service"), _str(ctx, "tool"), _str(ctx, "class")
        done = await self.core.db_act(ctx, lambda conn: policy.classify(
            conn, service, tool, cls, clock=self.core.clock
        ))  # fmt: skip
        return self._classified(done)

    async def tools_accept(self, ctx: Ctx) -> Response:
        service, tool = _str(ctx, "service"), _str(ctx, "tool")
        done = await self.core.db_act(ctx, lambda conn: policy.accept(
            conn, service, tool, clock=self.core.clock
        ))  # fmt: skip
        return self._classified(done)

    async def tools_diff(self, ctx: Ctx) -> Response:
        path, core = _str(ctx, "path"), self.core
        node, changes = await core.act(ctx, lambda: pinning.diff(
            core.db.call, core.key, core.outbound, core.upstream, path, clock=core.clock
        ))  # fmt: skip
        # the upstream's text travels as JSON data; the console renders it as text, never markup
        return TextJSONResponse({"node": node, "changes": [_change_doc(c) for c in changes]})

    async def tools_validate(self, ctx: Ctx) -> Response:
        path, name, core = _str(ctx, "path"), _str(ctx, "connection"), self.core
        # an empty tool is a tool name, refused as the CLI's `--tool ""` is, never "every tool"
        # (review 579 B2)
        tool = _given_str(ctx, "tool")
        done = await core.act(ctx, lambda: pinning.validate(
            core.db.call, core.key, core.outbound, core.upstream, path, name, tool,
            clock=core.clock,
        ), recheck=True)  # fmt: skip
        return TextJSONResponse(
            {
                "validated": [
                    {
                        "connection": v.connection,
                        "role": v.role,
                        "tool": v.tool,
                        "before_sha256": v.before,
                        "after_sha256": v.after,
                    }
                    for v in done
                ],
                **CLIENTS_NOTICE,
            }
        )
