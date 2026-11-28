# ContextForge trial against the isolation acceptance run (F-001, HULL-2)

**Status: final report (SCR-03.final)** — 16 tests run, verdict: **no-go**.

| Field | Value |
|-------|-------|
| Product | ContextForge (IBM/mcp-context-forge), version 1.1.0 |
| Image | `ghcr.io/ibm/mcp-context-forge@sha256:7eb21fb9a8070df59435c03c76c40d8c71230c55ba89f884f2482d2cc81cff1c` (published on 20 October 2026) |
| Other images | `postgres@sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b`, `python@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2` (upstream) |
| Acceptance run date | 2026-10-27 |
| Bench commit | `3f0a593` (policy, configuration and runner committed before the final acceptance run, unchanged during it: BR-011). The first set of results, obtained at commit `1e36d9a`, is replaced: T06, T14 and T15 were fixed after the review (round 1) and the full acceptance run was replayed |
| Replay | `poc/isolation-bench/bench.sh` (one command, BR-005) |
| Evidence | `poc/isolation-bench/evidence/results.json` (results per test, upstream log entries, configuration fingerprints) |
| Source of the acceptance run | OpenAI technical review of 26 October 2026, section 6 (16 tests), without transposition (BR-001) |

Counts: 7 « réussi », 7 « échoué », 2 « non jouable », 0 « non joué ». (Round 2, `apply-findings`: T06 goes from « réussi » to « échoué », E-07; T15 replayed on both protocol generations.)

## Results

| Id | Test | Result | Gap | Evidence |
|----|------|--------|-----|----------|
| T01 | Access token A on A, authorized action | réussi | — | whoami via A: account witness-1; 5 upstream entries, all witness-1 (positive control). `results.json#T01` |
| T02 | Access token A on B | échoué | E-01 | Refusal and empty upstream log, but HTTP 403 `Access denied` (without `WWW-Authenticate`) instead of 401 `invalid_token`. `results.json#T02` |
| T03 | Refresh token A with resource B | non jouable | E-02 | No authorization server for MCP clients: `/.well-known/oauth-authorization-server`, `/.well-known/openid-configuration`, `/token`, `/authorize`, `/oauth/token`, `/register`, `/oauth/register` all answer 404; the OpenAPI API (300 paths) offers only `/oauth/*` on the upstream side (OAuth client); no refresh token issued to clients. `results.json#T03` |
| T04 | Refresh A without resource or with A+B | non jouable | E-02 | Same evidence as T03: no renewal function for an MCP client. `results.json#T04` |
| T05 | Token with audiences A and B | échoué | E-03 | Valid token (signature, `iss`, `exp`, `jti`) with `aud = [mcpgateway-api, ressource B]` accepted: call executed, 5 upstream entries. `results.json#T05` |
| T06 | Invalid signature, issuer or expiration | échoué | E-07 | Invalid signature, `iss` and `exp`: 401, 0 upstream entries. ID token (`token_use: id`, `jti` kept, signed with the bench key) in place of an access token: accepted, call executed, 5 upstream entries. Control: a token of unchanged type stripped of its `jti` alone is refused with 401; the refusal of the previous version of the test therefore came from the `jti` removed along with the type, not from the type. `results.json#T06` |
| T07 | Call from A with the connection_id of B | échoué | E-04 | Tool of B called from A: refused; `connection_id` as argument, header or query parameter: ignored, call executed on the account of A (15 upstream entries, all witness-1). `results.json#T07` |
| T08 | U1 supplies a connection identifier of U2 | échoué | E-04 | `connection_id` argument of U2 ignored, call executed on witness-1 (5 entries); tool of U2 refused; reading of the connection of U2 by U1: 403. `results.json#T08` |
| T09 | Hidden tool called directly | réussi | — | Absent from `tools/list`, direct call `Unknown tool`, call through `/rpc` `-32601`; no generic tool listed; 0 upstream entries. `results.json#T09` |
| T10 | Account added after profile creation | réussi | — | Passed by construction (see Limits): tool list of profile A identical before/after adding a connection to the other account; next call on witness-1 only. `results.json#T10` |
| T11 | Two simultaneous renewals | échoué | E-05 | Two simultaneous calls with a provider token to renew: one renewal issued, one `invalid_grant`; both calls fail, and so does the next call ("Please authorize conn-1 first") until the administrator authorizes again. `results.json#T11` |
| T12 | Revocation or deletion of the profile | réussi | — | Token revocation: cut after 30.8 s (bound 60 s) on a persistent TCP connection, without MCP session or SSE stream (see Limits); profile deletion: 0.1 s; 0 upstream entries after the cut. `results.json#T12` |
| T13 | Vault / revocation check unavailable | réussi | — | Database stopped: 401 at 0, 5 and 15 s, 0 upstream entries; service restored in 0.4 s after restart. `results.json#T13` |
| T14 | OAuth discovery on A and B | échoué | E-06 | Without token: 401 `Bearer` without `resource_metadata`; protected resource metadata of A and B: 404 (team profiles); upstream log read: 0 entries. `results.json#T14` |
| T15 | Two generations of MCP clients | réussi | — | Generation with sessions (`initialize` handshake): 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 negotiated, authorized call on witness-1, 401 without token. Generation 2026-07-28 (no `initialize`, `_meta` envelope per request, `Mcp-Method` and `Mcp-Name` headers): `server/discover` announces `supportedVersions: [2026-07-28]`, `tools/list` and tool call on witness-1, 401 without token, no `Mcp-Session-Id` issued. Versions accepted by the gateway: see § Protocol versions. `results.json#T15` |
| T16 | Recovery after provider error | réussi | — | Provider error after a write: error returned to the client, 1 single upstream call, 1 single write, no entry of another account. `results.json#T16` |

## Gaps

### E-01 · Refusal of a token A on B with 403, not 401 `invalid_token` (T02)
- Tests: T02
- Cause: the `server_id` scope of the token is checked after authentication and returns 403 `Access denied` without a `WWW-Authenticate` header. Isolation holds (no upstream call, including through `/rpc`), only the refusal code differs from the acceptance run.
- Fix: overlay (response rule of the front proxy: 403 on `/servers/*/mcp` returned as 401 with `WWW-Authenticate: Bearer error="invalid_token"`).
- Cost (estimated): 0.5 days.

### E-02 · No authorization server for MCP clients (T03, T04)
- Tests: T03, T04 (non jouable)
- Cause: ContextForge is an OAuth client toward the upstream (`/oauth/authorize/{gateway_id}`, `/oauth/callback`), not an authorization server for its clients. The "access tokens" are API JWTs issued to an authenticated user (`/v1/tokens`), signed by a shared key; no `/authorize`, `/token`, `/register` endpoint, no authorization server metadata, no refresh token or `resource` parameter on the client side.
- Fix: in-core (BR-007: an authorization server for MCP clients is one of the isolation functions the product should provide; replacing it with an overlay is an in-core gap). Content: code + PKCE, resource indicators, refresh token rotation with reuse detection, client registration, tokens bound to an audience per profile, bridge to the token validation of ContextForge.
- Cost (estimated): 20 days (range 15 to 25).

### E-03 · No audience check per resource (T05)
- Tests: T05
- Cause: `aud` is a global constant (`mcpgateway-api`); a token whose audience list contains this constant and the URL of resource B is accepted. Only the `server_id` scope bounds a token, not the audience. Method limit: the token is forged with the bench key to represent a token legitimately issued with two audiences; an ordinary client does not produce it, but an external authorization server could.
- Fix: in-core (BR-007: audience check; modifies the token validation of ContextForge).
- Cost (estimated): 5 days (assumes E-02 is done).

### E-04 · Connection selector supplied by the client ignored instead of refused (T07, T08)
- Tests: T07, T08
- Cause: a `connection_id` supplied as a tool argument, header or query parameter is neither honored nor rejected. The call executes on the connection of the called profile. No leak toward the account of B or U2 observed (all upstream entries are witness-1); the gap is that the acceptance run requires a refusal and the gateway does not issue one.
- Fix: overlay (request filter in front of the gateway rejecting these selectors, or strict input schema).
- Cost (estimated): 1 day.

### E-05 · Loss of the upstream OAuth session on concurrent renewals (T11)
- Tests: T11
- Cause: two simultaneous calls that trigger the renewal of the provider token present the same refresh token; the provider rotates one and refuses the other (`invalid_grant`). ContextForge does not serialize the renewal, loses the token and requires a new manual authorization (verified: the next call fails, it succeeds after reauthorization).
- Fix: in-core (the fix is a renewal lock in the token manager of ContextForge). A workaround overlay exists (serialize all requests per connection in the front proxy), at the price of concurrency; it does not fix the defect.
- Cost (estimated): 3 days.

### E-06 · No protected resource metadata for non-public profiles (T14)
- Tests: T14
- Cause: the product serves RFC 9728 metadata only for a public virtual server (404 for a team server, read in `routers/well_known.py` of the image); making A and B public would lift their isolation. The 401 without token does not carry `resource_metadata`.
- Fix: overlay (static documents per profile and challenge header added by the front proxy). Useful only if E-02 is done: the metadata would otherwise point to a nonexistent authorization server.
- Cost (estimated): 1.5 days.

### E-07 · An ID token signed by the gateway is accepted as an access token (T06)
- Tests: T06
- Cause: token validation reads `token_use` only to distinguish `session` from another type (`auth.py`, team resolution); no token type is refused. A token signed with the gateway key, not expired, carrying a known `jti` and `token_use: id`, is treated as an access token. Method limit: ContextForge does not issue ID tokens; the token is forged with the bench key to represent a token of another type, signed with this key, which assumes a key shared with the issuer (see E-02).
- Fix: overlay (the front proxy decodes the claims without verifying the signature and accepts only the `token_use` values that ContextForge issues, `api` and `session`). Variant refused as in-core: have ContextForge refuse an unknown `token_use`.
- Cost (estimated): 0.5 days (author's estimate, not measured).

## Total cost of the overlays

| Class | Gaps | Cost |
|-------|------|------|
| overlay | E-01, E-04, E-06, E-07 | 3.5 days |
| in-core | E-02, E-03, E-05 | 28 days |
| total | seven gaps | 31.5 days |

### Comparison with the gateway specification's estimates

Source: gateway specification, "Estimate" table (order of magnitude for a comfortable developer, assisted by Claude Code, "not a quote"). Figures carried over as is, without weighing (BR-007, BR-008).

| Item | Specification's estimate | Costs of this trial |
|------|--------------------------|---------------------|
| ContextForge trial on the acceptance run | 1 to 2 days | — |
| Go POC (inbound OAuth with Claude, relay of one connection, two protocol generations) | 3 to 5 days | overlays: 3.5 days; in-core: 28 days; total: 31.5 days |
| Complete version 1 (tree, library, tokens, policy, web administration, statistics, Docker) | 6 to 10 weeks | same |
| Code volume, tests included | 8,000 to 15,000 lines | not estimated in lines |

The costs of this trial are estimates by the report's author, not measured; those of the specification are by another author and of another nature (building versus adopting and fixing). The table does not make them commensurable: the weighing belongs to the stack choice.

## Supported protocol versions (T15)

ContextForge 1.1.0 (pinned image) announces, on the inbound client side, `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25` (generation with `initialize` handshake) and `2026-07-28` (sessionless generation, per request). Evidence:

1. The answer to `MCP-Protocol-Version: 2099-01-01` is a 400, code -32022, whose `data.supported` lists these five versions (`results.json#T15`).
2. `server/discover` in 2026-07-28 answers `supportedVersions: ["2026-07-28"]`, server `ContextForge` 1.1.0 (`results.json#T15`).
3. In the image: `mcp_types/version.py` (`HANDSHAKE_PROTOCOL_VERSIONS` = the first four, `MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)`); `middleware/protocol_version.py` validates the header against this list; `config.py`: `mcp_inbound_protocol_mode` defaults to `auto` (accepts 2026-07-28), `legacy` rejects 2026-07-28 with 400.

Version assumed without a header: `2025-11-25` (`config.py`, `protocol_version`). The bench configuration does not modify `mcp_inbound_protocol_mode`.

## Limits of the measurement

- The "vault" of T13 is the Postgres database that holds tokens, revocations and users (ContextForge has no separate vault). Finding: refusal when down (401), contrary to the "fails open" hypothesis of the documentation, which did not hold for a tool call.
- T10: the "account addition" is the addition, by U1, of a second connection to the other witness account.
- T15: streamable HTTP transport only; the legacy HTTP+SSE transport is not run. The "with sessions" generation is run through the `initialize` handshake: the gateway issued no `Mcp-Session-Id` in this configuration, so resuming a protocol session between two requests is not exercised. The 2026-07-28 generation is run by a hand-written client based on the gateway's error responses (`_meta` envelope, `Mcp-Method` and `Mcp-Name` headers), not by an official MCP client: a 2026-07-28 requirement that the gateway does not enforce, or enforces differently, is not verified.
- T10: passed by construction. The upstream binds the account to the `client_id` (`upstream/server.py`, lines 29 to 32): an existing connection cannot be reauthorized to the other account, and only adding a second connection, which the test plays, is possible. The scenario "reauthorize the same connection to the other account" cannot be run on this bench; the result does not attest that ContextForge would resist an upstream that allowed it. To be taken up again with an upstream without this binding for the Go POC.
- T12: the "persistent connection" is a maintained TCP connection (keep-alive), without an open MCP session or SSE stream (`recette.py`, `persistent_call`). The cut of an already established long MCP stream or session is not measured.
- T14: the count of upstream log entries is now read in the test window (0 entries); the first version wrote it as 0 without reading it.
- Cut delay of 60 s (T12, T13, `policy.json`): bench choice, bound of BR-011 (strictly less than the lifetime of the access token, 86,400 s); the gateway specification sets no delay. The result of T12 (30.8 s) holds for any bound above 31 s.
- T06: the control "`jti` removed alone" is outside the verdict; it only establishes why the previous version of the test refused.
- T12: the delay of 30.5 s matches the default TTL of the revocation cache (30 s).
- A single run per test on one version; the bench allows replaying on a later version (out of scope).

## Verdict

**no-go.** Criterion (BR-007): the verdict is "go" if the 16 tests pass or if each gap can be fixed in an overlay. Here 7 tests pass out of 16 and three gaps are in-core: E-02 (authorization server for MCP clients, replaced and not fixed by an overlay), E-03 (audience check) and E-05 (fix in ContextForge). The four overlay gaps (E-01, E-04, E-06, E-07) do not make up for those. The verdict depends neither on E-07 (overlay) nor on the comparison of costs (BR-007): E-02 alone suffices.

The stack choice belongs to the operator (BR-008); this trial writes none.
