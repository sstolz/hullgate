# Gateway Go POC report (F-002, HULL-5)

> **Status: final report (SCR-04.final).** The go / no-go table below was fixed and committed BEFORE the first probative result
> (BR-018, commit `e681dd9`); its "Criterion" and "Expected evidence" columns have not changed since; only the "State" column is filled in.

## Header

| Item | Value |
|------|-------|
| Probative acceptance run date | 2026-10-28T00:25:58Z, `./bench.sh --target gateway --purge-images`, from scratch (POC images absent at start); replay after the round 2 review of PR #15 |
| Bench commit (`bench_commit`) | `bc87ce94df54487f4b8f12098da0a4401eff5565` (source and configuration clean, `source_dirty: false`) |
| Go / no-go table commit | `e681dd9e9d215b460a2d3df539ca4767c5a74124`, ancestor of the `bench_commit` (`git merge-base --is-ancestor`: true, recorded in `results-gateway.json`) |
| Libraries | `ory/fosite` v0.49.0, `modelcontextprotocol/go-sdk` v1.4.0, `golang.org/x/oauth2` v0.34.0, `modernc.org/sqlite` v1.38.2 (`go.sum`) |
| Images | build base `golang@sha256:1a6d4452…77ac` (1.24), final `debian@sha256:62bd423a…034ad` (trixie-slim, pre-existing), gateway built locally (`isolation-bench-gateway:poc`); upstream and front proxy `python@sha256:b3575dcf…c3b2`; `gitea/gitea@sha256:a664a64d…eab`; Gitea MCP server `docker.gitea.com/gitea-mcp-server@sha256:ccdb5660…83d3` (1.11.0, pre-existing) |
| Evidence | `poc/isolation-bench/evidence/results-gateway.json` (16 tests, 6 controls, decoded claims of the access token, disk) |
| Disk (BR-021) | 23.5 GB free at start; measured growth 2.08 GB (ceiling 4); after teardown, image lists (identifiers and tags, `-a`) and volume lists equal to those from before, `gitea-mcp-server:1.11.0` included |

**Discarded acceptance runs.** An acceptance run at commit `0899265` (23:09Z, 16/16) was discarded: its BR-021 check read `docker images` without `-a`, blind to images pulled by digest and to builder intermediates, and wrongly concluded "lists equal" (fixed in `af4759f`). The run of `af4759f` (23:11Z, 16/16) was replaced by that of `2aaec9c` (23:19Z, 16/16) after the discovery of a gateway defect outside the acceptance run (role and vault check never executed at `/token`); this one, by the run of `9f8f2eb` after the addition to T06 of the assertion "one property per variant" (BR-014), identical results. The verdict covers the delivered POC (gateway source identical between `2aaec9c` and `9f8f2eb`). The go / no-go table did not change between these runs.

**Replay after review.** The run of `9f8f2eb` (2026-10-27T23:26:47Z, 16/16, six controls passed) is replaced by that of `b2458d6`, which carries the fixes of the review of PR #15 (N3, N5 to N9, see "Requirements carried over to the v1 core"): 16/16, six controls passed, identical results; T08 now plays the connection placed in role B (reading at the spec's role), refused in the same way. Configuration (`policy.json`, `mapping.json`, compose, `go.mod`, `go.sum`, `Dockerfile`) unchanged since `9f8f2eb`; go / no-go table unchanged.

**Replay after the round 2 review.** The run of `b2458d6` (2026-10-28T00:05:45Z) is replaced by that of `bc87ce9` (2026-10-28T00:25:58Z), which carries the fix of `/revoke` (503 if the revocation is not persisted, see "Requirements carried over to the v1 core") and the memory of `core.hooksPath` in the common git directory: 16/16, six controls passed (BR-013, BR-015/BR-022, H-2, BR-022, BR-016, BR-003), identical results; growth 2.08 GB, 23.5 GB free before and after, image and volume lists equal to those from before. The hook found a different pre-existing value (`core.hooksPath` absolute to the main worktree), replaced it for the launch, then restored it at teardown (`results-gateway.json`, `meta.hook`). Configuration unchanged since `9f8f2eb`; go / no-go table unchanged.

## Go / no-go table (fixed before the acceptance run, BR-018, verdict criterion: BR-023)

| Id | Hypothesis | Success criterion | Expected evidence | State |
|----|------------|-------------------|-------------------|-------|
| H-1 | Isolation acceptance run 16/16 against the gateway | 16 lines T01..T16 run (none « non joué », none « non jouable »); the eleven isolation lines T02 to T09, T12, T13, T14 « réussi » | `evidence/results-gateway.json` (16 results) and test table | held: 16/16, eleven isolation lines « réussi » |
| H-2 | Inbound OAuth 2.1 with a `ping` tool | resource and server metadata specific to each access point; code + PKCE; `resource`; single-audience JWT access token whose `exp - iat` equals the duration of `policy.json`; renewal with rotation; revocation | decoded claims in `results-gateway.json`; T02, T03, T04, T05, T06, T14; `ping` call from the bench client | held (H-2 control passed, see "Hypotheses") |
| H-3 | Gitea relay end to end | one read call and one write call of role A reach the disposable instance; the hidden tool of role B is absent from its list and refused with no entry in the front proxy log; no fingerprint of the inbound token on the upstream side | `BR-015/BR-022` control of `results-gateway.json` | held (BR-015/BR-022 control passed) |
| H-4 | Two protocol generations inbound | T15 « réussi »: four versions with session and 2026-07-28 without session; an unknown version receives a refusal whose `error.data.supported` lists the set; otherwise each version not served is a costed gap (outside isolation lines) | T15 of `results-gateway.json` | held (T15 « réussi », five versions) |
| H-5 | Maturity of `ory/fosite` | audience per access point, `resource`, JWT token, renewal with rotation and revocation obtained by configuring `fosite` and glue, without rewriting any part of the authorization server; otherwise in-house plan B built and run, line "not held" with the measured cost of plan B | code `poc/go-gateway/oauth.go` and gap reading of the report | held, with glue (see "Hypotheses"); gap E-02 |
| H-6 | API of the MCP `go-sdk` | the SDK client opens and holds a session toward the bench upstream (with session) and toward the Gitea MCP server (without session) for `initialize`, `tools/list`, `tools/call`; versions served by the SDK noted; what the SDK does not provide is written by hand, classed "in-house" and costed | code `poc/go-gateway/relay.go`, reading of the SDK versions, gaps of the report | held in the sense of the criterion: relay through the SDK; MCP entry written by hand, "in-house", gap E-01 |
| H-7 | Claude.ai web and iOS (inbound OAuth from Claude clients) | operator execution of the trial runbooks kept in the private source, result reported on HULL-3 | operator feedback (BR-020); never bench evidence | awaiting operator |
| H-8 | Measured development cost | cost reported in day-equivalents, lines of Go and lines of test; benchmark: 3 to 5 days and 8,000 to 15,000 lines (specification), ContextForge: 31.5 days of gaps; output of H5 at 8 day-equivalents without isolation lines run | "Measured cost" section of this report | measured (see "Measured cost") |

**Verdict criterion (BR-023)**: "go" if the isolation lines (T02 to T09, T12, T13, T14) are « réussi » on the delivered POC and if the sum of the estimated costs of the remaining gaps (non-isolation tests T01, T10, T11, T15, T16; hypothesis lines H-3 to H-6 not held) does not exceed 5 d; "no-go" otherwise; no verdict while a line is « non joué ». Line H-7 is "awaiting operator" and counts neither for nor against; a "go" is worded "go, Claude.ai / iOS reservation open" as long as it is. The verdict does not choose the stack (BR-008 v2).

## Results of the 16 tests

Acceptance run of `bench_commit` `b2458d6`. "Log" = upstream log entries (bench upstream + Gitea front proxy union, BR-002, BR-013) in the test window.

| Id | Test | Result | Gap | Evidence |
|----|------|--------|-----|----------|
| T01 | Access token A on A, authorized action | réussi | | 1 entry `mcp witness-1 whoami`; connection token fingerprint received (BR-022) |
| T02 | Access token A on B | réussi | | 401 `invalid_token`, empty log |
| T03 | Refresh token A with resource B | réussi | | 400 `invalid_target`, no token issued; positive control (resource A) 200; empty log |
| T04 | Refresh A without resource or with A+B | réussi | | 400 `invalid_target` for both variants; positive control 200; empty log |
| T05 | Token with audiences A and B | réussi | | 401 for `aud=[A,B]` (the "product audience" variant coincides here with A); empty log |
| T06 | Invalid signature, issuer or expiration | réussi | | 7 variants refused with 401, one property each (BR-014), including `token_use=refresh` carrying its own `jti`; re-signed reference token accepted; empty log |
| T07 | Call from A with the connection_id of B | réussi | | tool of connection B unknown in role A; `connection_id` refused as argument (-32602), as header and as query parameter (400); empty log |
| T08 | U1 supplies a connection identifier of U2 | réussi | | read at the role: `connection_id` argument of the connection placed in role B refused, tool of this connection (`w2_whoami`) unknown to role A, reading of the connection through a public point 404; empty log |
| T09 | Hidden tool called directly | réussi | | hidden tool absent from `tools/list`, refused on direct call (-32602) and through `/rpc` (404); empty log |
| T10 | Account added after profile creation | réussi | | connection placed in role A in closed list: tool list unchanged, account `witness-1` unchanged |
| T11 | Two simultaneous renewals | réussi | | simultaneous calls 200/200, 0 `invalid_grant` (renewals serialized per connection) |
| T12 | Revocation or deletion of the profile | réussi | | cut in 0 s (bound 60 s) for revocation and deletion; no upstream entry after the cut |
| T13 | Vault / revocation check unavailable | réussi | | 503 `store_unavailable` at 0, 5 and 15 s; immediate recovery; empty log |
| T14 | OAuth discovery on A and B | réussi | | RFC 9728 metadata specific to A and to B; 401 challenge pointing to those of the resource; root 404 |
| T15 | Two generations of MCP clients | réussi | | 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 (session) and 2026-07-28 (no session); unknown version: 400, `error.data.supported` = the five |
| T16 | Recovery after provider error | réussi | | provider error returned to the client (`isError`), a single upstream call, one effective write, no entry of another account |

## Controls outside the acceptance run

| Control | Result | What it establishes |
|---------|--------|---------------------|
| BR-013 | réussi | GET, DELETE, GET and POST on unknown path traced on both logs before the first test |
| BR-015 / BR-022 | réussi | role A: `gitea_list_issues` reads the witness ticket, `gitea_issue_write` creates a ticket on the disposable instance; role B: `gitea_issue_write` absent and refused, front proxy without entry; the front proxy receives the connection token fingerprint (the Gitea MCP server has no token at startup) and no inbound token fingerprint |
| BR-022 | réussi | none of the fingerprints of the tokens issued by the gateway during the acceptance run in the upstream logs |
| BR-016 | réussi | network `isolation-bench_internal` `internal: true`, Gitea, Gitea MCP server and front proxy attached; sweep of forbidden hosts negative |
| BR-003 | réussi | `core.hooksPath` installed; a fake secret from the population makes `git commit` fail, the file is not erased |
| H-2 | réussi | see "Hypotheses" |

Operator mode (`AUTHORIZE_SECRET`, client `claude-operator`), manual trial outside the bench on an image built just before `2aaec9c` (this trial revealed the empty session subject, fixed in `2aaec9c`; the operator mode itself has not changed since): form served, wrong secret refused, redirect to `https://claude.ai/api/mcp/auth_callback` with code, PKCE exchange, `ping` → `pong`, renewal 200, log "token issued: grant=…" visible through `docker logs`. This is not evidence for H-7 (BR-020).

## Hypotheses

- **H-2, inbound OAuth** (`runner/oauth_checks.py`): server metadata `code_challenge_methods_supported = [S256]`; PKCE `plain` and absent PKCE refused; wrong verifier refused; code bound to A exchanged for B refused (`invalid_target`); single-use code, and its reuse revokes the tokens it issued (RFC 6749 §4.1.2); `ping`; renewal token rotation (new token, fresh `jti` and `iat`, single audience, duration = `policy.json`), renewal toward B refused without consuming the token, old access token invalidated by the rotation, reuse of the old renewal refused; RFC 7009 revocation of a renewal token (the access token of the same authorization falls) and of a JWT access token; renewal refused after deletion of the role and during a vault outage. Informative: a code refused for its resource is burned (the PKCE handler of `fosite` deletes its session at validation); the reuse of a renewal revokes the lineage.
- **H-5, maturity of `fosite`**: held without rewriting any part of the authorization server, with glue (≈ 120 lines) that is the real cost: `fosite` does not know `resource` (RFC 8707) — translated to `audience` at `/authorize`, and compared with the original audience at renewal, because `fosite` takes over the original audience without looking at the parameter (without this check, T03 issued a token); stateless JWT access tokens — the revocation and rotation of `fosite` do not touch them, hence `revStore` which carries the revocation over to the `jti`; `iat` and `jti` taken from the cloned session at renewal, hence `fixedSession`. Only storage provided: in memory (gap E-02). Neither dynamic registration (DCR) nor CIMD in `fosite`: pre-registered clients (gap noted E-04).
- **H-6, `go-sdk` API**: the SDK client (`StreamableClientTransport`) holds the sessions toward the bench upstream (with session) and toward the Gitea MCP server (without session), for `initialize`, `tools/list`, `tools/call`. SDK v1.4.0 knows 2024-11-05 to 2025-11-25 (the latter marked "not yet released" in `mcp/shared.go`) and **not** 2026-07-28: it talks to the Gitea MCP server through the `initialize` handshake, which the latter accepts. On the inbound side, the SDK server does not serve 2026-07-28 and does not allow a tool list per authenticated role without wrapping it: the MCP entry (`mcp.go`, ≈ 180 lines) is written by hand, classed "in-house" (gap E-01).
- **H-3, H-4**: see the BR-015 / BR-022 controls and T15.

## Gaps

No test failed. The gaps below are maturity gaps noted on the hypothesis lines, costed for the stack weighing (BR-023); the sum counts them all, out of caution.

### E-01 · MCP entry written by hand (H-6)

- Cause: the `go-sdk` v1.4.0 server does not serve 2026-07-28 and does not expose a tool list per role behind its own authentication; `mcp.go` routes `initialize`, `server/discover`, `tools/list`, `tools/call` itself. It does not validate `Mcp-Session-Id`, serves no SSE stream (GET answers 405) and no cancellation.
- Fix: in-house — complete the entry (sessions, SSE, cancellation, `listChanged`) or back it with the SDK server when it serves 2026-07-28.
- Cost: 2 d

### E-02 · OAuth storage in memory (H-5)

- Cause: `fosite` provides only `storage.MemoryStore`; codes, renewal tokens and sessions are lost at restart (the POC's SQLite vault holds only roles and issued and revoked `jti`).
- Fix: in-house — implement the storage interfaces of `fosite` on SQLite (`modernc.org/sqlite`).
- Cost: 1 d

### E-03 · Outbound renewal on every call for a short-lived provider token (T11)

- Cause: the renewal margin is fixed (5 min); a provider token of less than 5 min is renewed on every call (T11: 3 renewals for 3 calls, without loss of session).
- Fix: in-house — margin proportional to the token's lifetime.
- Cost: 0.5 d

### E-04 · Client registration: pre-registration only (noted, outside the sum)

- Cause: neither DCR (RFC 7591) nor CIMD in `fosite`; the POC pre-registers `bench-gw-client` and `claude-operator`. The scope says "measured if the SDK allows it, otherwise noted" (spec §2); if Claude.ai required it, H-7 would say so (BR-020).
- Fix: in-house — minimal DCR or CIMD.
- Cost: 1.5 d

## Comparability with F-001 (H6 of the spec)

Changes of the runner since F-001: branches T03 / T04 "authorization server present"; BR-014 variants added to T06 (the F-001 "ID token" variant is run only against ContextForge, rewritten as single-property variants for the gateway); reference control of T06; window on the union of two logs; catch-all log and fingerprint of the received token (`upstream/server.py`); case-insensitive reading of the `WWW-Authenticate` challenge; T14 additionally requires, against the gateway, the `resource_metadata` pointer of the challenge and the absence of a global resource; correspondence check (`mapping.json`) at startup.

| Tests | Comparison with F-001 |
|-------|-----------------------|
| T01, T02, T07 to T13, T15, T16 | same test code (log reading extended to two sources) |
| T03, T04, T05, T06, T14 | not strict: branches, variants or requirements added; T05 has no distinct "product audience" |

## Measured cost

| Measure | Value |
|---------|-------|
| Lines of Go (gateway) | 1,030 (`poc/go-gateway/*.go`) |
| Bench lines added or modified | 1,120 insertions, 75 deletions over 13 files (adapter, front proxy, controls, fixes N4 / N6 / N9) |
| Session time | ≈ 1 h 15 of agent work (22:31–22:51 Sonnet, then 22:53–23:20 Opus for the resumption and fixes, excluding the report) |
| Day-equivalents | estimate: 3 to 4 d of developer for a result of this volume and nature (agent time is not convertible as is) |
| Benchmarks | specification: 3 to 5 d and 8,000 to 15,000 lines (for the v1, not the POC); ContextForge: 31.5 d of gaps |
| Remaining gaps | E-01 + E-02 + E-03 = 3.5 d (E-04 noted: 1.5 d, outside the sum; with it 5.0 d) |

## Requirements carried over to the v1 core (review of PR #15)

Three non-blocking findings of the review (PR #15, review 397) are not fixed in this disposable POC (BR-008 v2); they are **requirements of the v1 core** (ticket #7 "Core"):

- **N1 · `client_id` claim (RFC 9068 §2.2)**: the access token carries `client_id` (REQUIRED), in addition to `aud`, `exp`, `iat`, `iss`, `jti`, `sub`; the POC omits it although spec §3.1 announces the RFC 9068 profile.
- **N2 · scope checked at the access point**: the announced scope (`scopes_supported: [mcp]`, RFC 9728 and 8414) is required by the token verification; failing that, `403` with `WWW-Authenticate: Bearer error="insufficient_scope"` (RFC 6750 §3.1). The POC does not check it (no effect on isolation: the role carries the boundary).
- **N4 · role deletion final for its tokens**: recreating a deleted role revives no token issued before the deletion (refusal of an already deleted identifier, or revocation of the role's `jti` at its deletion); the POC, through `insert or replace … deleted=0`, makes them valid again.

Fixed in the branch after the review (`b2458d6`): N3 at `/token` and `/admin/revoke` (revocation not persisted: no token delivered, 503), N5 (generic upstream error to the client), N6 and N7 (hook and teardown of the bench), N8 (T08 read at the role), N9 (runbooks). N3 at `/revoke` was not held by `b2458d6`: `fosite` v0.49.0 translates the vault failure into `temporarily_unavailable` but `WriteRevocationResponse` answers 200 for any error other than `invalid_request` / `invalid_client`, and nothing was revoked (measured by the round 2 review at `49f6f07`, `.cairn/runs/acts/HULL-5-review-2.md`). Fixed in round 2: the gateway itself writes `503 {"error":"temporarily_unavailable"}` (RFC 7009 §2.2.1) if the vault is down or if `revStore` reports a revocation not persisted; the nominal path remains that of `fosite`. Evidence: review harness `HULL-5-review-2-n3-test.py` replayed, 16 controls held out of 16 (simulated outage and real SQLite write failure, `/revoke` 503 in the four cases), `.cairn/runs/acts/HULL-5-apply-findings-2-n3-results.json`. Cost for the weighing of H-5: a `fosite` defect worked around by glue (≈ 30 lines added), to be carried over to the v1 core if `fosite` is chosen.

## Operator step (BR-020)

Copy-paste runbooks: the trial’s operator runbooks for Claude.ai web and Claude iOS, kept in the private source. Result to report on HULL-3; line H-7 remains "awaiting operator" until then.

## Verdict

Criterion: BR-023. Isolation lines: T02, T03, T04, T05, T06, T07, T08, T09, T12, T13 and T14 **all « réussi »** on the delivered POC (`b2458d6`, replay after review; previously `9f8f2eb`, same results). Non-isolation tests T01, T10, T11, T15, T16 « réussi ». Sum of the estimated costs of the remaining gaps: **3.5 d** (E-01 2 d, E-02 1 d, E-03 0.5 d; all "in-house"), under the threshold of **5 d**; E-04 (1.5 d, noted outside the sum) would bring the sum to 5.0 d, still at the threshold.

**go, Claude.ai / iOS reservation open** — line H-7 is awaiting operator (BR-020) and may reopen the verdict through HULL-3 if the defect lies in the POC's OAuth.

Weighing for the operator (not a stack choice, BR-008 v2): the isolation functions are written in Go on `fosite` + `go-sdk` in little code; the cost is concentrated in the OAuth glue (RFC 8707, stateless JWT against the revocation of `fosite`) and in the MCP entry, which the SDK does not cover for the 2026-07-28 generation. These two points are "in-house": they weigh on maintenance, not on feasibility.
