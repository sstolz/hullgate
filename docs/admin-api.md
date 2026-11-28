# Admin API (F-016)

The JSON API of the admin console, on the admin listener (port 4791 in the container), under
`/admin/api/v1`. This page lists the paths and bodies settled at implementation (§3.15); the rules
are F-016's. **Slice 1** ships identity, sessions, the allow-list and `meta`; **slice 2** the
tree, settings, tokens, clients, library (services, applications, connections, placements) and
audit groups; the policy, history and import groups arrive with slices 3 to 5.

## Conventions

- **Origin.** The console's origin is `ADMIN_URL` (`http://localhost:14791` on the production server). A request
  whose `Host` differs gets 421 with no body (BR-206); `/healthz` excepted.
- **Allow-list.** When the organisation's list of CIDRs is non-empty, a client address outside it
  gets 403 on every `/admin` path, before any other check (BR-209). The client address is the socket
  peer, or the rightmost `X-Forwarded-For` entry outside `TRUSTED_PROXIES` when the peer is a
  declared proxy (BR-208).
- **Session.** Cookie `hullgate_admin`: `HttpOnly`, `SameSite=Strict`, `Path=/admin`, `Secure`
  under an `https` `ADMIN_URL`. 30 minutes idle, 8 hours absolute (BR-204). Only requests carrying
  `X-Hullgate-Activity: 1` extend the idle expiry; the console sets it on calls the operator
  caused, never on its background poll.
- **CSRF.** Every request other than `GET` carries `Origin` equal to the console's origin, and,
  outside the four ceremonies, `X-CSRF-Token` equal to the session's token (BR-205); else 403
  `csrf_refused`. The token is returned at sign-in and by `GET session`.
- **Fresh re-authentication.** Adding or deleting a passkey and changing the allow-list need an
  assertion of at most 5 minutes (`POST session/fresh`); else 403 `fresh_auth_required` (BR-210).
- **Bodies.** JSON objects, 64 KiB at most (413 `too_large`), read within a fixed 30 seconds
  from awaiting the first body byte (F-016 §3.15); expiry returns 408 `request_timeout` and closes
  the connection. Session and freshness are rechecked after the body and at mutation admission
  with current time. An admitted provider operation records its outcome even if the session
  ends during the exchange. See [request body deadlines](request-bodies.md).
  Every response carries
  `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`
  and the console's CSP. No CORS header is ever sent.
- **Refusals.** `{"code": "<closed code>", "message": "<English text>"}`; the console shows the
  message as it is (BR-211). Codes are a closed vocabulary (BR-131), pinned by
  `tests/unit/test_br_131_admin_api.py`.
- **Audit.** Writes are audited with actor `admin:<administrator id>`; ceremony refusals with
  actor `anonymous` and the client address in the detail (BR-066 v5).

| Status | Codes |
|---|---|
| 400 | `invalid_request`, `enrolment_failed`, `passkey_failed` |
| 401 | `unauthenticated`, `sign_in_failed` |
| 403 | `csrf_refused`, `forbidden`, `fresh_auth_required`, `fresh_auth_failed` |
| 404 | `not_found`, `enrolment_invalid` |
| 408 | `request_timeout` |
| 409 | `allowlist_self`, `last_passkey`, `passkey_exists` |
| 413 | `too_large` |
| 421 | (no body) |
| 422 | `invalid_request` |
| 429 | `rate_limited` |
| 503 | `store_unavailable` |

## WebAuthn

Options travel as the JSON form of `PublicKeyCredentialCreationOptions` and
`PublicKeyCredentialRequestOptions`, binary values in base64url; credentials come back in the JSON
form of `PublicKeyCredential` (`id`, `rawId`, `type`, `response.{clientDataJSON,
attestationObject | authenticatorData, signature, userHandle}`). Discoverable credentials, user
verification required, attestation `none`, ES256, EdDSA or RS256. A challenge is single-use and
lives 5 minutes; a client address holds at most 5 live ones (BR-202, BR-207).

## Routes of lot A

### Ceremonies (no session)

| Route | Body | Answer |
|---|---|---|
| `POST auth/options` | `{}` | `{"publicKey": <request options>}`; 429 past 5 live challenges or 5 failures in 15 minutes |
| `POST auth/assert` | `{"credential": <assertion>}` | 200 `{"administrator", "csrf_token", "expires_at"}` and the cookie; 401 `sign_in_failed`, one message for every failure; 429 `rate_limited` |
| `POST enrol/options` | `{"link": "<token of the enrolment link>"}` | `{"email", "name", "publicKey": <creation options>}`; 404 `enrolment_invalid` for a used, expired, replaced or unknown link |
| `POST enrol/verify` | `{"link", "label", "credential": <attestation>}` | 200 as `auth/assert`: the account turns `active`, the link is consumed, the session opens |

The enrolment link is `${ADMIN_URL}/admin/enrol/<token>`: the console reads the token from the
path and takes it out of the address bar before its first API call (BR-203).

### Session

| Route | Body | Answer |
|---|---|---|
| `GET session` | — | `{"administrator", "csrf_token", "expires_at", "fresh_until"}` |
| `POST session/signout` | `{}` | `{"signed_out": true}`; the session row is deleted, the cookie cleared |
| `POST session/fresh/options` | `{}` | `{"publicKey": <request options>}` restricted to the account's passkeys |
| `POST session/fresh` | `{"credential": <assertion>}` | `{"fresh_until"}`; 403 `fresh_auth_failed`; value and CSRF token unchanged |

### Passkeys

| Route | Body | Answer |
|---|---|---|
| `GET passkeys` | — | `{"passkeys": [{"id", "label", "created_at", "last_used_at"}]}` |
| `POST passkeys/options` (fresh) | `{}` | `{"publicKey": <creation options>}`, the account's passkeys excluded |
| `POST passkeys` (fresh) | `{"label", "credential": <attestation>}` | 201 `{"passkey"}`; 400 `passkey_failed`; 409 `passkey_exists` |
| `DELETE passkeys/{id}` (fresh) | — | `{"deleted", "sessions_ended"}`: the account's other sessions end; 409 `last_passkey`; 404 |

### Allow-list

| Route | Body | Answer |
|---|---|---|
| `GET allowlist` | — | `{"cidrs": [...], "client_address"}` |
| `PUT allowlist` (fresh) | `{"cidrs": ["203.0.113.0/24", ...]}` | `{"cidrs", "client_address"}`, canonical and sorted; 409 `allowlist_self` naming your address; 422 for a value that is not a CIDR |

### Meta

| Route | Body | Answer |
|---|---|---|
| `GET meta` | — | `{"version", "public_url", "admin_url", "rp_id", "readiness": {"ok", "reason"}, "features": {"web_return": true}, "web_redirect_uri"}` |

`readiness` is the gateway's `/readyz` predicate (store, schema, signing key), which the admin
listener does not expose otherwise: the console's banner reads it (§3.4).

## Routes of slice 2

Every write below calls the domain operation of the CLI verb named beside it and returns its
refusal as it is (BR-211): 409 `refused` with the CLI's text, unless a more precise code is named.
Paths of the tree and connection names travel in the body (writes) or the query string (reads),
never in a path segment. A secret entering (a provided token, an application secret) travels in a
`POST` body only and is never echoed back (BR-086, BR-213); it passes the CLI reader's checks
(one final newline removed; refused empty, blank, on several lines or over 64 KiB). A body whose
text holds a lone surrogate is 400 `invalid_request`.

### Impacts (BR-214)

An act that deletes, revokes or changes a path or a sensitivity first reads its impact, then
quotes the impact's `digest` in its write as `"impact": "<digest>"`. The server recomputes the
impact when the write arrives; a different or missing digest gets 409 `impact_changed`, with the
current impact in the body (`{"code", "message", "impact"}`), and nothing is done. Connection
revoke and remove, which may wait for a renewal in progress, and application reregister, which
calls the provider first, compare the digest in the transaction that erases or revokes.

| Route | Query | Answer |
|---|---|---|
| `GET impacts` | `act` and the act's parameters, below | `{"act", "target", "impact": {…}, "shares": [<share signal>], "digest"}` |

| `act` | Parameters | `impact` numbers | Write |
|---|---|---|---|
| `tree.delete` | `path` | `descendants`, `tokens_revoked`, `urls`, `placements_removed` | `POST nodes/delete` |
| `tree.rename` | `path`, `segment` (optional) | `new_path`, `urls`, `lineages_revoked` | `POST nodes/rename` |
| `tree.move` | `path`, `parent` | `new_path`, `urls`, `lineages_revoked`, `tokens_lost`, `tokens_gained` | `POST nodes/move` |
| `tree.sensitive` | `path`, `sensitive` (`true`/`false`) | `reopened_tokens`, `env_tokens`, `long_role_tokens` | `POST nodes/sensitive` |
| `connection.revoke` | `name` | `placements`, `roles` | `POST connections/revoke` |
| `connection.remove` | `name` | `placements_removed`, `roles` | `POST connections/remove` |
| `client.remove` | `client_id` | `lineages_revoked` | `POST clients/remove` |
| `application.remove` | `id` | `connections` | `POST applications/remove` |
| `service.remove` | `id` | `applications`, `connections` | `POST services/remove` |
| `application.reregister` | `id` | `reauth_required` (the connections moved to reauthentication required) | `POST applications/reregister` |

A share signal is `{"connection", "reasons": ["other_client" | "sensitivity"], "roles": [{"role",
"sensitive"}]}`: a connection the act makes shared (BR-079). The console's dialog shows these
numbers as they are, and says that the neighbours of a shared connection share its call limit.

### Tree and settings

A node is `{"id", "path", "parent", "level", "segment", "name", "sensitive", "marked",
"locked_by", "locked_here", "url"}`: `sensitive` is the effective sensitivity (BR-047), `marked`
the flag of an environment, `locked_by` the path of the nearest locked node or `null`, `url` the
access point of a role (the server's, BR-211) or `null`.

| Route | CLI | Body or query | Answer |
|---|---|---|---|
| `GET nodes` | `tree show` | — | `{"nodes": [<node>, …]}`, depth first from the organisation |
| `POST nodes` | `tree add` | `{"parent", "level", "name", "segment"?}` | 201 `{"created": [<node>, …]}` (an environment also creates its `principal` role) |
| `POST nodes/rename` | `tree rename` | `{"path", "name", "segment"?, "impact"}` | `{"old_path", "new_path", "lineages_revoked"}` |
| `POST nodes/move` | `tree move` | `{"path", "parent", "impact"}` | `{"path", "new_path"}` |
| `POST nodes/delete` | `tree delete` | `{"path", "impact"}` | `{"path", "descendants"}` |
| `POST nodes/sensitive` | `tree sensitive` | `{"path", "sensitive", "impact"}` | `{"path", "sensitive"}` |
| `POST nodes/lock` | `tree lock` | `{"path"}` | `{"path", "locked_by"}` |
| `POST nodes/unlock` | `tree unlock` | `{"path"}` | `{"path", "locked_by"}` |
| `GET settings` | `settings get` | `?path=` | `{"path", "settings": [{"key", "value", "origin", "here"}]}` |
| `PUT settings` | `settings set` | `{"path", "key", "value"}` | `{"key", "value", "origin"}` |
| `POST settings/unset` | `settings unset` | `{"path", "key"}` | `{"key", "value", "origin"}` |

### Tokens and OAuth clients

| Route | CLI | Body or query | Answer |
|---|---|---|---|
| `GET tokens` | `token list` | `?path=&client=` (both optional) | `{"tokens": [{"id", "type", "holder", "path", "issued_at", "expires_at", "state"}]}`; `type` is `operator` or `oauth` (a lineage) |
| `POST tokens` | `token create` | `{"path", "label", "duration"?, "confirm"?}` | 201 `{"id", "value", "path", "label", "expires_at", "capped", "uncovered_sensitive"}`: the **only** response carrying a secret, once (BR-053 v3, BR-213) |
| `POST tokens/revoke` | `token revoke` | `{"id"}` | `{"id", "type", "already"}` |
| `GET clients` | `client list` | — | `{"clients": [{"client_id", "kind", "path", "redirect_uris", "name", "state"}], "dcr"}` |
| `POST clients` | `client add` | `{"client_id", "path", "redirect_uris": […], "name"?}` | 201 `{"client_id", "path", "redirect_uris"}` (BR-061 v2) |
| `POST clients/remove` | `client remove` | `{"client_id", "impact"}` | `{"client_id", "lineages_revoked"}` |
| `PUT clients/dcr` | `client dcr` | `{"on": true or false}` | `{"dcr"}` |

### Library: services, applications, connections, placements

| Route | CLI | Body or query | Answer |
|---|---|---|---|
| `GET services` | `service list` | — | `{"services": [{"id", "model", "url", "scopes", "internal_host", "endpoints", "resource", "upstream"}]}` |
| `POST services` | `service declare` | `{"id", "server", "model"?, "scopes"?, "internal_host"?}` | 201 `{"service", "addresses"}` |
| `POST services/remove` | `service remove` | `{"id", "impact"}` | `{"removed"}` |
| `POST services/rediscover` | `service rediscover` | `{"id", "confirm"}` | `{"before", "after"}` |
| `POST services/upstream` | `service upstream` | `{"id", "type", "mcp_url", "mcp_internal_host"?, "confirm"?}` or `{"id", "remove": true, "confirm"}` | `{"service", "changed"}` |
| `GET applications` | `application list` | — | `{"applications": [{"id", "service", "mode", "client_id", "redirect_uri", "secret", "secret_expires_at", "method", "web_return"}], "web_redirect_uri"}` |
| `POST applications` | `application declare` | `{"id", "service", "mode": "declared", "client_id", "secret"?}` or `{"id", "service", "mode": "dynamic"}` | 201 `{"application"}` |
| `POST applications/remove` | `application remove` | `{"id", "impact"}` | `{"removed"}` |
| `POST applications/reregister` | `application reregister` | `{"id", "confirm", "impact"}` | `{"id", "client_id", "upstream"}` |
| `PUT applications/web-return` | — (API only, F-016 §9) | `{"id", "registered": true or false}` | `{"id", "web_return"}` |
| `GET connections` | `connection list`, `connection status` | — | `{"connections": [<view>], "key"}`: a view is `connection status --json`'s object plus `"web_return"` |
| `POST connections` | `connection add` | `{"name", "service", "prefix", "application"}` or `{"name", "service", "prefix", "provided": {"value", "account"?}}` | 201 `{"name", "type", "state", "authorization_url", "redirect"}`; `redirect` is `web` (BR-216), `paste` (F-006) or `null` |
| `POST connections/reauthorize` | `connection reauthorize` | `{"name"}` or `{"name", "provided": {"value", "account"?}}` | `{"name", "state", "authorization_url", "redirect"}` |
| `POST connections/authorize` | `connection authorize` | `{"name", "callback": "<return URL pasted>", "confirm_account"?}` | `{"name", "account", "scopes", "expires_at"}` |
| `POST connections/callback` | `connection authorize` | `{"state", "code"?, "error"?, "confirm_account"?}` | as `connections/authorize` (BR-216) |
| `POST connections/renew` | `connection renew` | `{"name"}` | `{"name", "generation", "expires_at"}` |
| `POST connections/revoke` | `connection revoke` | `{"name", "impact"}` | `{"name", "upstream"}` |
| `POST connections/remove` | `connection remove` | `{"name", "impact"}` | `{"name", "placements", "upstream"}` |
| `PUT connections/prefix` | `connection prefix` | `{"name", "prefix"}` | `{"name", "prefix"}` |
| `POST connections/tools` | `connection tools` | `{"name", "role", "purge_names"?, "confirm"?}` | `{"connection", "role", "version", "tools": [{"name", "upstream_name"}], "invalid_names", "purged_names"}` |
| `POST placements` | `connection place` | `{"connection", "role", "confirm_share"?}` | 201 `{"connection", "role", "sensitive", "confirmed"}`; 409 `share_confirmation_required` with `"reasons"` |
| `POST placements/remove` | `connection unplace` | `{"connection", "role"}` | `{"connection", "role"}` |

**Web return (BR-216).** An application whose web return is registered at its provider
(`PUT applications/web-return`, set by the operator after adding `web_redirect_uri` there) opens
its authorisations with `redirect_uri` `${ADMIN_URL}/admin/oauth/callback`; the provider sends the
browser back to that console route, which posts `state` and `code` to `connections/callback` under
the session and clears the address bar. The `redirect_uri` chosen at opening is kept with the
pending authorisation and sent again at the exchange (BR-082 v3). Without it the console shows the
authorisation URL and a field for the return URL (`connections/authorize`).

**Clients see changes at their next `tools/list` (BR-215).** The answers of the tree, placement
and connection writes, and of application reregister, carry `"tools_notice": true`: the gateway pushes no
`notifications/tools/list_changed`, and connected clients see the new list at their next
`tools/list`. No change needs a restart.

### Audit (BR-218)

| Route | Body or query | Answer |
|---|---|---|
| `GET audit` | `?since=&until=&actor=&path=&action=&result=&before=&limit=` | `{"entries": [<entry>], "next"}`, newest first; `before` is a `seq` (paging), `limit` 1 to 500 (default 100); `path` and `action` are prefixes, `since` and `until` UTC ISO 8601 |
| `POST audit/export` | the same filter as an object | `application/x-ndjson`, one entry per line, newest first, at most 100 000; the export is audited (`audit.export`, with its filter) |

An entry is `{"seq", "at", "actor", "node_id", "path", "action", "result", "reason", "before",
"after", "detail"}` as `audit tail --json` gives it. No route writes the audit other than as the
trace of an act.

### Refusal codes added by slice 2

| Status | Codes |
|---|---|
| 409 | `refused` (a domain refusal, the CLI's text), `impact_changed`, `share_confirmation_required` |
| 503 | `key_unavailable`, `provider_unavailable`, `connection_unavailable`, `policy_unavailable` |

## Routes of slice 3

The policy surfaces of F-015 (BR-217): every write calls the domain operation of the CLI verb
named beside it, as the administrator, and returns its refusal as it is (409 `refused`, the CLI's
text); 503 `policy_unavailable` when the policy state cannot be read (F-015 BR-185). A placement is
named by `connection` (its name) and `role` (its path). The answers of the policy writes and of the
catalogue writes carry `"tools_notice": true` (BR-215).

### Rights preview (BR-217)

| Route | CLI | Body | Answer |
|---|---|---|---|
| `POST rights` | `policy preview` | `{"role", "token"?}`; an empty `token` is an unknown token (409) | `{"role", "token", "exposed", "tools", "unavailable", "policy_errors"}` |

`tools` are the rows of `policy preview --json` (exposed first: `public_name`, `connection`,
`upstream_name`, `class`, `source`, `exposed`, `reasons`, `opened`, `grouped`, `argument_rule`,
`pinned`, `sensitive_via`); `exposed` is the list of exposed public names, the names `tools/list`
returns under a token of that role. An entry of `unavailable` (`{"connection", "code"}`) is a
connection whose tools are **unknown**, never absent. `policy_errors` names the connections whose
evaluation failed (their tools are hidden); the CLI exits 1 on them, the route answers 200.

With `token` (an identifier from `GET tokens`, never a value), `token` in the answer is `{"token",
"type", "role", "reaches", "step", "reason"}`: steps 4 (expiry) to 7 of the authorization order
(BR-055, BR-051) played for that token on that role, as the access point would, without its value
and writing nothing (no last use, no audit). `reason` is the refusal reason of the step that
answered (`invalid_token` for an expired token, `revoked`, `node_deleted`, `audience_mismatch`,
`out_of_scope`, `sensitive_requires_role_token`, `insufficient_scope`), `null` when it reaches. The
role's preview follows in either case: in v1 a token narrows no tool (F-015 §Out of Scope).

It is a `POST` although it changes no policy: its listing is fresh and audited, records the
catalogue and may suspend a pinned tool (F-015 BR-173, BR-181), which no `GET` may do (BR-234).

### Placement policy

| Route | CLI | Body or query | Answer |
|---|---|---|---|
| `GET placements/policy` | `policy show` | `?connection=&role=` (one placement), or `?path=` (every placement on a role under `path`, default `/`) | `{"placements": [<policy>]}`; a policy is `policy show --json`'s object: `{"connection", "role", "mode", "mode_origin", "pin", "sensitive", "open", "deny", "allowlist", "pinned_tools", "arguments"}`, plus `"names"` (below) |
| `PUT placements/mode` | `policy mode` | `{"connection", "role", "mode": "read" or "write" or "destructive" or "inherit"}` | the policy object |
| `POST placements/lists` | `policy add` | `{"connection", "role", "list": "open" or "deny" or "allowlist", "tools": [...]}` or `{…, "list": "allowlist", "class": "read"}` | the policy object plus `"added"` |
| `POST placements/lists/remove` | `policy remove` | `{"connection", "role", "list", "tools": [...]}` | the policy object plus `"removed"` |
| `PUT placements/arguments` | `policy arg` | `{"connection", "role", "tool", "argument", "values": [...]}` | the policy object; the upstream is listed at entry, unavailable → 409 `refused`, no rule recorded |
| `POST placements/arguments/remove` | `policy arg-remove` | `{"connection", "role", "tool"}` | the policy object |
| `PUT placements/pin` | `policy pin` | `{"connection", "role", "pin": "on" or "off" or "inherit"}` | the policy object |
| `POST placements/tool-pins` | `policy pin-tool` | `{"connection", "role", "tool"}` | the policy object plus `"already"` |
| `POST placements/tool-pins/remove` | `policy unpin-tool` | `{"connection", "role", "tool"}` | the policy object |

### Tool catalogue

| Route | CLI | Body or query | Answer |
|---|---|---|---|
| `GET tools/queue` | `tool queue` | `?service=` | `{"queue": [{"service", "name", "class", "source", "proposal", "removed"}]}` |
| `POST tools/classify` | `tool classify` | `{"service", "tool", "class": "read" or "write" or "destructive" or "clear"}` | `{"service", "tool", "class", "source"}` |
| `POST tools/accept` | `tool accept` | `{"service", "tool"}` | `{"service", "tool", "class", "source"}` |
| `POST tools/diff` | `tool diff` | `{"path"}` | `{"node", "changes": [{"connection", "role", "tool", "state", "baseline_sha256", "current_sha256", "fields": [{"field", "old", "new"}]}]}` |
| `POST tools/validate` | `tool validate` | `{"path", "connection", "tool"?}`; an empty `tool` is a name, refused as `--tool ""` is | `{"validated": [{"connection", "role", "tool", "before_sha256", "after_sha256"}]}` |

`tools/diff` is a `POST` for the reason of `rights`. The upstream's text (`tool`, `old`, `new`)
is data: the console shows it as text, never as markup. A lone surrogate in it (`"\ud800"` in the
upstream's JSON) is written ASCII-escaped, as the CLI's `--json` does, on every route of this
section and of the rights preview. A non-finite number in it (`NaN`, `Infinity`, `-Infinity`,
which an upstream's JSON may carry and JSON does not allow) is written as the string of the token
the CLI's `--json` prints, `"NaN"`, `"Infinity"` or `"-Infinity"`, so the answer stays JSON. Any
other answer is unchanged.

An empty `connection`, `role` or `service` in a query is a name, refused as the CLI refuses it
(409), never "every placement" or "every service"; an absent one keeps its meaning.

The lists of a policy object are labels of the upstream names (control characters replaced, 256
characters at most). `"names"` gives the names behind them: `{"open", "deny", "allowlist",
"pinned_tools"}` as lists in the labels' order, `"arguments"` as `{label: name}`. A removal names
the tool by that name; `policy show --json` does not carry `names`.

## Host CLI (no route)

`hullgate admin bootstrap <email> [--name N]` (BR-201), `hullgate admin list [--json]`,
`hullgate admin recover <email>` (BR-228), `hullgate admin attempts clear` (BR-207),
`hullgate admin allowlist clear` (BR-235). No route
creates, lists or removes an administrator, or issues an enrolment link.

## Routes of slice 4

All these admin routes retain the session, Host, allow-list and CSRF checks, no-store responses,
and the ordinary domain operation refusals. No recovery route exists.

| Method and route | Response or act |
|---|---|
| `GET organisation` | Organisation name; current administrator, passkey count and last successful sign-in from the audit; configured public/admin origins and RP id; applications and both return URIs; expiry threshold. No secrets. |
| `GET setup` | Five `{id, done, url}` steps computed from stored existence; `dismissed`, `show`, return URIs and each declared service's kind and scopes. No write or upstream request. |
| `POST setup/dismiss` | Sets the instance-wide dismissal only; idempotent. BR-229's API-only act. |
| `GET tools/catalogue?service=…&limit=…&after_service=…&after_name=…` | `{catalogue: […], next}` with the same row shape as `tools/queue`, for classified entries outside the queue, including removed tools. Ordered by service/name, `limit` 1–500 (default 100); `next` is the next query's `{after_service, after_name}` or null. Supply both cursor fields or neither. An empty explicit service is refused. |

Organisation uses the existing `nodes/rename` (including impact digest), `settings`, `allowlist`
and passkey routes. The catalogue uses the existing `tools/classify` for reclassification and
`class: "clear"`; these invoke the CLI's domain operation, with its lock refusals.

`PUT settings` / `settings set / connection_expiry_days <days>` accepts whole days 1–90, default
7. Only the organisation may set this global value; changes write `settings.changed`. Reads take
immediate effect. `GET connections` and the CLI JSON view add `expiring` and `access_ends_at`.
The server reports active connections whose known non-renewable end is at or before now plus the
threshold (including already elapsed ends); no persisted warning state. Provided tokens use their
known expiry, OAuth uses the refresh-token expiry, never its renewable access-token expiry.
Unknown ends are not guessed.

For a provided token, `POST connections` and `connections/reauthorize` accept optional
`provided.expires_at` as an ISO 8601 timestamp with a timezone. The CLI equivalents accept
`--expires-at`; both use the same parser and domain act. Replacing a token without that field
clears its former expiry. This is operator-supplied metadata, not decoded from an opaque token.
OAuth `refresh_token_expires_in` is a non-negative integer duration whose resulting UTC timestamp
must be representable, stored as an absolute timestamp
on authorization and renewal. An unchanged refresh token retains its known end if no new duration
is supplied; a rotated token without a duration has an unknown end.

`POST tools/diff` adds optional `type_marks` to a changed field when a string and a non-finite
number occur at the same location. Each mark has `path` (JSON Pointer within that field; empty for
the field itself), `old` and `new` (`string` or `number`). The values remain JSON-safe strings
(`NaN`, `Infinity`, `-Infinity`) as before; SCR-10 displays the type mark so equal spellings do not
hide the change. CLI diff JSON is unchanged.

The public barrier continues to use the role's existing `/consent` route, not an admin route.
POST `decision=passkey-options` returns WebAuthn options; POST `decision=passkey` carries the JSON
assertion in form field `credential`. Both require the consent cookie/hidden-field pair and exact
`PUBLIC_URL` Origin. Challenges are single-use, five-minute, address-bound and bound to that
pending consent, with a separate `barrier` purpose; verification uses the configured admin RP id
and public origin. It never issues an admin session. BR-207's per-address limits apply; the TOTP
window is untouched. The barrier script is constant and allowed by its CSP SHA-256 hash, with
same-origin fetch only. Code issuance records `factor` as `totp`, `passkey` or `host_approval`.

## Routes of slice 5: history and import

All routes use the existing administrator session, Host and allow-list checks; writes also
require Origin and CSRF. The CLI and API call `changes` and `importing` domain operations.
No preview or export route writes domain state or an audit entry.

| Method and path (under `/admin/api/v1/`) | Input | Result |
|---|---|---|
| `GET changes` | Query `seq`, positive SQLite integer | Decoded audit entry, `undoable`, `undo_reason`; for reversible entries, `current`, `expected`, `stale`, and a sensitivity `impact` with its digest |
| `POST changes/undo` | `seq`, optional `impact` digest and `confirm_share` (default false) | New audit `seq`, plus the normal clients-next-list notice |
| `GET export` | None | The current `hullgate export` document, `application/yaml`, download `hullgate.yaml` |
| `POST import/preview` | `document`: YAML text | `digest`, `changes` (kind/action/path/before/after/impact), `reauthorize` count, `apply_deletions: false` |
| `POST import/apply` | The same `document`, `digest`, optional `apply_deletions` and `confirm_share` (both default false) | `import_id`, applied change count and the clients-next-list notice |

Audit rows also carry `undoable` and `undo_reason`. The closed reversible list includes the
expiry threshold's `settings.changed` event, node lock/unlock's setting events, placements,
prefixes and every F-015 placement policy event. `change.undo` is a reversible envelope:
its `detail.undo_action` identifies the original class and `detail.undo_of` identifies the
entry reversed. Ordinary domain audit events are retained, linked to the same entry.
Entries bearing `import_id` always report `part_of_import`. Older placement/policy entries
without enough data to reconstruct the old state report `history_snapshot_unavailable`;
historical audit rows are never rewritten.

A changed target returns 409 `undo_stale` with `current` and `expected`. Sensitivity undo
requires the impact digest from the read; 409 `impact_changed` asks for a new read. It
restores the sensitivity flag, never old tokens. A newly recreated share returns 409
`share_confirmation_required` until explicitly confirmed. Ordinary domain refusals still
apply. Refused undo/apply operations append a refusal after rolling back their transaction.

Import accepts the current export format, **5**. YAML must have UTF-8 encodable strings,
unique text keys, no tags, anchors or aliases, at most 32 collection levels and 50,000 parser
events. Unknown keys at any level are refused. The decoded YAML is capped at **1 MiB**;
the two JSON transport routes allow up to `6 * 1 MiB + 4096` bytes so JSON escaping does not
reduce that limit. Size refusals are 413 `import_too_large`; malformed documents are 422
`import_invalid`, with `location`. A populated tree refuses a different organisation id
with 422 `import_foreign_organisation`; an empty tree adopts it transactionally.

The digest binds the exact document bytes, relevant configuration and credential state,
and the complete plan including impacts. A changed document, state or impact returns 409
`import_stale`. Preview uses a read snapshot and makes no upstream calls. Apply uses one
write transaction and names the failing path; successful entries share `detail.import_id`.
Provider revocation after a connection removal is deferred until the transaction commits.
Missing nodes, placements and connections are deleted only with `apply_deletions: true`.
Missing setting overrides on retained nodes return to inheritance without that flag.

New connections await authorization with no tokens; new applications hold no secret. The
export carries no pinned definitions or schemas. Restored argument policies on connections
without credentials therefore get structural/catalogue checks and pin on the first authorized
listing, as an export restore requires; changes on authorized connections use the ordinary
fresh-schema validation. Existing service/application replacement obeys ordinary removal
refusals when referenced. An existing connection's service/application cannot be reassigned.

Equivalent CLI commands (JSON output, refusal exit code 2):

```sh
hullgate export > current.yaml
hullgate import --preview proposed.yaml
hullgate import --apply DIGEST proposed.yaml
hullgate import --apply DIGEST proposed.yaml --apply-deletions --confirm-share
hullgate undo SEQ --preview
hullgate undo SEQ --impact DIGEST --confirm-share
```

SCR-11 reads the server's plan and renders its confirmation, share refusal and stale
comparison. SCR-12 renders the server's diff and impacts, defaults both confirmations off,
invalidates a preview when its text changes, and offers the current export beside Apply.
