# Report on the Python gateway POC (F-003, HULL-16)

> **Record of a finished trial.** The Python POC (`poc/py-gateway/`), its bench target `py-gateway` and its witness `evidence/results-py-gateway.json` were removed by HULL-58; git history keeps them. The paths and commands below are those of the trial.

> **State: final report (SCR-04.final).** The go / no-go table below was fixed and committed BEFORE the first probative result
> (BR-018 v2, commit `7e414c3`); its "Success criterion" and "Expected evidence" columns are unchanged since; only the "State" column is filled in.

## Header

| Item | Value |
|---------|--------|
| Date of the probative acceptance run | 2026-10-28T02:09:23Z (`date_utc` of `results-py-gateway.json`), `./bench.sh --target py-gateway --purge-images`, from scratch (POC image absent at the start, built by `uv sync --locked`) |
| Bench commit (`bench_commit`) | `a8faea7e86b61ce6486854d3efca89b59900e55c` (clean source and configuration, `source_dirty: false`) |
| Go / no-go table commit | `7e414c36ccce775f35b57a1d153dd6bbe55c5e06`, ancestor of `bench_commit` (`git merge-base --is-ancestor`: true, recorded in `results-py-gateway.json`) |
| Packages (read from `uv.lock`) | `mcp` 2.3.0 (`mcp-types` 2.3.0), `authlib` 1.9.1, `pyjwt` 2.15.1, `starlette` 1.10.0, `uvicorn` 0.54.0, `httpx` 0.28.1 (Authlib), `httpx2` 2.16.0 (`mcp` SDK), `cryptography` 50.0.2, `pydantic` 2.14.1; `uv` 0.13.6 at build time |
| Images | gateway built locally (`isolation-bench-py-gateway:poc`) on `python@sha256:b3575dcf…c3b2` (Python 3.13.18), the same image as the bench's upstream and front end; `gitea/gitea@sha256:a664a64d…eab`; Gitea MCP server `docker.gitea.com/gitea-mcp-server@sha256:ccdb5660…83d3` (1.11.0, pre-existing) |
| Evidence | `poc/isolation-bench/evidence/results-py-gateway.json` (16 tests, 10 checks, decoded claims), `evidence/py-gateway-n3-results.json`, `evidence/py-gateway-revoke-nominal-results.json` |
| Disk (BR-021) | 23.2 GB free at the start; measured growth 0.05 GB (cap 4; the base Python image and Gitea, already present, do not count); after teardown with `--purge-images`, the image lists (identifiers and labels, `-a`) and volume lists equal those from before (`images_equal_to_before`, `volumes_equal_to_before`: true), 23.2 GB free |
| Runner (H6) | `git diff bc87ce9 -- runner/recette.py runner/adapter_gateway.py runner/oauth_checks.py mapping.json`: empty; `policy.json` differs only by the key `py_gateway_config_files` (policy values unchanged): **strict** comparison of the 16 tests |

**Acceptance runs.** A first probative acceptance run at `9373ecf` (2026-10-28T02:06:59Z, 16/16, ten checks passed) is replaced by that of `a8faea7`, which differs only by the issuance log line (`jeton émis : grant=… rôle=…`, format expected by the Go POC runbooks, see "Operator step"); identical results. The tuning launches (`--dev`, non-probative) found three defects fixed before any probative acceptance run: `/revoke` of a public client refused by the SDK (E-02 below, glue), an upstream session reopened on every call (seven upstream requests per call, including a GET stream and a DELETE without account: T10 échoué), and a 500 response to an unknown version (body not read).

## Go / no-go table (fixed before the acceptance run, BR-018 v2, verdict criterion: BR-023)

| Id | Hypothesis | Success criterion | Expected evidence | State |
|----|-----------|---------------------|-----------------|------|
| H-1 | 16/16 isolation acceptance run against the Python gateway | 16 rows T01..T16 played by the unchanged bench runner `bc87ce9` (H6 of the spec); no "non joué", no "non jouable"; the eleven isolation rows T02 to T09, T12, T13, T14 passed | `evidence/results-py-gateway.json` (16 results) and the test table | held: 16/16, eleven isolation rows passed, runner unchanged |
| H-2 | Inbound OAuth 2.1 with a `ping` tool | RFC 9728 metadata of each access point and RFC 8414; code + PKCE S256; `resource` at authorization, at code exchange and at renewal; single-audience JWT access token whose `exp - iat` equals the duration in `policy.json`; renewal with rotation; revocation; unchanged check `runner/oauth_checks.py` passed | check `H-2` and decoded claims of `results-py-gateway.json`; T02, T03, T04, T05, T06, T14 | held (check H-2 passed; `exp - iat` = 86,400 s) |
| H-3 | End-to-end Gitea relay | a read call and a write call of role A reach the disposable instance; role B's hidden write tool is absent from its list and refused with no entry in the front end's log; no inbound token fingerprint on the upstream side | checks `BR-015/BR-022` and `BR-022` of `results-py-gateway.json` | held (two checks passed) |
| H-4 | Two protocol generations on input | T15 passed: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 with session and 2026-07-28 without session; an unknown version receives a refusal whose `error.data.supported` lists the set; otherwise each version not served is a costed gap (outside the isolation rows) | T15 of `results-py-gateway.json` | held (T15 passed, five versions; the full list of versions is glue, see GP-2026) |
| H-5 | Maturity of the chosen OAuth building block (`mcp.server.auth` of the `mcp` SDK 2.3.0) | audience per access point, `resource`, RFC 9068 JWT token (with `client_id`), renewal with rotation, RFC 7009 revocation and persistent storage obtained by configuring `mcp.server.auth`, SQLite provider and glue, without rewriting an SDK handler; otherwise an in-house plan B done and played, row "not held" with the measured cost of plan B (H4 of the spec) | code `poc/py-gateway/` and the "library / glue / in-house" reading of the "Go vs Python" section | held, with glue (no handler rewritten; see "Hypotheses"); gap E-02 |
| H-6 | API of the chosen MCP SDK (`mcp` 2.3.0) | the MCP entry point (five versions, two generations) is served by the SDK's server; the SDK's client relays to the bench upstream and to the Gitea MCP server for `tools/list` and `tools/call`; what the SDK does not provide is written by hand, classed "in-house" or "glue" and costed | code `poc/py-gateway/`, versions read from `uv.lock`, gaps of the report | held: entry point and relay by the SDK, with glue (upstream session kept, GET stream cut) |
| H-7 | Claude.ai web and iOS (inbound OAuth from Claude clients) | operator contract identical to the Go POC (paths, `client_id` `claude-operator`, scope `mcp`, redirect `https://claude.ai/api/mcp/auth_callback`, `AUTHORIZE_SECRET`, public port 8080): the trial’s operator runbooks, kept in the private source and run by the operator with the Python POC launch block; otherwise runbooks of its own (BR-020); result reported on HULL-3 | operator feedback (BR-020); never bench evidence | awaiting operator |
| H-8 | Measured development cost | lines of gateway Python, lines of bench added, agent time, equivalent days, compared with the Go POC (1,030 lines, ≈ 1 h 15, 3 to 4 eq. d); exit from H5 at 8 equivalent days without isolation rows played | "Measured cost" section of this report | measured (see "Measured cost") |
| H-9 | Known v1 core requirements (BR-026) | N1 `client_id` claim; N2 token without scope `mcp` refused with `403` `insufficient_scope` (positive check: re-signed reference token accepted); N4 role deleted then recreated: 401 at the access point and `invalid_grant` at renewal for the earlier tokens; N13 `/revoke` without `token` → `400 invalid_request`; a requirement not held is a costed gap **outside the sum** of BR-023 (same criterion as the Go POC, operator decision at the freeze) | check `BR-026` (`runner/socle_checks.py`) of `results-py-gateway.json`; assertion `sans_token_400_invalid_request` of `evidence/py-gateway-revoke-nominal-results.json` | held: N1, N2, N4, N13 (checks `BR-026` and `BR-026/N13` passed) |
| H-10 | Checks outside the Go POC acceptance run replayed without changing an assertion (BR-025) | inbound OAuth check (`oauth_checks.py`) passed; N3 harness: 16 checks held; nominal `/revoke` harness: 14 checks held; a failed check is a costed gap **counted** in the sum of BR-023 | check `H-2` of `results-py-gateway.json`; `evidence/py-gateway-n3-results.json`; `evidence/py-gateway-revoke-nominal-results.json`; diff of each harness copy with its original | held: H-2 passed, N3 16/16, nominal 14/14 |

**Verdict criterion (BR-023)**: "go" if the isolation rows (T02 to T09, T12, T13, T14) are passed on the delivered POC and if the sum of the estimated costs of the remaining gaps (non-isolation tests T01, T10, T11, T15, T16; rows H-3 to H-6 and H-10 not held) does not exceed 5 days; "no-go" otherwise; no verdict while a row is "non joué". The gaps of row H-9 are costed and carried to the comparison, outside the sum. Row H-7 is "awaiting operator" and counts neither for nor against; a "go" is worded "go, Claude.ai / iOS reservation open" as long as it is. The verdict does not choose the stack (BR-008 v2).

**Acceptance run configuration**: dynamic client registration (DCR, RFC 7591) is **disabled** during the acceptance run (`DCR_ENABLED` absent from the `gateway` service of `py-gateway.compose.yaml`); it is measured separately by the sub-check `dcr` of `runner/socle_checks.py`, on a disposable instance where it is enabled.

## Results of the 16 tests

Acceptance run of `bench_commit` `a8faea7`, runner identical to the bench `bc87ce9` (strict comparison). "Log" = entries of the upstream log (bench upstream + Gitea front end union, BR-002, BR-013) within the test window.

| Id | Test | Result | Gap | Evidence |
|----|------|----------|-------|--------|
| T01 | Access token A on A, allowed action | réussi | | 1 entry `mcp witness-1 whoami` (upstream session kept: one upstream request per call, like the Go POC) |
| T02 | Access token A on B | réussi | | 401, `WWW-Authenticate: Bearer error="invalid_token"` (SDK middleware), empty log |
| T03 | Refresh token A with resource B | réussi | | 400 `invalid_target`, no token issued; positive check (resource A) 200; empty log |
| T04 | Refresh A without resource or with A+B | réussi | | 400 `invalid_target` for both variants; positive check 200; empty log |
| T05 | Token with audiences A and B | réussi | | 401 for `aud=[A,B]` (two variants; PyJWT alone would have accepted a list containing A: glue); empty log |
| T06 | Invalid signature, issuer or expiry | réussi | | 7 variants refused with 401, one property each (BR-014); re-signed reference token accepted; empty log |
| T07 | Call from A with connection_id of B | réussi | | B's tool unknown (-32602); `connection_id` refused as an argument (-32602), in a header and in a query parameter (400); empty log |
| T08 | U1 supplies a connection identifier of U2 | réussi | | read by role: argument `connection_id` refused, `w2_whoami` unknown to role A, reading of the connection through a public point at 404; empty log |
| T09 | Hidden tool called directly | réussi | | hidden tool absent from `tools/list`, refused on direct call (-32602) and through `/rpc` (404); empty log |
| T10 | Account added after profile creation | réussi | | tool list unchanged, account `witness-1` unchanged, 1 upstream entry |
| T11 | Two simultaneous renewals | réussi | | simultaneous calls 200/200, 3 renewals issued, 0 `invalid_grant` (lock per connection) |
| T12 | Profile revocation or deletion | réussi | | cut-off in 0 s (bound 60 s) for revocation and deletion; no upstream entry after cut-off |
| T13 | Vault / revocation check unavailable | réussi | | 503 `store_unavailable` at 0, 5 and 15 s; immediate recovery; empty log |
| T14 | OAuth discovery on A and B | réussi | | RFC 9728 metadata of A and of B; 401 challenge pointing to those of the resource; root at 404 |
| T15 | Two generations of MCP clients | réussi | | 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 negotiated by the SDK's server (stateless mode: no `Mcp-Session-Id` issued) and 2026-07-28 (`server/discover`, no session); unknown version: 400, `error.data.supported` = the five |
| T16 | Recovery after provider error | réussi | | provider error returned to the client (`isError`), a single upstream call, one effective write, no entry from another account |

## Checks outside the acceptance run

| Check | Result | What it establishes |
|----------|----------|------------------|
| BR-013 | réussi | GET, DELETE, GET and POST on an unknown path traced on both logs before the first test |
| BR-015 / BR-022 | réussi | role A: `gitea_list_issues` reads the witness ticket, `gitea_issue_write` creates a ticket; role B: `gitea_issue_write` absent and refused, front end without entry; the front end receives the fingerprint of the connection token and no inbound token fingerprint; the SDK's client negotiates alone with the Gitea MCP server (2026-07-28) |
| BR-022 | réussi | none of the fingerprints of the tokens issued by the gateway in the upstream logs |
| BR-016 | réussi | network `isolation-bench_internal` `internal: true`, Gitea, Gitea MCP server and front end attached |
| BR-003 | réussi | `core.hooksPath` installed; a fake secret makes `git commit` fail, the file is not erased |
| H-2 | réussi | `oauth_checks.py` unchanged, 18 checks held out of 18 (plus two informative) (see "Hypotheses") |
| BR-026 | réussi | N1, N2 (+ positive check), N4 (+ positive check), DCR disabled during the acceptance run, sub-check `dcr` (see "Core requirements") |
| BR-026/N13 | réussi | `POST /revoke` without `token` → 400 `invalid_request`, read on the nominal harness |
| H-10 / N3 harness | réussi | 16/16 (`evidence/py-gateway-n3-results.json`) |
| H-10 / nominal harness | réussi | 14/14 (`evidence/py-gateway-revoke-nominal-results.json`) |

**Harness copies (BR-025).** `runner/harness/HULL-5-review-2-n3-test.py`: `diff` with `.cairn/runs/acts/HULL-5-review-2-n3-test.py` = one line, `IMG, NAME = "poc-review-n3:tmp", "poc-review-n3"` → `"isolation-bench-py-gateway:poc", "isolation-bench-py-n3"`; `runner/harness/HULL-5-review-3-revoke-nominal-test.py`: one line, `NAME = "poc-review-n3-nominal"` → `"isolation-bench-py-n3-nominal"`. Ports unchanged (18300, 18301), no assertion touched; the files keep their name (the nominal harness imports the N3 harness by its name). Played by `run_bench.py` against the image built by the launch, before the teardown.

**Operator mode, outside the bench** (`.cairn/runs/acts/HULL-16-operator-mode-test.py`, results `HULL-16-operator-mode-results.json`, image built from `a8faea7`): 10/10 — metadata (`scopes_supported`: resource `[mcp]`, server `[mcp, offline_access]`, no `registration_endpoint`), `/authorize` of the client `claude-operator` sent to the `/consent` form without code, wrong secret refused, right secret → redirect to `https://claude.ai/api/mcp/auth_callback` with code and `state`, PKCE exchange 200, `ping` → `pong`, renewal 200, log `jeton émis : grant=authorization_code rôle=role-a` then `grant=refresh_token`, no JWT in the log. This is not evidence for H-7 (BR-020).

## Hypotheses

- **H-2, inbound OAuth** (`runner/oauth_checks.py`, unchanged): `code_challenge_methods_supported = [S256]`; PKCE `plain` and absent refused (`invalid_request` redirect, SDK handler); wrong verifier refused; code bound to A exchanged for B refused (`invalid_target`, glue); single-use code; code reuse → issued tokens revoked (401); `ping` → `pong`; renewal toward B refused without consuming the token; rotation; renewed token with new `jti` and `iat`, single audience, same duration; old access token invalidated (401); reuse of the old renewal token refused and lineage revoked; RFC 7009 revocation of a renewal token (the lineage's access token falls) and of an access token; renewal refused after role deletion and during a vault outage (503). Informative, not held: a code refused for its resource **is not** burned (the Go POC burned it as a side effect of `fosite`); the client can reuse it for the right resource, which RFC 6749 does not forbid.
- **H-5, maturity of `mcp.server.auth`**: held without rewriting a handler. The `/authorize`, `/token`, `/revoke`, `/register` handlers and the RFC 8414 metadata are the SDK's; the provider (SDK interface) is written on SQLite (≈ 150 lines, codes, lineages, renewal tokens as fingerprints, `jti` registry), the SDK providing no storage. The glue (≈ 50 lines, marked `COLLE` in `oauth.py`): (1) `RevocationRequest` declares `client_secret: str | None` **without default**, hence mandatory: every `/revoke` of a public client answered 400 `invalid_request` (measured during tuning by the N3 harness: S2 400 ×2, nominal 400), fixed by replacing the module's model; (2) `TokenHandler` does not pass `resource` on to renewal and does not compare that of the code exchange: comparison before the handler, without rotation; (3) `AuthorizationHandler` and `TokenHandler` read the form through `dict()`: several `resource` values reduce to the last, hence an explicit refusal of the ambiguity; (4) `/revoke` answers 401 `unauthorized_client` to an unknown client (RFC 6749 §5.2: `invalid_client`) and does not translate a provider exception (500): translated into 401 `invalid_client` and 503 `temporarily_unavailable`; (5) one resource per application (`resource_server_url`): RFC 9728 metadata and `BearerAuthBackend` instantiated per access point; (6) PyJWT accepts a list audience that contains the expected audience: single audience required.
- **H-6, API of the `mcp` SDK**: the MCP entry point is the SDK's `StreamableHTTPSessionManager` in stateless mode, JSON responses; it serves the four handshake versions and 2026-07-28 (`server/discover`) without in-house code; tools per role read from the ASGI scope by `on_list_tools` / `on_call_tool`. The SDK's client relays (`Client` in `auto` mode: handshake with the bench upstream, 2026-07-28 with the Gitea MCP server). Glue: (1) the client lives in an asynchronous context, one task per connection holds it open (upstream session reused, like the Go POC); (2) the client opens a GET stream after `initialize` and reopens it twice on failure: upstream requests outside any call, cut; (3) an unknown version receives `-32022` from the SDK with `supported` = the modern versions only (`['2026-07-28']`, measured by calling `unsupported_protocol_version_rejection` in the image): the gateway lists the five. Outbound OAuth: `authlib` `AsyncOAuth2Client` (code + PKCE, renewal with rotation), lock per connection (T11).
- **H-3, H-4**: see the checks BR-015 / BR-022 and T15.

## Core requirements (BR-026, row H-9)

| Requirement | Result | Measurement |
|----------|----------|--------|
| N1 · `client_id` claim | held | claims issued: `aud`, `client_id`, `exp`, `iat`, `iss`, `jti`, `role`, `scope`, `sub`, `token_use`; `client_id` = `bench-gw-client` |
| N2 · scope `mcp` required | held | forged token, `mcp` alone removed from `scope` (one property): 403, `Bearer error="insufficient_scope", error_description="Required scope: mcp", resource_metadata=…` (SDK middleware `RequireAuthMiddleware`); re-signed reference token: 200 |
| N4 · role recreated | held | dedicated role: 200 before deletion; after deletion and recreation: 401 at the access point, 400 `invalid_grant` at renewal; fresh authorization on the recreated role: 200 |
| N13 · `/revoke` without `token` | held | 400 `invalid_request` (SDK handler, once glue (1) of H-5 is applied) |
| DCR (comparison) | measured | acceptance run instance: `/register` 404, no `registration_endpoint`; disposable instance `DCR_ENABLED=1`: `registration_endpoint` announced, registration 201 with `client_id`, `/authorize` of the registered client sent to the barrier (no code), wrong secret refused, right secret → code to the registered redirect |

No H-9 gap.

## Go vs Python

Go measure: `docs/trials/poc-go.md`, `evidence/results-gateway.json` and the harnesses of the PR #15 review at `7287af4` (H10), never replayed. Python measure: `results-py-gateway.json` (`a8faea7`), harnesses and checks above. Means: "library" = configuration of the building block; "glue" = code that fixes or completes the building block; "in-house" = code in place of the building block (BR-024). Comparison of the tests is **strict** (identical runner, H6). A row "Python better" is not a stack recommendation (BR-008 v2).

| Id | Point | Go measure | Python measure | Evidence | Means (Python) | Direction |
|----|-------|-----------|---------------|--------|----------------|------|
| GP-T01 | T01 | réussi | réussi | T01 of both result files | — | equivalent (strict) |
| GP-T02 | T02 | réussi (401 `invalid_token`) | réussi (401 `invalid_token`) | T02 | — | equivalent (strict) |
| GP-T03 | T03 | réussi | réussi | T03 | — | equivalent (strict) |
| GP-T04 | T04 | réussi | réussi | T04 | — | equivalent (strict) |
| GP-T05 | T05 | réussi | réussi | T05 | — | equivalent (strict) |
| GP-T06 | T06 | réussi | réussi | T06 | — | equivalent (strict) |
| GP-T07 | T07 | réussi | réussi | T07 | — | equivalent (strict) |
| GP-T08 | T08 | réussi | réussi | T08 | — | equivalent (strict) |
| GP-T09 | T09 | réussi | réussi | T09 | — | equivalent (strict) |
| GP-T10 | T10 | réussi | réussi | T10 | — | equivalent (strict) |
| GP-T11 | T11 | réussi (3 renewals for 3 calls, E-03) | réussi (3 renewals for 3 calls, E-01) | T11 | — | equivalent (strict) |
| GP-T12 | T12 | réussi (cut-off 0 s) | réussi (cut-off 0 s) | T12 | — | equivalent (strict) |
| GP-T13 | T13 | réussi | réussi | T13 | — | equivalent (strict) |
| GP-T14 | T14 | réussi | réussi | T14 | — | equivalent (strict) |
| GP-T15 | T15 | réussi | réussi | T15 | — | equivalent (strict) |
| GP-T16 | T16 | réussi | réussi | T16 | — | equivalent (strict) |
| GP-ISO | Isolation rows | 11/11 passed | 11/11 passed | test tables | — | equivalent |
| GP-ECARTS | Remaining gaps, one by one, and sum | E-01 in-house MCP entry point 2 d, E-02 in-memory storage 1 d, E-03 renewal margin 0.5 d: 3.5 d (E-04 DCR/CIMD 1.5 d outside the sum) | E-01 renewal margin 0.5 d, E-02 glue on SDK internals 0.5 d: 1.0 d (E-03 CIMD 1 d outside the sum) | "Gaps" of both reports | — | Python better |
| GP-COUT | Measured cost | 1,030 lines of Go; ≈ 1 h 15 of agent time; 3 to 4 eq. d | 771 lines of Python; ≈ 50 min of agent time; 2 to 3 eq. d | "Measured cost" of both reports | — | Python better (indicative: different executants and sessions) |
| GP-RES-AUTH | `resource` at authorization | glue: `resource` translated into `fosite`'s `audience`, a single resource required | library + glue: the SDK receives `resource` and passes it to the provider; several `resource` values reduced to the last by the SDK, refusal of the ambiguity as glue | H-2, T14 | glue | equivalent |
| GP-RES-CODE | `resource` at code exchange | glue (comparison before `NewAccessResponse`); the refused code is burned | glue (the SDK does not compare); the refused code stays usable for its resource | H-2 `code_lié_à_A_échangé_pour_B_refusé` (400 `invalid_target` on both sides) | glue | equivalent |
| GP-RES-REFRESH | `resource` at renewal | glue before `NewAccessResponse` (`oauth.go`) | glue before `TokenHandler`: SDK 2.3.0 does not pass `resource` to `exchange_refresh_token` | T03, T04, H-2 | glue | equivalent |
| GP-REVOKE-JWT | Revocation of JWT access tokens | glue: `revStore` (including `RotateRefreshToken`), `fosite` does not touch the JWTs | in-house in the provider required by the SDK: `jti` registry and lineage revocation | H-2 `révocation_RFC7009_jeton_d_accès`, nominal harness `access_revoque_401` | in-house | equivalent |
| GP-STORE | Persistent storage | `fosite`: memory only (E-02, 1 d) | no storage in the SDK; SQLite provider written by the POC (codes, lineages, renewals, `jti`) | N3 harness S2: revocation and rotation writes failing under SQLite lock → 503, no token | in-house | equivalent |
| GP-DCR | DCR (RFC 7591) | not measured on the Go POC: absent from `fosite` (E-04) | library: the SDK's `RegistrationHandler`, 201, registered client subject to the operator barrier (5 lines of glue in the provider) | sub-check `dcr` of `BR-026` | library | Python better |
| GP-CIMD | CIMD | not measured on the Go POC: absent from `fosite` (E-04) | read, not measured: `client_id_metadata_document_supported` on the client side only in `mcp/shared/auth.py`, nothing on the server side | reading of the SDK (spec F-003 §9) | — | read, not measured |
| GP-2026 | 2026-07-28 on input | in-house: `mcp.go` written by hand (E-01, 2 d), the `go-sdk` v1.4.0 does not serve it | library: `server/discover` and sessionless envelope served by the SDK; 12 lines of glue so that `supported` lists the five versions | T15 | library | Python better |
| GP-TOOLS | Tool list per authenticated role | in-house (`mcp.go`) | library + glue: the SDK's `on_list_tools` / `on_call_tool` handlers, role passed through the ASGI scope | T09, T07, check BR-015/BR-022 | glue | Python better |
| GP-REVOKE-503 | `/revoke` 503 on persistence failure | glue (error carrier in the context, B1); N3 16/16 | glue (provider exception translated into 503); N3 16/16 | `HULL-5-apply-findings-2-n3-results.json`; `evidence/py-gateway-n3-results.json` | glue | equivalent |
| GP-REVOKE-400 | `/revoke` 400 without `token` | not held (N13: 200) | held by the SDK handler (after glue on `client_secret`) | nominal harness `sans_token_400_invalid_request`: Go 13/14, Python 14/14 | library | Python better |
| GP-REVOKE-CLIENT | `/revoke` unknown client → `invalid_client` | library (`fosite`, 401 `invalid_client`) | glue: the SDK answers 401 `unauthorized_client` | nominal harness `client_inconnu_invalid_client` held on both sides | glue | Go better |
| GP-N1 | N1 `client_id` claim | not held | held (claims written by the POC: the SDK does not mint JWTs) | `BR-026` `N1_claim_client_id` | in-house | Python better |
| GP-N2 | N2 scope `mcp`, 403 `insufficient_scope` | not held | held by the SDK's `RequireAuthMiddleware(required_scopes=["mcp"])` | `BR-026` `N2_*` | library | Python better |
| GP-N4 | N4 role recreated | not held (`insert or replace` resurrects the tokens) | held: deleting the role = revoking its lineages | `BR-026` `N4_*` | in-house | Python better |

## To carry over to Go

Conditional on keeping Go; not a stack recommendation (BR-008 v2). Feeds HULL-7 (core, #7) if Go is chosen.

- **R-01** · GP-N2 — check the `scope` claim at the access point and answer `403` with `WWW-Authenticate: Bearer error="insufficient_scope", resource_metadata=…` (form of `RequireAuthMiddleware`); keep the check of `socle_checks.py` (forged token with one property + positive check). Target: `poc/go-gateway/mcp.go` (`handleMCP`, after `verify`) and requirement N2 of #7.
- **R-02** · GP-N1 — add `client_id` to the access token's claims (`JWTClaims.Extra`) in `newSession`. Target: `poc/go-gateway/oauth.go` and requirement N1 of #7.
- **R-03** · GP-N4 — when a role is deleted, revoke all lineages (or `jti`) issued for it, rather than marking the role: the Python POC's "lineage per authorization" model (table `lineages`, revoked per role, per reused code, per reused renewal) covers N4 and RFC 7009 revocation in one place. Target: `poc/go-gateway/admin.go` (`DELETE /admin/roles/{id}`) and `revStore` of `oauth.go`; requirement N4 of #7.
- **R-04** · GP-REVOKE-400 — presence check of `token` before `NewRevocationRequest`, 400 `invalid_request`. Target: `poc/go-gateway/oauth.go` (`POST /revoke` handler) and requirement N13 of #7.
- **R-05** · GP-REVOKE-CLIENT — keep the nominal harness's assertion `client_inconnu_invalid_client` in the core: `fosite` holds it, another building block (the Python SDK) does not hold it without glue. Target: core tests of #7 (harness `HULL-5-review-3-revoke-nominal-test.py`).
- **R-06** · GP-STORE — store codes, renewal tokens (as SHA-256 fingerprints) and lineages in SQLite, one `BEGIN IMMEDIATE` transaction per issuance or rotation (a failed record issues nothing): this is the fix for E-02 of the Go POC, of which the schema of `poc/py-gateway/gateway/store.py` is a starting point. Target: `poc/go-gateway/oauth.go` (`fosite` storage) and core #7.
- **R-07** · GP-DCR — if dynamic registration is kept, subject it to the same operator barrier as `claude-operator` (no code without the secret, refusal if no secret is configured) and keep the sub-check `dcr`. Target: client registration requirement of core #7.
- GP-2026: no entry — the gain comes from the Python SDK, which serves 2026-07-28; in Go, the entry point stays in-house as long as the `go-sdk` does not serve it (E-01 of the Go POC), nothing transposable to the code.
- GP-TOOLS: no entry — same reason: in Go, the per-role tool list is already written (`mcp.go`); the Python gain comes from the SDK's entry point.
- GP-ECARTS: no entry — the lower sum results from the rows above (R-01 to R-07, E-01 of the Go POC), not from a distinct way of doing things.
- GP-COUT: no entry — indicative cost measure, no way of doing things to carry over.

## Gaps

No test failed; no row H-9 or H-10 not held. The gaps below are maturity gaps noted on the hypothesis rows, costed for the weighing of the stack (BR-023).

### E-01 · Outbound renewal on every call for a short-lived provider token (T11)

- Cause: the renewal margin is fixed (5 min, taken from the Go POC to keep T11 comparable); a provider token of less than 5 min is renewed on every call (T11: 3 renewals for 3 calls, without loss).
- Fix: in-house — margin proportional to the token's lifetime (`relay.py`).
- Cost: 0.5 d

### E-02 · Glue laid on SDK internals (H-5, H-6)

- Cause: two fixes replace internal objects of the `mcp` SDK 2.3.0 at load time: the `RevocationRequest` model (`client_secret` mandatory) and `StreamableHTTPTransport.handle_get_stream` (the client's GET stream); an SDK version upgrade may break them with no visible error.
- Fix: in-house — replace them with explicitly passed subclasses (own `/revoke` handler, derived client transport) or with an upstream fix of the SDK, and keep the N3 / nominal harnesses and T10 as a guard.
- Cost: 0.5 d

### E-03 · CIMD absent on the server side (noted, outside the sum)

- Cause: the `mcp` SDK 2.3.0 knows CIMD only on the client side (read, not measured); the POC pre-registers `bench-gw-client` and `claude-operator`, and serves DCR behind the operator barrier (disabled by default). Out of the spec's scope (§Out of Scope); if Claude.ai required it, H-7 would say so (BR-020).
- Fix: in-house — client metadata document resolved and validated at `/authorize`.
- Cost: 1 d

## Measured cost

| Measure | Value |
|--------|--------|
| Lines of Python (gateway) | 771 (`poc/py-gateway/gateway/*.py`), of which ≈ 90 of glue marked `COLLE` and ≈ 150 of SQLite provider |
| Lines of bench added or modified | 683 insertions, 15 deletions over 9 files (target `py-gateway` of `run_bench.py`, `socle_checks.py`, `report_check.py --target py-gateway`, compose, `policy.json`, README, and the two harness copies) |
| Session time | ≈ 50 min of agent work (≈ 01:30–02:20 UTC from the commit and PR timestamps, quota interruption not counted), implementation, two probative acceptance runs, report and closure |
| Equivalent days | estimate: 2 to 3 d of developer time (agent time is not convertible as is) |
| Reference points | Go POC: 1,030 lines, ≈ 1 h 15, 3 to 4 eq. d, gaps 3.5 d; ContextForge: 31.5 d of gaps |
| Remaining gaps | E-01 + E-02 = 1.0 d (E-03 noted: 1 d, outside the sum; with it 2.0 d) |

## Operator step (BR-020)

**Comparison of the operator values** (BR-020 clarified) — read in `poc/go-gateway/oauth.go`, `poc/py-gateway/gateway/app.py` and `oauth.py`, and measured for the Python POC by the operator-mode trial:

| Value | Go | Python |
|--------|----|--------|
| Access point path | `/p/acme/app/prod/role-a/mcp` | `/p/acme/app/prod/role-a/mcp` |
| `client_id` | `claude-operator` (`OPERATOR_CLIENT_ID`) | `claude-operator` (`OPERATOR_CLIENT_ID`) |
| Scopes | resource `[mcp]`, server `[mcp, offline_access]` | resource `[mcp]`, server `[mcp, offline_access]` |
| Redirect URIs | `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback` (`OPERATOR_REDIRECTS`) | identical (`OPERATOR_REDIRECTS`) |
| Authorization secret | `AUTHORIZE_SECRET`, field `gw_secret` | `AUTHORIZE_SECRET`, field `gw_secret` (`/consent` page instead of `/authorize`, invisible to the operator) |
| Ports | public 8080, administration 8081 | public 8080, administration 8081 |
| Issuance log | `jeton émis : grant=… rôle=…` | `jeton émis : grant=… rôle=…` |

The values are identical, so the Go trial’s operator runbooks for web and iOS, kept in the private source, also apply here. Only **step 3 (build) and the image name** at steps 5 and 7 change. Run the runbooks as they are, replacing step 3 with the block below and `poc-gw-go` with `poc-gw-py` wherever it appears (steps 5, 7 and the appendix for the production server, where the folder becomes `poc/py-gateway/`), and `gateway POC Go` with `gateway POC Python` in the connector name.

Step 3 (Python POC) — in the first terminal, at the repository root:

```sh
docker image ls -aq --no-trunc | sort -u > "${TMPDIR:-/tmp}/poc-images-before"; docker system df --format '{{.Type}}={{.Size}}' | grep '^Build Cache=' > "${TMPDIR:-/tmp}/poc-cache-before"; docker build -t poc-gw-py poc/py-gateway
```

**Expected result**: the last line is `Successfully tagged poc-gw-py:latest` (legacy builder) or `naming to docker.io/library/poc-gw-py` (BuildKit); the build downloads the Python image (≈ 0.15 GB) and the packages pinned by `uv.lock` (≈ 40 MB), without compilation.

**If the result differs**: `uv sync --locked` failing (`lockfile needs to be updated`) → the repository is not up to date: `git pull` then rebuild; network error toward `pypi.org` or `files.pythonhosted.org` → the Mac does not reach PyPI (proxy, VPN): restore access and rebuild; any other error → copy the last 20 lines into the report on HULL-3.

Step 5 (launch) — same command as the runbook, image `poc-gw-py`:

```sh
export GW_PUBLIC="https://COLLE-ICI-L-ADRESSE.trycloudflare.com"; docker run -d --name poc-gw -p 127.0.0.1:8080:8080 -p 127.0.0.1:8081:8081 -e PUBLIC_BASE="$GW_PUBLIC" -e ADMIN_TOKEN="$GW_ADMIN_TOKEN" -e AUTHORIZE_SECRET="$GW_AUTHZ_SECRET" -e ACCESS_TTL_SECONDS=300 poc-gw-py && sleep 2 && docker logs poc-gw
```

**Expected result**: `passerelle POC Python : base https://…trycloudflare.com, durée de vie du jeton d'accès 300 s`.

**If the result differs**: as in the runbook (port in use, wrong base); if Claude.ai requires dynamic registration (step 7), relaunch adding `-e DCR_ENABLED=1`: the Python POC serves DCR, and the registered client goes through the same "Authorize" page (secret of step 2) — report it on HULL-3, it is a result.

Step 12 (cleanup) applies as is: it purges by difference the POC image and the Python database. In "if the result differs", replace `poc-gw-go` with `poc-gw-py` and read the `python` repository where the runbook says `golang`. Result to report on HULL-3; row H-7 stays "awaiting operator".

## Verdict

Criterion: BR-023. Isolation rows: T02, T03, T04, T05, T06, T07, T08, T09, T12, T13 and T14 **all passed** on the delivered POC (`a8faea7`). Non-isolation tests T01, T10, T11, T15, T16 passed; rows H-3 to H-6 held; row H-10 held (H-2, N3 harness 16/16, nominal harness 14/14); row H-9 held (N1, N2, N4, N13), outside the sum anyway. Sum of the remaining gaps: E-01 + E-02 = **1.0 d**, under the threshold of 5 d (E-03 CIMD noted outside the sum, 1 d).

**go, Claude.ai / iOS reservation open** — row H-7 is awaiting operator (BR-020) and may reopen the verdict via HULL-3 if the defect lies in the POC's OAuth.

Weighing for the operator (not a stack choice, BR-008 v2): both POCs pass the same acceptance run identically. Python obtains from its building blocks what Go wrote by hand (MCP entry point 2026-07-28, DCR, `insufficient_scope`, 400 without `token`), but not the storage (none in the SDK), and it needs as much OAuth glue as Go on `resource` (same defect at renewal), plus two fixes of SDK internals (E-02) and an `invalid_client` response that `fosite` held. The transposable lessons are in "To carry over to Go".
