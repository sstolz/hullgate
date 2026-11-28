# Request body deadlines (HULL-85)

Admin API writes (including the four sign-in/enrolment ceremonies) and public MCP requests
read their bodies with one monotonic deadline. The existing bounds remain 64 KiB for admin,
2 MiB for MCP, and 64 KiB for an unsupported MCP version's error envelope. A growing bytearray
holds the body; incoming chunks do not restart the timer. Anonymous MCP requests still fail
before any body is read.

The deadline is a fixed **30 seconds**, starting when the first body byte is awaited. It is
not configurable or inherited. Tests may inject a shorter deadline into the reader or patch
its module constant; nothing is stored in settings. The schema remains at version 12.

An incomplete body at the deadline receives HTTP 408 and `Connection: close`. Admin returns
`{"code":"request_timeout","message":"The request body did not arrive in time."}`.
MCP returns `{"error":"request_timeout"}`. No handler, SDK request or upstream call is dispatched.
MCP writes the existing `access.body_refused` event with reason `request_timeout`, the actor
identified at the header check, role/path, and `detail.timeout_seconds` and `detail.address`.
This does not claim the credential is still valid when the timeout occurs. Admin body timeouts,
like its existing malformed/oversized-body refusals, do not add an audit event; ceremony failure
counters do not count an assertion that never arrived. A disconnected admin client is handled
as an incomplete request (400, connection closed), without a mutation.

After a complete body, admin resolves the session again and checks freshness for routes that
require it, with current time, before calling the handler. For database-only mutations these
checks also run after acquiring the mutation's `BEGIN IMMEDIATE` lock. The server's domain
transactions use the shared connection through `Database.call`; the CLI has its own connection.

Operations that can call a provider first check the session and any required freshness in a
short admission transaction, releasing the lock before the exchange. The admitted policy
routes are `PUT placements/arguments`, `POST tools/validate`, `POST tools/diff`, and `POST rights`;
they can renew a token while obtaining the upstream listing.

Another check runs with current time inside the transaction that decides or starts
the mutation on these nine routes: `POST connections/remove`, `POST connections/revoke`,
`POST services` (declare), `POST services/rediscover`, `PUT placements/arguments`,
`POST tools/validate`, `POST connections/tools` when `purge_names` is true, `POST import/apply`,
and `POST changes/undo`. This covers the erasing transaction after a lease wait, service writes
after discovery, policy/pin/name writes after listing, renewal lease placement, and composite
import/undo writes after preparation. A session ended before that transaction refuses the
mutation: 401 `unauthenticated`, or 403 `fresh_auth_required` where freshness is required, with
nothing committed by that transaction. Discovery and tools/list are reads, not provider outcomes
that must be recorded.

Transactions recording a provider exchange already performed remain admitted: a rotated token
and renewal result, an issued grant or registration, and an upstream revocation audit are saved
even if the session ends during the exchange. The exemption includes any outer transaction
opened by a composite preparation runner to record that outcome. On these nine routes it ends
with the outcome record; a later mutation decision must check again. Other routes retain
operation-wide admission, including `POST tools/diff`, `POST rights`, and `POST connections/tools` without
`purge_names`. `POST import/preview` remains read-only with its existing admission behavior.
Domain checks such as lease ownership, generation and impact digests still apply.
No database write lock spans a body read or provider wait.

MCP repeats its complete authorization order before SDK dispatch, including current token
expiry, revocation, token placement/coverage, sensitive-role restrictions, audience and scope.
A token revoked during the upload receives the existing 401 `invalid_token`, its usual
`WWW-Authenticate` challenge, and `access.refused` with reason `revoked`. Other failures retain
their existing statuses, codes and audit reasons. A call already dispatched upstream is outside
this body-read boundary; this change does not cancel previously admitted upstream work.

## Closed vocabulary additions (BR-131)

| Family | Spelling | Meaning / contract |
|---|---|---|
| Admin refusal | HTTP 408 / `request_timeout` | Incomplete body at its deadline; close connection |
| MCP HTTP error | HTTP 408 / `request_timeout` | Incomplete body at its deadline; close connection |
| `access.body_refused` reason | `request_timeout` | Existing event reused; identified actor, role/path, timeout and address |
| Timeout audit detail key | `timeout_seconds` | Fixed total body-read budget, in seconds |

The existing `request_too_large` reason and `limit_bytes` detail remain unchanged for MCP 413.
Admin statuses/codes are pinned in `tests/unit/test_br_131_admin_api.py`, and the MCP timeout
vocabulary in
`tests/unit/test_hull_85_body.py` and the wire/audit assertions in
`tests/integration/test_hull_85_body_deadline.py`.
