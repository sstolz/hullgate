"""BR-131 (clarification, F-016 lot A) — the admin API's keys and refusal codes, its cookie and its
header names are closed vocabularies, pinned here as literals: the refusal codes by an AST walk of
every `problem(…)` and `ApiError(…)` call of `hullgate.admin.app` and `hullgate.admin.console`,
the routes by the application's
declarations, the names by their constants. A change to any of them is a change of this table."""

from __future__ import annotations

import ast
from pathlib import Path

from hullgate import config
from hullgate.admin import sessions
from hullgate.admin.app import API_PREFIX, build_admin_app
from hullgate.db import Database

ADMIN = Path(__file__).resolve().parents[2] / "src" / "hullgate" / "admin"
SOURCES = (ADMIN / "app.py", ADMIN / "console.py")

PINNED_CODES = {
    (400, "invalid_request"), (400, "enrolment_failed"), (400, "passkey_failed"),
    (401, "unauthenticated"), (401, "sign_in_failed"),
    (403, "csrf_refused"), (403, "forbidden"), (403, "fresh_auth_required"),
    (403, "fresh_auth_failed"),
    (404, "not_found"), (404, "enrolment_invalid"),
    (408, "request_timeout"),
    (409, "allowlist_self"), (409, "last_passkey"), (409, "passkey_exists"),
    (409, "refused"), (409, "impact_changed"), (409, "share_confirmation_required"),
    (413, "too_large"), (422, "invalid_request"), (429, "rate_limited"),
    (503, "store_unavailable"), (503, "key_unavailable"), (503, "provider_unavailable"),
    (503, "connection_unavailable"), (503, "policy_unavailable"),
}  # fmt: skip
PINNED_ROUTES = {
    "POST auth/options", "POST auth/assert", "POST enrol/options", "POST enrol/verify",
    "GET session", "POST session/signout", "POST session/fresh/options", "POST session/fresh",
    "GET passkeys", "POST passkeys/options", "POST passkeys", "DELETE passkeys/{credential_id}",
    "GET allowlist", "PUT allowlist", "GET meta",
    "GET changes", "POST changes/undo", "GET export", "POST import/preview", "POST import/apply",
    # slice 2
    "GET impacts",
    "GET nodes", "POST nodes", "POST nodes/rename", "POST nodes/move", "POST nodes/delete",
    "POST nodes/sensitive", "POST nodes/lock", "POST nodes/unlock",
    "GET settings", "PUT settings", "POST settings/unset",
    "GET tokens", "POST tokens", "POST tokens/revoke",
    "GET clients", "POST clients", "POST clients/remove", "PUT clients/dcr",
    "GET services", "POST services", "POST services/remove", "POST services/rediscover",
    "POST services/upstream",
    "GET applications", "POST applications", "POST applications/remove",
    "POST applications/reregister", "PUT applications/web-return",
    "GET connections", "POST connections", "POST connections/reauthorize",
    "POST connections/authorize", "POST connections/callback", "POST connections/renew",
    "POST connections/revoke", "POST connections/remove", "PUT connections/prefix",
    "POST connections/tools", "POST placements", "POST placements/remove",
    "GET audit", "POST audit/export",
    # slice 3: policy surfaces (F-015's verbs)
    "POST rights", "GET placements/policy", "PUT placements/mode", "POST placements/lists",
    "POST placements/lists/remove", "PUT placements/arguments", "POST placements/arguments/remove",
    "PUT placements/pin", "POST placements/tool-pins", "POST placements/tool-pins/remove",
    "GET tools/queue", "POST tools/classify", "POST tools/accept", "POST tools/diff",
    "POST tools/validate",
    "GET tools/catalogue", "GET organisation", "GET setup", "POST setup/dismiss",
}  # fmt: skip
STATUSES = {400, 401, 403, 404, 408, 409, 413, 421, 422, 429, 503}  # F-016 §3.15 (408: aa59a1a)


def _codes() -> set[tuple[int, str]]:
    found: set[tuple[int, str]] = set()
    nodes = [n for f in SOURCES for n in ast.walk(ast.parse(f.read_text(encoding="utf-8")))]
    for node in nodes:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ("problem", "ApiError") or len(node.args) < 2:
            continue
        status, code = node.args[0], node.args[1]
        if (
            isinstance(status, ast.Constant)
            and isinstance(status.value, int)
            and isinstance(code, ast.Constant)
        ):
            found.add((status.value, str(code.value)))
    return found


def test_br_131_admin_refusal_codes_are_the_pinned_table() -> None:
    codes = _codes()
    assert codes == PINNED_CODES
    assert {status for status, _ in codes} <= STATUSES
    assert all(code.isascii() and code == code.lower() for _, code in codes)


def test_br_131_admin_routes_are_the_pinned_table(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "x.db"))
    assert db.open()
    try:
        app = build_admin_app(db, config.admin_origin("http://localhost:14791"))
        assert {f"{s.method} {s.path}" for s in app.api_routes} == PINNED_ROUTES
    finally:
        db.close()
    assert API_PREFIX == "/admin/api/v1"


def test_br_131_admin_cookie_and_header_names() -> None:
    assert sessions.COOKIE == "hullgate_admin"
    assert sessions.CSRF_HEADER == "x-csrf-token"  # X-CSRF-Token, compared case-insensitively
    assert sessions.ACTIVITY_HEADER == "x-hullgate-activity"


def test_br_131_admin_code_walk_sees_a_mutant() -> None:
    """Positive fixture of the walk: a code it did not pin is seen."""
    tree = ast.parse('problem(418, "teapot", "x")')
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert isinstance(call.args[1], ast.Constant)
    assert (418, "teapot") not in PINNED_CODES


def test_br_131_history_import_refusals_are_pinned() -> None:
    sources = [ADMIN.parent / name for name in ("changes.py", "importing.py")]
    found = set()
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Refusal"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                status = next(
                    (
                        k.value.value
                        for k in node.keywords
                        if k.arg == "status" and isinstance(k.value, ast.Constant)
                    ),
                    409,
                )
                found.add((status, node.args[0].value))
    assert found == {
        (422, "import_invalid"),
        (422, "import_foreign_organisation"),
        (413, "import_too_large"),
        (409, "import_stale"),
        (409, "undo_stale"),
        (409, "refused"),
        (409, "share_confirmation_required"),
        (409, "impact_changed"),
        (422, "invalid_request"),
        (404, "not_found"),
    }
