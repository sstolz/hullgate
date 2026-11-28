"""The admin API's route declaration, request context and refusal form (§3.15), shared by the
lot of identity routes (`hullgate.admin.app`) and the console's routes
(`hullgate.admin.console`)."""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hullgate.admin import sessions


class TextJSONResponse(JSONResponse):
    """A JSON answer whose strings may hold text Python can carry and UTF-8 cannot encode: a lone
    surrogate from an upstream's tool definition (`"\\ud800"` in its JSON; a request body holding
    one is refused before any handler). Starlette's strict encoding raises on it (a 500, outside
    §3.15's statuses); such a document is written ASCII-escaped instead, as the CLI's `--json`
    forms write theirs (F-016 slice 3, review 579 B3). A non-finite number (`NaN`, `Infinity`,
    which the upstream's JSON may carry and strict JSON cannot) is written as the string the
    CLI's `--json` prints for it, `"NaN"`, `"Infinity"` or `"-Infinity"`, so that the document
    stays JSON a browser parses (review 581 B3). Any other document is rendered as `JSONResponse`
    renders it."""

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except (UnicodeEncodeError, ValueError):
            return json.dumps(
                _finite(content),
                ensure_ascii=True,
                allow_nan=False,
                indent=None,
                separators=(",", ":"),
            ).encode("ascii")


_NON_FINITE = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}


def _finite(content: Any) -> Any:
    """A copy of `content` where each non-finite float is its CLI spelling as a string; walked
    with a stack, not recursion, as an upstream schema may nest deeper than the recursion limit
    while `json.dumps` still encodes it."""

    def fixed(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return _NON_FINITE[repr(value)]
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, (list, tuple)):
            return list(value)
        return value

    root = fixed(content)
    stack = [root] if isinstance(root, (dict, list)) else []
    while stack:
        node = stack.pop()
        keys = node.keys() if isinstance(node, dict) else range(len(node))
        for k in keys:
            node[k] = fixed(node[k])
            if isinstance(node[k], (dict, list)):
                stack.append(node[k])
    return root


def problem(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """A refusal of the API: `{"code", "message"}` (§3.15), closed codes (BR-131); `extra` adds
    the keys a refusal names (the current impact of `impact_changed`, the reasons of a share)."""
    return JSONResponse({"code": code, "message": message, **extra}, status_code=status)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(slots=True)
class Ctx:
    """Request data; authorization refreshes `now` and `session` when admitting a mutation."""

    request: Request
    now: int
    address: str
    body: dict[str, Any]
    session: sessions.Session | None
    cookie: str

    @property
    def live(self) -> sessions.Session:
        """The route's session; routes declared `session=True` always have one."""
        if self.session is None:
            raise ApiError(401, "unauthenticated", "Sign in again.")
        return self.session

    @property
    def administrator_id(self) -> str:
        return self.live.administrator_id


Handler = Callable[[Ctx], Awaitable[Response]]


@dataclass(frozen=True, slots=True)
class ApiRoute:
    """One API route: `session=False` only for the ceremonies, which check `Origin` and their
    single-use challenge instead of a CSRF token (BR-205's named exemptions)."""

    method: str
    path: str
    handler: Handler
    session: bool = True
    fresh: bool = False
