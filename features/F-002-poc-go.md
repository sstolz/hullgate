---
id: F-002
title: Gateway Go POC, judged by the transposed isolation acceptance run
ticket: HULL-5
status: implemented
closed: 2026-10-27
domain: [isolation, gateway, poc]
br-introduced: [BR-012, BR-013, BR-014, BR-015, BR-016, BR-017, BR-018, BR-020, BR-021, BR-022, BR-023]
br-touched: [BR-001, BR-002, BR-003, BR-004, BR-005, BR-006, BR-008]
decisions: []
drafted-by: [model:claude-sonnet-5-5, model:claude-opus-5-5]
validated-by: human:sstolz
capabilities:
  extends: [MCP gateway isolation acceptance run]
  introduces: []
---

# F-002 · Gateway Go POC, judged by the transposed isolation acceptance run

## Metadata

| Field | Value |
|-------|-------|
| **Priority** | P1 — second step of the stack decision (part 4 of the brief) |
| **Branch** | `feat/HULL-5-poc-go` |
| **Last updated** | 2026-10-27 |

## 1. Context & Objective

### Current State

The ContextForge trial (F-001, HULL-2) returned a **no-go** verdict on 2026-10-27: 7 tests out of 16, three core gaps (E-02 authorization server for MCP clients, E-03 audience check, E-05 simultaneous renewals), 31.5 days of estimated gaps (28 d in-core, 3.5 d overlays) against 3 to 5 estimated days for a Go POC (gateway specification, estimate table). The operator decided the same day to launch the Go POC. What remains from F-001 is a reusable bench (`poc/isolation-bench/`: T01..T16 runner over HTTP/OAuth only, instrumented upstream, ContextForge-specific adapter) and three limits raised in the review of PR #14: N4 (incomplete upstream log: GET, DELETE and unknown paths not traced), N6 (the secrets scan detects but no hook blocks the commit), N9 (gap E-07 played with a single type of forged token).

Not one line of the gateway exists. The choice of language (Go proposed, Rust or Python mentioned by the specification) rests on estimates, not on a measurement; the maturity of `ory/fosite` (authorization server) and the exact APIs of the MCP `go-sdk` (relay) are explicitly "to be confirmed at the POC".

### Objective

Build a disposable POC in Go that exposes, for two access points (roles A and B), an OAuth 2.1 authorization server with a `ping` tool, relays the bench's instrumented upstream (subject of the acceptance run: two witness accounts, outbound OAuth to its authorization server) and relays a Gitea MCP server end to end, behind a role policy; judge it by **the same acceptance run** as ContextForge, **transposed to the role** as the gateway specification writes it, on the same bench; deliver a go / no-go table with criteria written in advance, which confirms or refutes the choice of Go. The deliverable is a POC, an extended bench and a report; it does not choose the stack (BR-008 v2).

### Expected Impact

- 16 tests out of 16 played against the gateway, each with a result and evidence of no upstream call (BR-012, BR-013).
- The three limits of the bench (N4, N6, N9) fixed before the first probative result (BR-013, BR-014, BR-003).
- A go / no-go table whose every row cites its criterion and its evidence (BR-018, BR-023), and a measured cost to set against the 3 to 5 estimated days and the 31.5 days of ContextForge.
- The part of the test that requires the operator's account (Claude.ai, iOS, public HTTPS URL) is isolated, documented in copy-paste runbooks and attached to HULL-3, without blocking the rest (BR-020).

### Open Product Questions

None open: the operator does not review, the points below were settled by the recommended value and are repeated in §9 (Architecture Decisions) for challenge at the freeze.

- **H1 — Claude.ai / iOS**: moved out of the autonomous scope, operator step (BR-020). The gateway specification writes that "the tests on Claude's behavior and the official server matrix do not depend on the language"; it does **not** say so of the inbound OAuth used from Claude.ai and iOS, the first criterion of the POC (step-by-step plan, point 2) and an output of the ticket. The language verdict is therefore rendered without waiting, but always worded with the open reservation (BR-023), which HULL-3 lifts or reopens (BR-020).
- **H2 — Protocol versions**: the POC serves as input the two generations that the bench measured on ContextForge: with a session (the four versions that T15 plays: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25) and without a session (2026-07-28), subject to the `go-sdk`; a version that the SDK does not serve makes T15 fail and becomes a costed gap (BR-017), without counting among the isolation rows of the verdict (BR-023).
- **H3 — Gitea upstream**: the official Gitea MCP server (`docker.gitea.com/gitea-mcp-server:1.11.0`, present locally) facing a disposable Gitea instance. **Verified on 2026-10-27** (`/app/gitea-mcp --help`, container trial): HTTP transport available (`-t http`, port 8080, `/mcp` endpoint), **sessionless** server (startup log: "stateless, protocol up to 2026-07-28") that also accepts `initialize` in 2025-06-18; default host `https://gitea.com` (so `-H` is mandatory, BR-016); static token (`-T`, `GITEA_ACCESS_TOKEN`) or per-request token in `-oauth` mode. Fallback, if the relay fails: a bench upstream that imitates two Gitea tools, with the gap noted.
- **H4 — OAuth plan B**: if `fosite` does not hold on a point (audience per access point, limited renewal), the POC may write the necessary OAuth 2.1 subset (plan B of the specification); the "fosite maturity" row of the table then becomes a costed "gap", and a plan B that is built and played by the acceptance run is not a gap of the verdict (BR-023).
- **H5 — Effort**: reference point of 3 to 5 days (specification), not a ceiling; stop if the cumulative effort exceeds 8 day-equivalents without the isolation rows of BR-023 being played: partial report (SCR-04.partial), verdict "not rendered", return to the operator (exit rule, §12).
- **H6 — Completed acceptance run**: the additions to `recette.py` (T03/T04 branches with an authorization server, token variants of BR-014) are not replayed against ContextForge. They **add** variants without modifying the existing ones, except the "ID token" variant of T06, which changes two properties (`token_use` and `scopes`) and which BR-014 forces to be rewritten. The F-001 report stays as it is; the POC report lists the runner changes and marks T03, T04, T05 and T06 "comparison with F-001 not strict", the other twelve tests being played by the same code.

## 2. Scope

- **POC gateway in Go** (`poc/go-gateway/`), containerized, two access points `/p/<client>/<projet>/<env>/<rôle>/mcp` for roles A and B (same tree reduced to two leaves), tokens bound to a role.
- **Authorization server per access point**: own resource metadata (RFC 9728) and server metadata (RFC 8414), authorization code with PKCE, `resource` parameter (RFC 8707), single-audience token, renewal with rotation, revocation; registration by pre-registration (CIMD / DCR: measured if the SDK allows it, otherwise noted).
- **`ping` tool** served by the gateway itself.
- **Relay of the bench's instrumented upstream** (`upstream/server.py`), subject of T01..T16: two connections (witness accounts `witness-1`, `witness-2`) obtained by outbound OAuth to the bench's authorization server (`golang.org/x/oauth2`, the building block retained by the specification), renewal with rotation, including simultaneous renewal (T11); prefixed tools `whoami`, `get_note`, `write_note`, `flaky`, `hidden_admin`.
- **End-to-end relay** to a disposable Gitea MCP server behind a disposable Gitea instance, with prefixed tool renaming and one tool hidden by the role B policy. The two upstreams cover the two generations: the bench upstream opens a session (`Mcp-Session-Id`), the Gitea MCP server is sessionless (H3) — "one upstream per protocol generation" from the specification.
- **Two protocol generations** as input (H2).
- **Bench extension**: `gateway` adapter (POC administration: roles, revocation, vault failure, forgery), completion of the T03/T04 branches, fixes N4, N6, N9, addition of a disposable Gitea to the composition.
- **Transposed acceptance run**: T01..T16 against the gateway; **report** `docs/trials/poc-go.md` with a go / no-go table.
- **Operator step documented in copy-paste runbooks**: the operator runbook of the trial, kept in the private source and the operator runbook of the trial, kept in the private source, each step ready to paste (BR-020).
- Developer workstation: local container, one bench client direct (equivalent of "Claude Code direct").

## Out of Scope

- **Claude.ai web, Claude iOS, public HTTPS URL, tunnel**: operator step attached to HULL-3 (BR-020); the session opens no tunnel and no public URL.
- **Behavior of the Claude clients** (resource, CIMD, two connectors on one domain, tool-list refresh, name length): HULL-3.
- **Official server matrix** (Drive, Dropbox Dash, Work IQ, Notion, Canva): HULL-4.
- **Claude Desktop via local MCPB connector**: operator step, HULL-3.
- **Outbound OAuth to a real provider**, dynamic registration or application declared with a provider, connection library, encryption of secrets at rest: v1 (HULL-8); the POC is an OAuth client only of the bench's authorization server (pre-registered bench clients) and injects a disposable Gitea connection by configuration.
- **Performance, load, multi-architecture images**: no target; the measured cost is a development cost.
- **Full tree, web administration, statistics, full policy (modes, pinning, guardrails)**: v1 (HULL-7, 9, 10, 11, 12).
- **Hosting on the production server, Traefik, deployment**: foundation (HULL-6); no action toward `prod-gateway`.
- **Reuse of the POC code**: disposable; no promise of carrying it over to v1 (the decision belongs to HULL-6 / 7).
- **Stack choice, `project.stack`, ADR**: the operator, after the POC (BR-008 v2).
- **Replay of the bench on ContextForge** after the runner changes (H6) and replay on other library versions.

## 3. Functional Spec

### 3.1 The POC gateway (SCR-01)

A Go binary in a container, a cgo-free SQLite for tokens and roles, configuration at initialization (no web interface).

- **Access points**: `/p/acme/app/prod/rôle-a/mcp` and `…/rôle-b/mcp` (or the equivalent of the bench paths). Each access point has its own OAuth resource (`resource` = its URL) and its own metadata.
- **Authorization**: `/authorize`, `/token`, `/revoke`, metadata `/.well-known/oauth-protected-resource/<chemin>` and `/.well-known/oauth-authorization-server`. An access token is bound to a single audience (the access point); a renewal token to a role.
- **Access token format**: signed JWTs (RFC 9068 profile: `iss`, `aud`, `exp`, `iat`, `jti`, type), signing key generated at launch and read by the adapter as a test secret (BR-010). The bench reads the claims (`run_bench.py` checks `exp - iat` against `policy.json`) and the adapter forges JWTs (T05, T06, BR-014): an opaque token would empty these tests. Lifetime: that of `policy.json` (BR-011, values unchanged).
- **Tools**: `ping` (roles A and B); tools of the bench upstream relayed with a prefix (roles A and B, each on its witness account, `hidden_admin` hidden); Gitea tools relayed with a prefix (`gitea_…`) for role A; for role B, a write tool hidden by the policy (closed list by default, writes opened tool by tool).
- **Relay**: upstream sessions opened by the gateway with the **connection token** (OAuth token of the witness account for the bench upstream, token of the disposable instance for Gitea, passed per request in `-oauth` mode of the Gitea MCP server or, failing that, at the start of that server); the incoming token is never forwarded (BR-022).
- **POC administration**: local, unexposed administration endpoints that only the bench adapter calls (role creation, revocation, role deletion, vault failure simulation, placement of a connection in a role).

#### Visual States

- **SCR-01**: nominal, two healthy access points.
- **SCR-01.error**: upstream unavailable or vault down: explicit refusal (401 or 503 depending on `policy.json`), never a silent open.
- **SCR-01.revoked**: role deleted or token revoked: tokens invalid, sessions cut.

### 3.2 The transposed acceptance run (SCR-02)

The 16 tests of section 6 of the review, **read at the role** through the table of the gateway specification (BR-012). Reminder of the reading:

| In the acceptance run | In the gateway |
|-----------------------|----------------|
| U1 with access to profiles A and B | tokens issued for roles A and B |
| U1 supplies a connection identifier of U2 | the token of role A supplies a `connection_id` placed in role B |
| Revocation or deletion of the profile | deletion of a role: tokens invalidated, sessions cut, caches emptied |
| Deletion of a user | revocation of a token |
| Addition of an account after the profile is created | placement of a connection in an existing role: no silent account change |

The other tests apply as they are. To be played for the first time (not playable against ContextForge):

- **T03, T04**: renewal of A's token with resource B, without `resource`, with A+B (the authorization server now exists);
- **T14**: resource-specific metadata for A and B, with no fallback to a global audience;
- **T06 and variants**: tokens forged to one property (BR-014), including a token of another type carrying its own `jti` (N9);
- **T13**: vault failure, default refusal according to `policy.json`;
- **T15**: the two input generations of H2, as T15 plays them (four versions with a session, one without).

All tests address the bench's instrumented upstream (witness accounts, log, `flaky` failure control, adjustable lifetime for T11); the Gitea relay is judged separately (BR-015). A test whose function the POC does not implement is « échoué » with a gap, never « non jouable » (BR-012).

The policies (T04: refusal of ambiguous requests; T13: default refusal; T12: cut-off delay < lifetime of the access token) are those of `policy.json`, fixed and committed before the first test (BR-011 of F-001, applied as is).

### 3.3 The extended bench (SCR-03)

`poc/isolation-bench/` remains the single entry point (`./bench.sh --target gateway`). Additions: `gateway` service (built image, Go base pinned by digest), disposable Gitea and Gitea MCP server (BR-015, BR-016), exhaustive upstream log (BR-013) with a fingerprint of the received token (BR-022), logging front proxy between the gateway and the Gitea MCP server, reading by the test window of the **union** of the two logs (BR-002), blocking pre-commit hook (BR-003), bounded cleanup (BR-021). Network: Gitea, the Gitea MCP server and the front proxy on an internal Docker network; the gateway and the bench upstream are joined to it and published on `127.0.0.1` for the runner (a container attached to the internal network alone publishes no port). Secrets remain generated at launch and destroyed at teardown (BR-010 of F-001, applied as is).

### 3.4 The report and the go / no-go table (SCR-04)

`docs/trials/poc-go.md`: header (image digests, versions of `fosite` and the `go-sdk`, bench commit, date), 16-row table (id, result, evidence), gaps (cause, fix, cost in days, class "library" or "in-house" in the sense of BR-023, BR-004), comparability with F-001 per test (H6), go / no-go table (BR-018, BR-023), measured cost (day-equivalents, lines of Go and test) against the estimates, pointer to the operator runbooks (BR-020), verdict.

#### Visual States

- **SCR-04.partial**: partial report committed early (the bench and a skeleton first); no « non joué » row in the final. This is also the state rendered at the exit of H5: verdict "not rendered".
- **SCR-04.final**: 16 rows, complete table, verdict.

## 4. Business Rules

### Naming Convention

`BR-NNN`, sequential and global: `conventions/br-identifiers.md`. Ceiling read: BR-011 (rule index, eleven rows, and §4 of F-001 `implemented`; no other spec in flight); first rule allocated: BR-012.

### Overlap report (br-identifiers §Overlap)

Index loaded: 11 rules. Filter on the `Statement` column (acceptance run, log, refusal, secret, token, gap, verdict, stack, adapter, test): **11 rows retained out of 11**. Verdict after reading:

- BR-001 (16 tests, no transposition) — **overlap** — **new version v2** (C2: v1 requires "U1/U2 as the review writes them"; a transposed acceptance run did not satisfy it and satisfies it after adoption). v2: 16 tests, without transposition against a product that has users (ContextForge), read at the role according to BR-012 against a gateway with roles; the control (count = 16) does not change. The transposition itself (the table, its cases) is another obligation: **BR-012 new** (C1).
- BR-004 (16 rows, gaps classed overlay / in-core) — **overlap** — **new version v2** (C2: a POC gap classed "library" or "in-house" does not satisfy v1, and `report_check.py` would refuse it). v2: fix classed according to the verdict criterion of the target (BR-007 for ContextForge, BR-023 for the POC).
- BR-009 (« non jouable » is proven) — **applied as is**; against the POC, its case is bounded by BR-012 (a function of the scope that is not implemented is « échouée »).
- BR-002 (refusal = no upstream call) — **overlap** with N4 — the log must be exhaustive: exhaustiveness can be violated while the refusal tests are run, and vice versa → **BR-013 new** (C1); BR-002 receives a clarification (C3: "the log covers the MCP server and the authorization server" is not held if a GET, a DELETE or an unknown path escapes; with two relayed upstreams, the upstream log of the window is the union of the bench upstream's log and that of the Gitea front proxy; T03 and T04 join the refusal tests).
- BR-003 (no secret, a positive scan blocks the commit) — **overlap** with N6 — clarification of BR-003 (C3: the rule already writes "blocks"; the instrument is missing).
- BR-005 (one command, pinned versions) — **overlap** — clarification of BR-005 (C3: Go base, Gitea and Gitea MCP server by digest, `go.sum`; the bench is the same).
- BR-006 (acceptance run HTTP/OAuth only, reusable without modification) — **overlap** — clarification of BR-006 (C3: the rule aims at the absence of dependency on the product; only the adapter changes for the product). The runner additions (BR-012, BR-014) are not an adaptation to the product but depart from "without modification" read literally: gap **accepted** (`warning` rule), listed in the report (H6). Its control state goes from `deferred` to `verified` at closure.
- BR-007 ("go" criterion of the trial) — **neighbor**: criterion of the ContextForge trial, different object; **BR-023 new** (C1), of analogous form; fixing the table in advance is another obligation: **BR-018 new** (C1, analogous to BR-011).
- BR-008 (the recommendation does not decide the stack) — **overlap** — **new version v2** (C2; operator arbitration at the freeze, 2026-10-27, which replaces the new rule BR-019 of the draft): v1 has for subject "the trial" (of ContextForge) and a POC satisfies it only by analogy. v2: the subject becomes any stack-choice trial or POC (the ContextForge trial, the Go POC); the rule (neither `project.stack`, nor ADR, nor stack decision in the repository; a verdict and a recommendation; the decision belongs to the operator) and its control (`check_scope.py`, `verified`) do not change; a violation makes `check_scope.py` fail and the PR is not merged.
- BR-010 (disposable test secrets) — **neighbor**: applied as is to the POC (no modification, hence not in `br-touched`); the "real Gitea" case is another obligation (access point, not a secret): **BR-016 new** (C1).
- BR-011 (policies fixed before the acceptance run) — **applied as is**, cited in §3.2 (values of `policy.json` unchanged; the list of configuration files becomes specific to the target, which does not change the rule).

Number 019: allocated to the draft for the POC, abandoned at the freeze in favor of BR-008 v2; never written in the index nor extracted, **never reassigned** (`br-identifiers.md`: a number is never reused) — the sequence BR-018, BR-020 keeps this hole. New rules with no neighbor in the index: BR-015 (end-to-end relay), BR-017 (protocol versions), BR-020 (operator step), BR-021 (disk), BR-022 (incoming token not forwarded). Ceiling re-read at the stress test: BR-023 allocated next (verdict criterion, split off from BR-018).

### Existing rules touched (extracted at closure)

Texts written at closure from the overlap report above (C2: new version; C3: clarification, version incremented).

#### BR-001 · The acceptance run plays the 16 tests of the review (C2)

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-001.md`](../business-rules/BR-001.md).
> The content below reflects the state of this BR at the time of feature F-002 (v2).

**Rule** — The acceptance run that is executed includes the 16 tests of section 6 of the technical review, including the failure test (T13): against a product that has users (ContextForge), with profiles A/B and users U1/U2 as the review writes them, without transposition; against a role-based gateway (Go POC, v1), read at the role level per BR-012.

**Clarifications**
- No test is removed, merged or rewritten to fit the gateway's model; a test that the product does not allow to be played is « non jouable » (BR-009) and becomes a gap (BR-004); against the Go POC, « non jouable » is bounded by BR-012 (a function of the scope that is not implemented is « échoué »).
- The transposition itself (the table, its cases) is the subject of BR-012.
- The count of 16 is necessary, not sufficient: each test's fidelity to its line of the review is read by hand in PR review.

#### BR-004 · Every failed or non-playable test is a documented gap (C2)

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-004.md`](../business-rules/BR-004.md).
> The content below reflects the state of this BR at the time of feature F-002 (v2).

**Rule** — The report contains exactly 16 test lines; each « échoué » or « non jouable » test carries a gap with its cause, its fix classed by the target's verdict criterion — "overlay" or "in-core" for ContextForge (BR-007), "library" or "in-house" for the Go POC (BR-023) — and its estimated cost in development days; an incomplete gap is a report structure error.

**Clarifications**
- The class of a POC fix informs the weighing of the stack, it does not change the verdict (BR-023).

#### BR-008 · A stack-choice trial or POC does not decide the stack (C2)

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-008.md`](../business-rules/BR-008.md).
> The content below reflects the state of this BR at the time of feature F-002 (v2).

**Rule** — A trial or a stack-choice POC (the ContextForge trial, the Go POC) writes neither `project.stack` nor a stack choice (neither an ADR nor a stack decision in the repository); it produces a verdict and a recommendation; the decision belongs to the operator. A violation makes `check_scope.py` fail and the PR is not merged.

**Clarifications**
- Operator ruling at the F-002 freeze (2026-10-27): no new rule for the POC; the number BR-019, allocated to the draft, is never reassigned.

#### BR-002 · A refusal is proven by the absence of an upstream call (C3)

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-002.md`](../business-rules/BR-002.md).
> The content below reflects the state of this BR at the time of feature F-002 (v2).

**Rule** — A refusal counts as « réussi » only if the upstream log contains no entry in the test's window; otherwise the test is « échoué », even if the HTTP response is a refusal.

**Clarifications**
- The upstream log covers the upstream MCP server **and** its authorization server (provider token exchange or renewal = provider call).
- Attribution by time window: tests are played one after the other, and any entry between the start and the end of a test is attributed to it, whether or not it carries its identifier.
- Positive check: T01 must produce at least one entry, for the right account. Without it, the log is deemed mute, no refusal is counted « réussi », and the acceptance run stops (SCR-01.error).
- Refusal tests: T02 to T09, T12 (after the cut), T13. For T01, T10 and T16, the log is read for the provider account called.
- The log must be exhaustive (BR-013): the rule does not hold if a GET, a DELETE or an unknown path escapes the log.
- With two relayed upstreams (F-002), the upstream log of a window is the union of the bench upstream's log and that of the front placed in front of the Gitea MCP server.
- With an authorization server (Go POC), T03 and T04 join the refusal tests.

#### BR-003 · No secret in the evidence (C3)

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-003.md`](../business-rules/BR-003.md).
> The content below reflects the state of this BR at the time of feature F-002 (v2).

**Rule** — No retained artifact (committed file, evidence, log, report) contains a token or a secret; a positive scan blocks the commit.

**Clarifications**
- Scanned population: the secrets generated by the bench, ContextForge's configuration secrets, and any token received by the runner or issued by ContextForge during the session (read by the bench), not a fixed list of patterns.
- The scan precedes every commit, including early commits (decision F), not only the last one.
- Evidence about a token shows its decoded claims (`aud`, `iss`, `exp`), never the compact form or the signature.
- The block is a pre-commit hook (`poc/isolation-bench/hooks/pre-commit`, which runs `secrets_guard.py scan`), installed by the bench through `git config core.hooksPath`; each launch of the gateway target checks that a fake secret from the population makes `git commit` fail without erasing the file.
- Go POC population: secrets generated by the bench (including the administration token and the Gitea secret), the gateway's signing key, the disposable Gitea token, tokens issued or forged during the session; the operator runbooks are scanned.

#### BR-005 · Bench reproducible in one command (C3)

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-005.md`](../business-rules/BR-005.md).
> The content below reflects the state of this BR at the time of feature F-002 (v2).

**Rule** — A single command builds the bench from zero and plays the acceptance run; versions are pinned, ContextForge by digest.

**Clarifications**
- Go POC target: `./bench.sh --target gateway`; Go build base, Debian final image, Python, Gitea and the Gitea MCP server pinned by digest, Go modules by `go.sum`; the bench is the same.

#### BR-006 · The acceptance run knows the gateway only through HTTP/OAuth (C3)

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-006.md`](../business-rules/BR-006.md).
> The content below reflects the state of this BR at the time of feature F-002 (v2).

**Rule** — The runner's tests reach the gateway only through HTTP/OAuth, with no dependency on ContextForge, so that they can be reused without modification against the Go POC (#5) and against v1.

**Clarifications**
- The administration actions that some tests require (provisioning, account addition T10, revocation T12, failure T13) go through a gateway adapter, the only ContextForge-specific component, replaced for the Go POC.
- Identities (U1/U2 here, role tokens for the transposed gateway) are runner parameters.
- The rule targets the absence of dependency on the product: only the adapter changes from one product to the other (`adapter_contextforge.py`, `adapter_gateway.py`).
- The runner additions of F-002 (T03/T04 branches with an authorization server, BR-014 variants, union of two logs, requirements added to T14 against the gateway, case-insensitive reading of the challenge) depart from "without modification" read literally: accepted gap (`warning` rule), listed in the POC report with the per-test comparability.

### BR-012 · The acceptance run is transposed to the role, 16 tests

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-012.md`](../business-rules/BR-012.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `blocking` · Candidate state: `verified` — the runner declares 16 tests T01..T16 and the transposition table is a file read by the runner; a test without a mapping line makes startup fail.*

**Rule** — Against the gateway, the acceptance run plays the review's 16 tests read at the role level according to the table of the gateway specification (role tokens A and B in place of U1/U2 and the profiles), without removing, merging or rewriting a single one, including the failure test (T13); a test whose function the POC does not implement is « échoué » and becomes a costed gap, never « non jouable »: against the POC, « non jouable » (BR-009) exists only for a function that §Out of Scope excludes, and no function of T01..T16 is excluded. A badly transposed test (without a mapping line) stops startup.

**Clarifications**
- Identities (roles A and B, tokens) are runner parameters; the mapping lives in the adapter and in a readable mapping file, not in the tests.
- The tests address the bench's instrumented upstream relayed by the gateway (witness accounts `witness-1` for A, `witness-2` for B): this is the one the runner knows how to read (`recette.Upstream`, tools `whoami`, `flaky`, `hidden_admin`, adjustable lifetime of T11).
- Reason for refusing « non jouable »: the POC is the product being built; leaving a function unwritten would take it out of the verdict (BR-023) without evidence that Go allows it.
- T03 and T04 are played with their "authorization server present" branch: refusal at the authorization server for A→resource B, no token B issued; without `resource` or with A+B, refusal according to the policy.

**Example**
- **Given** an access token of role A
- **When** it is presented at the access point of role B
- **Then** 401 `invalid_token` and no entry in the upstream log within the test's window (BR-002, BR-013).

### BR-013 · The upstream log traces every request by construction

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-013.md`](../business-rules/BR-013.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `blocking` · Candidate state: `verified` — control test: a GET request, a DELETE and one on an unknown path each produce an entry.*

**Rule** — The instrumented upstream's log records every request received, whatever its method, path or result (catch-all), so that the absence of an entry proves the absence of a call; the bench runs this check before the first test, and an untraced request stops it.

**Clarifications**
- Fixes N4 (review of PR #14): the F-001 log does not trace GETs other than `/authorize` (including metadata and an SSE-stream GET `/mcp`), DELETEs, unknown paths (404), or an introspection refused before logging (`upstream/server.py`, `do_GET`, `do_DELETE`, `do_POST`).
- Two logs: that of the bench upstream (authorization server and MCP server, read through the control port) and that of the front placed between the gateway and the Gitea MCP server, since the latter cannot be instrumented; the front exposes the same reading (`/now`, `/journal`). The exhaustiveness check covers both; a call from the Gitea MCP server to the Gitea instance necessarily follows a call logged at the front and is not logged.
- The log contains no token or secret, only the truncated fingerprint of the token received (BR-003, BR-022).

### BR-014 · A token forgery changes only one property

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-014.md`](../business-rules/BR-014.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `strict` · Candidate state: `verified` — control per variant: the variant and its reference token differ by exactly one property.*

**Rule** — Each forged-token variant of T05 and T06 differs from the valid reference token by exactly one property, and the acceptance run contains at least one token of a type other than access (renewal, identity) carrying its own `jti`; a wrongly accepted token is a failure of the gateway, not of the test.

**Clarifications**
- Fixes N9: in F-001, E-07 had been played only with a known access token `jti`.
- The current "ID token" variant of T06 changes `token_use` **and** `scopes`: it is rewritten (one property); the other existing variants stay as they are and the new ones are added (H6).
- Minimal variants: altered signature, other issuer, past expiry, double audience, type `renouvellement`, type `identité`, unknown `jti` — each isolated by a check that makes it alone responsible for the refusal.

### BR-015 · The Gitea relay works end to end

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-015.md`](../business-rules/BR-015.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `blocking` · Candidate state: `verified` — end-to-end test: an authorized call from role A reaches the disposable instance; the hidden tool of role B is absent from the list and refused on call.*

**Rule** — A client holding a role token obtains, through the gateway, the list of that role's tools (prefixed names, hidden tools absent) and the result of an authorized call, served by the disposable Gitea MCP server; a call to a hidden tool is refused before any upstream call.

**Clarifications**
- One read call and one write call open, on a disposable repository and ticket; the write is open only for a role A tool.
- "Before any upstream call" is read on the front's log (BR-013): no entry in the refused call's window.
- The Gitea MCP server 1.11.0 is served over HTTP (H3, verified); if it groups operations in a tool with a `method` argument (`issue_write`), the hidden tool is the whole tool. Fallback (H3) only if the relay fails; it does not waive the evidence and becomes a gap.

### BR-016 · No contact with the real Gitea

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-016.md`](../business-rules/BR-016.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `strict` · Candidate state: `verified` — scan of the bench artifacts and configuration for the real host; the bench network has no outbound path to it.*

**Rule** — The relay targets only the Gitea instance created by the bench; neither the host of the project's Gitea, nor the session's `gitea` MCP server, nor a token of that Gitea appears in the bench's configuration, evidence or logs; a pattern found makes the scan fail, which blocks the commit (BR-003).

**Clarifications**
- The Gitea instance, the Gitea MCP server and the front are on an internal Docker network (no outbound); the gateway and the bench upstream are joined to it and published on `127.0.0.1` for the runner, which a purely internal network forbids. The gateway's protection is its configuration, which the scan reads.
- The Gitea MCP server targets `https://gitea.com` by default: `-H` (or `GITEA_HOST`) toward the disposable instance is mandatory, and its lack of network egress makes a configuration error harmless.
- The two containers of the session's `gitea` MCP server (image `gitea-mcp-server:1.11.0`, same dockerd) are neither reused, nor joined to a bench network, nor stopped.
- The scan reads the forge host and `gitea.com` as forbidden patterns, in addition to the BR-003 population.

### BR-017 · Protocol versions are declared and measured

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-017.md`](../business-rules/BR-017.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `warning` · Candidate state: `verified` — T15: one negotiation per version it plays; an unknown version receives a refusal that lists the supported versions.*

**Rule** — The gateway declares the set of MCP versions it serves on input, and an undeclared version receives an explicit refusal that names the served set (`error.data.supported`, the form T15 reads); a version that T15 plays and that the gateway does not serve makes T15 fail and becomes a costed gap.

**Clarifications**
- Target set (H2): the one T15 plays as is, 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 (with session) and 2026-07-28 (without session); the set actually served is the one the `go-sdk` allows.
- On the upstream side, the measurement covers one upstream per generation: the bench upstream (with session) and the Gitea MCP server (without session, H3); it is reported in the go / no-go table, with no rule of its own.

### BR-018 · The go / no-go table has criteria written in advance

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-018.md`](../business-rules/BR-018.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `blocking` · Candidate state: `verified` — `report_check.py` extended: the table has one row per hypothesis, one criterion and one expected evidence per row; the commit that introduces it precedes the `bench_commit` of the first probative result (`evidence/results.json`).*

**Rule** — Before the first probative test, the bench fixes and commits the go / no-go table: one line per POC hypothesis (acceptance run 16/16; inbound OAuth; Gitea relay; two generations; maturity of `fosite`; API of the `go-sdk`; Claude.ai and iOS; measured cost), each with its success criterion and the expected evidence; a criterion changed after the first probative result invalidates the acceptance run, like a policy (BR-011).

**Clarifications**
- The evidence of anteriority is the commit SHA recorded in the report and in `results.json`, not `git log` on `main` (unreadable after a squashed merge, learnings of 2026-10-27).

### BR-023 · POC "go" criterion

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-023.md`](../business-rules/BR-023.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `blocking` · Candidate state: `not-mechanisable` — the estimate of the remaining cost of the gaps is a judgment; `report_check.py` only checks the presence of the verdict, the isolation rows and the sum (regression guard, not the control).*

**Rule** — The verdict is "go" if the isolation lines (T02 to T09, T12, T13, T14) are « réussi » on the delivered POC, and if the sum of the estimated costs of the remaining gaps does not exceed 5 days; "no-go" otherwise; no verdict as long as a line is « non joué ».

**Clarifications**
- Isolation line: a test whose failure lets a token reach another role, an account or a tool outside its scope. In the POC, these functions are code that we write: a failure not fixed within the budget is evidence against the stack, not a gap to cost.
- Remaining gap: a test outside isolation (T01, T10, T11, T15, T16) or a hypothesis line (Gitea relay, generations, building blocks) not met, with its costed fix. Threshold on the **sum**, not per gap: ten gaps of 4 days do not make a go. 5 days = the upper bound of the POC estimate ("one more POC"), a recommended value that the operator adjusts at the freeze.
- Class of a fix (BR-004): "library" = obtained by the chosen building block (`fosite`, `go-sdk`, `x/oauth2`, `modernc.org/sqlite`) or its configuration; "in-house" = POC code in place of a building block. The class informs the weighing of the stack, it does not change the verdict.
- In-house OAuth plan B (H4) done and played by the acceptance run: not a gap of the verdict; the "maturity of fosite" line is "not met" with the measured cost of plan B.
- The "Claude.ai and iOS" line is "awaiting operator" (BR-020) and counts neither for nor against; a "go" is worded "go, Claude.ai / iOS reservation open" as long as it is.
- A "go" with gaps lists the cost of each; weighing Go against another language remains with the operator (BR-008 v2).

### BR-020 · The Claude.ai and iOS operator step is traced, never simulated, and delivered as copy-paste runbooks

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-020.md`](../business-rules/BR-020.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `blocking` · Candidate state: `not-mechanisable` — the evidence is the operator's return from their account; only the presence of the runbooks, their form (steps, blocks, expected result, fallback, report) and that of the table row can be checked (`report_check.py` extended: regression guard, not the control).*

**Rule** — The deliverable contains, for each operator path (Claude.ai web, Claude iOS), a copy-paste runbook in `docs/runbooks/` (one file per path: the operator runbook of the trial, kept in the private source, the operator runbook of the trial, kept in the private source), which covers exposing the gateway over HTTPS (public URL or tunnel), adding the connector, calling `ping`, observing the token renewal and reading the verdict; each step is a command block or a value to paste, ready to use (URL, `client_id`, scopes, POC and tunnel launch commands), with the expected result, the course of action if the result differs, and reporting the result on HULL-3; as long as the operator has not reported their result, the table line stays "awaiting operator" and never becomes « réussi » on bench evidence. The report points to these runbooks.

**Clarifications**
- The session opens no tunnel or public URL, and asks for no Claude account; it writes the commands and the values, the operator runs them.
- No real token, secret or key in a runbook: a secret value is a named slot that the operator fills (BR-003, bench scan extended to the runbooks).
- A failure reported later reopens the verdict via HULL-3 if the defect lies in the OAuth implementation (not the client).

### BR-021 · The disk footprint is bounded and cleaned up

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-021.md`](../business-rules/BR-021.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `strict` · Candidate state: `verified` — the bench refuses to start under 10 GB free, measures `docker system df` before and after, and fails beyond 4 GB of growth; the teardown purges by image identifier what the POC pulled or built, and compares the image and volume lists before and after.*

**Rule** — The bench refuses to start if free space is below 10 GB, does not exceed 4 GB of added images, volumes and caches, and deletes at teardown the images pulled or built for the POC, the volumes and the build cache, without touching the images and volumes that already existed.

**Clarifications**
- Measurement of 2026-10-27 (`df -h /var/lib/docker`, `docker system df`): 22 GB free out of 197 GB (89%, shared disk); images present 114 MB (`gitea-mcp-server:1.11.0`, also tagged `latest`, 13 MB; `debian:trixie-slim`, 101 MB), no volume or build cache.
- Estimated footprint (sizes not measured, images not pulled): Go build base ≈ 0.8 GB, gateway final image < 0.05 GB, Gitea ≈ 0.2 to 0.3 GB (SQLite, no Postgres), bench upstream Python image (F-001 digest) 0.15 to 1 GB depending on its variant, Go modules and cache ≈ 0.5 to 1 GB, Gitea MCP server already present; total ≈ 2 to 3 GB, under the 4 GB ceiling.
- The list of pre-existing artifacts is read before the pull; teardown deletes only the difference, by identifier. `docker compose down --rmi all` (the `--purge-images` option of F-001) is proscribed: it would remove the `gitea-mcp-server:1.11.0` tag on which the session's two MCP server containers depend.

### BR-022 · The inbound token never crosses to the upstream

> **Extracted on 2026-10-27.** Current version: [`business-rules/BR-022.md`](../business-rules/BR-022.md).
> The content below reflects the state of this BR at the time of feature F-002 (v1).

*`behavioral` · `strict` · Candidate state: `verified` — the upstream logs the fingerprint of the received token; no fingerprint of a token issued by the gateway appears there.*

**Rule** — The requests the gateway sends to the upstream carry only the token of the role's connection; the access or renewal token received from the client is never forwarded, neither in a header, nor in a parameter, nor in a body; a fingerprint of an inbound token in an upstream log is a failure of the gateway, reported as an isolation gap.

**Clarifications**
- Positive check: the upstream does receive the connection token's fingerprint for the authorized call (T01).
- The received fingerprint is an addition to the log of `upstream/server.py` (BR-013); it is read on the bench upstream. On the Gitea path, it is read on the front if the connection token goes through the request (`-oauth` mode); if it is fixed at startup of the Gitea MCP server, the front must receive no `Authorization` coming from the gateway.

## 5. Acceptance Criteria

Bench and fixes (SCR-03)
- [x] BR-013: the exhaustiveness control (GET, DELETE, unknown path) passes before the first test on both logs (bench upstream, Gitea front proxy); the `/journal` reading of the control port of each, recorded in `evidence/results.json`, shows one entry per request of the control.
- [x] BR-003: a pre-commit hook runs `secrets_guard.py scan` and refuses the commit on a positive result; `bench.sh` installs it (`git config core.hooksPath`) and says so (trial: a fake secret of the population makes `git commit` fail, without erasing it from the repository).
- [x] BR-014: each variant has its reference token and its control; the renewal-type token with its own `jti` is played.
- [x] BR-005: `./bench.sh --target gateway` builds (Go image by digest, `go.sum`) and plays the acceptance run from scratch.
- [x] BR-021: refusal under 10 GB free; measured growth ≤ 4 GB; after teardown, the list of images (identifiers and tags) and volumes equals that of before, `gitea-mcp-server:1.11.0` included.
- [x] BR-016: the scan of the forge host and `gitea.com` is negative on the committed artifacts and the configuration; `docker network inspect` shows the internal network of Gitea, the Gitea MCP server and the front proxy.

Gateway (SCR-01)
- [x] BR-015: a bench client obtains `ping`, the role A list (prefixed), a read call and a write call on the disposable Gitea instance; role B does not have the hidden tool, and its refused call leaves the front proxy log empty in its window.
- [x] BR-022: no fingerprint of a gateway token appears in the upstream logs; T01 produces the fingerprint of the connection token.
- [x] BR-017: T15, played as is, negotiates each of its five versions or the failures are costed gaps; an unknown version receives a refusal whose `error.data.supported` lists the set.
- [x] Tokens: the claims of an issued access token (`iss`, single `aud`, `exp`, `iat`, `jti`) are recorded decoded in `results.json` (BR-003); `exp - iat` equals the duration of `policy.json`.

Acceptance run (SCR-02)
- [x] BR-012: the runner declares 16 tests, the transposition table is read, T03/T04/T14 are played with the authorization server; the identities are parameters (no import of gateway code); `results.json` has no « non jouable » row.
- [x] BR-001 v2: the count of 16 is checked at load (`EXPECTED_TESTS`, unchanged).
- [x] BR-002: each refusal test reads the union of the complete upstream logs (BR-013); without an entry for T01, the acceptance run stops.
- [x] BR-006: `git diff` of the bench against F-001: only the `gateway` adapter, the T03/T04 branches (and the T14 reading if it changes), the BR-014 variants (additions, plus the rewrite of the T06 "ID token" variant), the reading of two logs and the BR-013 log change; the list is in the report with the per-test comparability (H6).
- [x] BR-018: policies (`policy.json`), gateway configuration and go / no-go table committed before the first probative result: the SHA of the commit that contains them is an ancestor of the `bench_commit` of `results.json`, recorded in the report (`git merge-base --is-ancestor`, read before the merge).

Report (SCR-04)
- [x] BR-004 v2: 16 rows, no « non joué » in the final; each failure carries a gap (cause, fix classed "library" or "in-house", days); `report_check.py --target gateway` passes.
- [x] BR-018: the table has one row per hypothesis, one criterion and one evidence per row.
- [x] BR-023: the verdict cites the state of the eleven isolation rows and the sum of the costs of the remaining gaps against the 5-day threshold; a "go" carries the mention "Claude.ai / iOS reservation open"; the estimated fixability is read by hand in PR review.
- [x] BR-020: the operator runbook of the trial, kept in the private source and the operator runbook of the trial, kept in the private source exist; each step carries a block to paste (URL, `client_id`, scopes, launch commands of the POC and the tunnel), the expected result, the course of action in case of gap and the report on HULL-3; no real secret value; the report points to them and the Claude.ai/iOS row is "awaiting operator".
- [x] BR-008 v2: the PR diff does not touch `project.stack` (`check_scope.py`).
- [x] Measured cost (day-equivalents, lines) set against 3 to 5 days / 8,000 to 15,000 lines and the 31.5 days of ContextForge.
- [x] BR-010 (F-001): after teardown, no bench secret remains; no real secret enters the bench.

## 6. Diagrams

### Selection Matrix

| Type | Decision |
|------|----------|
| Userflow | Omitted: no user journey in the POC; the operator procedure (BR-020) is a list of steps in the report. |
| Sequence | Included (6.2): client, gateway, authorization server, Gitea upstream. |
| State machine | Omitted: the token states (active, renewed, revoked) are those of OAuth, objects of the acceptance run; no component with its own states is specified. |
| ER / Class | Omitted: minimal SQLite schema (roles, tokens), disposable, not specified. |

### 6.2 Sequence — authorized call relayed to Gitea (BR-015), then refusal (T02)

The same diagram holds for the bench upstream (T01): `U` is then `upstream/server.py`, the connection token is that of the witness account, obtained and renewed by outbound OAuth.

```mermaid
sequenceDiagram
    participant C as Client de banc
    participant G as Passerelle (Go)
    participant U as Frontale Gitea + journal
    C->>G: GET .well-known (rôle A)
    G-->>C: métadonnées de la ressource A
    C->>G: /authorize + PKCE, resource=A
    G-->>C: code ; /token -> jeton d'accès (aud=A) + renouvellement
    C->>G: tools/call gitea_list_issues (jeton A)
    G->>U: appel avec le jeton de la connexion (BR-022)
    U-->>G: résultat
    G-->>C: résultat
    C->>G: tools/call sur le point d'accès B (jeton A)
    G-->>C: 401 invalid_token
    Note over G,U: aucune entrée au journal (BR-002, BR-013)
```

## 7. States & Captures

- **Freeze marker**: not applicable
- **Package-status**: absent

## 8. Design & Prototypes

Not applicable: no interface.

## 9. Implementation Notes

### Dependencies (prerequisite features)

- **HULL-2 / F-001** — ContextForge trial (implemented): no-go verdict that opens this POC; provides the bench, BR-001..011.
- **HULL-3** — Behavior of the Claude clients (Backlog, open): carries the Claude.ai / iOS part and the public HTTPS URL (BR-020); **planned, non-blocking**.
- **HULL-4** — Official server matrix (open): no dependency.
- Downstream: HULL-6 (foundation) reads the verdict; HULL-7 (core) may or may not take over the code (BR-008 v2).
- Tools: docker of the session's dockerd (available); Go toolchain **in a container** (no installation on the host); the session's `gitea` MCP connector not used by the bench (BR-016).

### Technical Constraints

- Go, `ory/fosite`, MCP `go-sdk`, cgo-free SQLite (`modernc.org/sqlite`), as retained by the specification; versions and digests pinned (BR-005).
- Everything in containers in the session's dockerd (sysbox mode: services joined by published `localhost:port`); internal bench network (BR-016).
- Disk: 22 GB free (measured on 2026-10-27), growth ceiling 4 GB, startup refused under 10 GB (BR-021).
- The session's dockerd runs the session's two MCP servers (`gitea-mcp-server:1.11.0`): the bench does not touch them (BR-016, BR-021).
- Disposable test secrets generated in the bench and destroyed at teardown (BR-010 of F-001); POC signing key generated at launch.
- No action toward `prod-gateway`, no tunnel, no token displayed or committed.

### Architecture Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| H1. Claude.ai / iOS in scope? | No, operator step attached to HULL-3 (BR-020); verdict rendered with the worded reservation (BR-023) | Requires an account, a connector and public HTTPS. The specification says "independent of the language" only of Claude's behavior and the server matrix, not of inbound OAuth: the reservation stays open, not erased. |
| H2. Which input versions? | Those that T15 plays (four with a session, one without), subject to the SDK | Same comparison as F-001; a failure is a costed gap, outside the isolation rows. |
| H3. Which Gitea upstream? | Gitea MCP server 1.11.0 over HTTP (verified, sessionless) behind a logging front proxy, facing a disposable Gitea instance | Only a front proxy makes the log exhaustive without modifying the product (BR-013); it provides the sessionless upstream. |
| Subject of the acceptance run | The bench's instrumented upstream, relayed with outbound OAuth to its authorization server | `recette.py` can read only it (witness accounts, `flaky`, adjustable lifetime of T11); without it, T01, T07 to T11, T13, T15 and T16 cannot be played. |
| H4. OAuth plan B? | Allowed in the POC; not a gap of the verdict if built and played (BR-023) | The specification names this plan B; the POC must measure its cost rather than avoid it. |
| H5. Effort? | Reference point 3 to 5 d; exit at 8 day-eq. without isolation rows played: partial report, verdict not rendered | The token budget bounds the POC (learnings 2026-10-27); the exit fixes what to render. |
| H6. Replay ContextForge? | No; variants added without modifying the existing ones (except T06 "ID token"), per-test comparability in the report | Out of scope; the F-001 report remains valid for its versions. |
| Verdict threshold | Isolation rows successful; sum of remaining gaps ≤ 5 d | Recommended value (upper bound of the POC); the operator adjusts at the freeze. A per-gap threshold without a sum let N gaps through. |
| Access tokens | Signed JWTs (RFC 9068) | The bench reads the claims and forges JWTs; opaque tokens would empty T05 and T06. |
| `capabilities` | `extends: [Recette d'isolation de passerelle MCP]`, `introduces: []` | The rules touched (BR-001 to 006) are those of the acceptance run; the rules introduced judge a disposable POC, without a durable product capability. To be confirmed at the freeze. |
| Where does the code go? | `poc/go-gateway/`, disposable | Separate from the bench; no promise of carrying it over (BR-008 v2). |

### Data Model Impact

- Migration: no
- Disposable POC SQLite (roles, tokens, connections); deleted at teardown.

### Components to Create / Modify

**Created**
- `poc/go-gateway/` — POC gateway (Go), Dockerfile, `go.mod`/`go.sum`
- `poc/isolation-bench/runner/adapter_gateway.py` — bench adapter for the gateway
- `poc/isolation-bench/mapping.json` — role transposition table
- `poc/isolation-bench/gateway.compose.yaml` — gateway, bench upstream, disposable Gitea, Gitea MCP server, logging front proxy, internal network (BR-016)
- `poc/isolation-bench/front/` — logging front proxy (catch-all, token fingerprint, `/now` and `/journal` reading like the upstream)
- `poc/isolation-bench/hooks/pre-commit` — blocking by `secrets_guard.py`
- `docs/trials/poc-go.md` — report and go / no-go table
- the operator runbook of the trial, kept in the private source, the operator runbook of the trial, kept in the private source — copy-paste operator runbooks (BR-020)

**Modified**
- `poc/isolation-bench/upstream/server.py` — catch-all (N4), fingerprint of the received token (BR-022)
- `poc/isolation-bench/runner/recette.py` — T03/T04 branches, BR-014 variants, window over two logs (BR-002)
- `poc/isolation-bench/bench.sh`, `runner/run_bench.py` — `--target` option, threshold and disk measurement, cleanup by difference (without `--rmi all`), hook installation
- `poc/isolation-bench/runner/report_check.py` — classes per target (BR-004 v2), table (BR-018), verdict (BR-023)
- `poc/isolation-bench/runner/secrets_guard.py` — forbidden host patterns (BR-016)
- `poc/isolation-bench/README.md` — usage guide
- `business-rules/BR-001.md`, `BR-004.md`, `BR-008.md` (v2); `BR-002.md`, `BR-003.md`, `BR-005.md`, `BR-006.md` (clarifications) — at closure

### Freeze decisions (operator, 2026-10-27)

- BR-023 threshold of 5 d: **kept**, confirmed by the operator.
- BR-019 abandoned: BR-008 v2 (see §4); number 019 never reassigned.
- Copy-paste runbooks for each operator step (BR-020): requested by the operator to limit their manipulations.
- Capability map: `capability-map.md` does not exist yet; it is created at `close-feature` Phase 3 (step 12a) of F-002 and records F-001 ("MCP gateway isolation acceptance run") and F-002 (`extends`).

### Pitfalls Discovered

- `fosite` v0.49.0 does not know `resource` (RFC 8707): translated into `audience` at `/authorize`; at renewal it takes the original audience without comparing the parameter — without a gateway check, T03 issued a token for B. The check must precede `NewAccessResponse`, otherwise a refusal already rotates the renewal token.
- Stateless JWT access tokens: `fosite` revocation and rotation do not touch them; `revStore` propagates the revocation to the `jti`s, also overriding `RotateRefreshToken` (the `MemoryStore` revokes through an internal call that bypasses the wrapper).
- `JWTSession.Subject` is distinct from `JWTClaims.Subject`: setting only the latter leaves `GetSubject()` empty and silently disables any check that uses it (role and vault check at `/token`, found by the operator-mode trial, outside the acceptance run).
- The session cloned at renewal keeps `iat` (and `fosite` rounds `exp`): `iat`, `exp` and `jti` are reset at each issuance.
- The `fosite` PKCE handler deletes its session upon validation: a code refused for its resource is burned (stricter than necessary, accepted).
- `go-sdk` v1.4.0 does not serve 2026-07-28: the MCP entry point is written by hand (gap E-01 of the report).
- `docker images` without `-a` lists neither images pulled by digest nor builder intermediates: a disk check based on it wrongly concludes "nothing remains".
- An HTTP health probe toward the upstream is logged (BR-013) and pollutes the test windows: TCP probe.
- Go `ServeMux` redirects the root of a subtree pattern (`/.well-known/oauth-protected-resource`) with a 301: exact pattern added (404).

## 10. Phases

### Phase 1 — Fixed bench and skeleton

- [x] N4 (BR-013), N6 (hook, BR-003), N9 (BR-014) done and checked on the upstream alone
- [x] Disk: threshold, measurement, bounded teardown (BR-021)
- [x] go / no-go table, policies and report skeleton committed (BR-018, BR-023 criterion)

### Phase 2 — Gateway and OAuth

- [x] Two access points, authorization server, `ping`, renewal, revocation
- [x] Relay of the bench upstream with outbound OAuth (two witness accounts, renewal)
- [x] T01..T14 playable; first rows of the report

### Phase 3 — Relay and generations

- [x] End-to-end Gitea relay, hidden tool (BR-015, BR-016, BR-022)
- [x] Two generations (BR-017), T15 and T16

### Phase 4 — Complete acceptance run, report

- [x] 16 tests played, gaps documented, secrets scan, teardown, measured cost
- [x] Operator runbooks `docs/runbooks/` (BR-020), verdict (BR-023), `project.stack` intact (BR-008 v2)
- [x] Closure reminder: `capability-map.md` created at the closure of F-002, with the capabilities of F-001 and F-002 (see §9)

## 11. Tests

### Isolation acceptance run

| Test | BR / Concern | File |
|------|-------------|------|
| T01..T16 against the gateway; count 16 | BR-012 | `poc/isolation-bench/runner/recette.py` |
| log read in each refusal test; T01 positive control | BR-002 | `poc/isolation-bench/runner/recette.py` |
| token variants, one property each | BR-014 | `poc/isolation-bench/runner/recette.py` |
| protocol versions (T15) | BR-017 | `poc/isolation-bench/runner/recette.py` |

### Bench controls

| Test | BR | File |
|------|-----|------|
| log exhaustiveness (GET, DELETE, unknown) | BR-013 | `poc/isolation-bench/runner/run_bench.py` |
| Gitea end to end; hidden tool | BR-015 | `poc/isolation-bench/runner/recette.py` |
| scan for the real host | BR-016 | `poc/isolation-bench/runner/secrets_guard.py` |
| incoming token absent from the upstream | BR-022 | `poc/isolation-bench/runner/recette.py` |
| blocking pre-commit hook | BR-003 | `poc/isolation-bench/hooks/pre-commit` |
| disk threshold and growth, teardown | BR-021 | `poc/isolation-bench/runner/run_bench.py` |
| diff without `project.stack` | BR-008 v2 | `poc/isolation-bench/runner/check_scope.py` |

| table committed before the first probative result | BR-018 | `poc/isolation-bench/runner/report_check.py` |
| gap classes per target | BR-004 | `poc/isolation-bench/runner/report_check.py` |

BR-023 (fixability estimate) and BR-020 (operator return) are not tested mechanically; the presence of the verdict, the isolation rows, the sum, the two runbooks and their sections (block to paste, expected result, course of action if different, report on HULL-3) is checked by `report_check.py` extended (regression guard); the detection of secret values in the runbooks falls to `secrets_guard.py`.

## 12. Risks & Watchpoints

- **Technical risk**: maturity of `fosite` (audience per access point, limited renewal) and the `go-sdk` API for the relay, hypotheses of the specification; in-house plan B allowed (H4), its cost enters the table.
- **Technical risk**: the Gitea MCP server (HTTP verified, H3) groups operations by argument (`method`) and perhaps reads the per-request token only in `-oauth` mode; fallback on an upstream imitating two tools, gap noted.
- **Planning risk**: 3 to 5 d estimated, 8 day-eq. exit (H5); the scope counts two upstreams and an outbound OAuth client (simultaneous renewal, T11, the ContextForge gap E-05) in addition to the bench fixes (N4, N6, N9): risk accepted at the stress test, the exit of H5 fixes what to render.
- **Dependency risk**: the completed acceptance run (H6) loses strict comparability with F-001 for T03 to T06; mitigation: variants added without modifying the existing ones (except T06 "ID token"), per-test comparability in the report, BR-006 (`warning`) set aside knowingly.
- **Privacy / security risk**: POC tokens and signing key in the evidence; BR-003 with a blocking hook; the real Gitea is never reached (BR-016).
- **Resource risk**: 22 GB free on a shared disk (BR-021); a Go build without a named cache or a forgotten image exceeds the ceiling; a purge by tag would break the session's MCP server. BR-021 keeps its three thresholds in one rule (one object: the bench's disk footprint); a control that reads only one of them does not verify it.
- **Functional risk**: language "go" pronounced without having played Claude.ai / iOS; reservation worded in the verdict (BR-023), reopened by HULL-3 if the defect lies in the POC's OAuth (BR-020).
- **Rule overlap**: settled by the operator at the freeze (2026-10-27): no new rule for the POC, BR-008 goes to v2 and widens its subject to any stack-choice trial or POC; number 019 is abandoned (see §4).

## Stress-test

**Passe du 2026-10-27** — Claude Opus 5.5 (product, BR review sub-agent delegated by the operator, blank context), six axes; corrections applied by delegation from the operator.
**25 findings — 3 critical, 10 major, 12 minor ; 21 retenus, 4 écartés.**

Critical:
- **S1** · axis F · §2, §3.1 vs `recette.py` — the acceptance run reads only the bench upstream (witness accounts, `whoami`, `flaky`, `hidden_admin`, adjustable lifetime of T11, control-port log); the spec relayed only Gitea and took outbound OAuth out of scope: T01, T07 to T11, T13, T15 and T16 were unplayable, 16/16 unreachable. Sort : corrigé — the bench upstream enters the scope as the subject of the acceptance run, with outbound OAuth to its authorization server (`x/oauth2`); Gitea remains judged by BR-015.
- **S2** · axis B · §BR-012 — against a POC, « non jouable » meant "not written": with the verdict criterion, a hard function left aside dropped out of the judgment. Sort : corrigé — a function of the scope that is not implemented is « échouée » with a gap; « non jouable » reserved for §Out of Scope.
- **S3** · axis F · §BR-018 — 5 d threshold **per** gap without a sum (ten gaps of 4 d gave "go"); the rule forbade an in-house fix replacing an isolation function while its clarification allowed the in-house plan B, and in the POC every isolation function is in-house. Sort : corrigé — BR-023: isolation rows (T02 to T09, T12 to T14) successful and sum of remaining gaps ≤ 5 d; classes "library" / "in-house" defined; plan B built and played outside the gap.

Majors:
- **S4** · axis F · §BR-002, §BR-013 — the test window reads a single log (`recette.Upstream`); that of the Gitea front proxy was read by nothing, a refusal leaking to Gitea went unnoticed. Sort : corrigé — union of the two logs, exhaustiveness control on both, clarification of BR-002.
- **S5** · axis F · §BR-016 vs §9 — a merely internal Docker network publishes no port, yet the runner joins the services through `127.0.0.1`; the Gitea MCP server targets `https://gitea.com` by default. Sort : corrigé — internal network for Gitea, Gitea MCP server and front proxy; gateway and upstream joined and published; `-H` mandatory; `gitea.com` forbidden to the scan.
- **S6** · axis B · §H3 — unverified hypothesis. Verified: `gitea-mcp` 1.11.0 serves over HTTP (`-t http`, `/mcp`, 8080), sessionless, up to 2026-07-28; static token or per-request in `-oauth`. The BR-017 clarification ("Gitea upstream with a session") was false. Sort : corrigé — H3, BR-015, BR-017, §2 ("one upstream per generation": bench with a session, Gitea without).
- **S7** · axis F · §BR-017 vs T15 — T15 requires four versions with a session and one without; BR-017 said "at least one per generation". Sort : corrigé — T15 played as is; a version not served is a costed gap, outside the isolation rows.
- **S8** · axis F · §BR-004 — set aside "by reference", but it requires the "overlay / in-core" classing that `report_check.py` checks; the POC classes "library / in-house". Sort : corrigé — BR-004 v2 (C2), classes according to the target.
- **S9** · axis A · §H1 — diverted quotation: "do not depend on the language" targets Claude's behavior and the server matrix, not inbound OAuth from Claude.ai / iOS, the first criterion of the POC and an output of the ticket. Sort : corrigé — justification rewritten; verdict worded "go, Claude.ai / iOS reservation open" (BR-023), reopening by HULL-3 (BR-020).
- **S10** · axis F · §4 overlap, BR-001 — clarification impossible: v1 requires "U1/U2 as the review writes them", a transposed acceptance run did not satisfy it (C2 before C4). Sort : corrigé — BR-001 v2.
- **S11** · axis A · §3.1 — token format unstated; `run_bench.py` reads `exp - iat` and the adapter forges JWTs: opaque tokens emptied T05, T06 and BR-014. Sort : corrigé — signed JWTs (RFC 9068), test key read by the adapter, criterion in §5.
- **S12** · axis B · §BR-021 — `--rmi all` of F-001 would remove the `gitea-mcp-server:1.11.0` tag from the session's two MCP containers; template without the bench upstream's Python image, with a needless Postgres; figures not measured. Sort : corrigé — purge by identifier and difference, criterion on tags, measurement of 2026-10-27 and template 2 to 3 GB (estimated, images not pulled).
- **S13** · axis F · §H6 — comparability with F-001: the T06 "ID token" variant changes two properties, BR-014 forces its rewrite; nothing said whether the other variants were replaced. Sort : corrigé — variants added without modifying the existing ones (except this one), T03 to T06 marked "non-strict comparison" in the report.

Minors:
- **S14** · axis B · §H5 — "critical rows" undefined, exit verdict unstated. Sort : corrigé — isolation rows of BR-023, SCR-04.partial, verdict "not rendered".
- **S15** · axis A · §5 BR-013 — "`grep` of the log": the log is in memory, read through the control port. Sort : corrigé — `/journal` reading recorded in `results.json`.
- **S16** · axis A · §5 policies — `git log` unreadable after a squashed merge (learnings of 2026-10-27). Sort : corrigé — SHA ancestor of the `bench_commit`, recorded in the report.
- **S17** · axis A · §BR-018 — two obligations (table fixed in advance; verdict criterion). Sort : corrigé — split into BR-018 (`verified`) and BR-023 (`not-mechanisable`).
- **S18** · axis A · §BR-021 — three thresholds in one rule. Sort : accepté — one object (the bench's disk footprint), enumeration of its parts; noted in §12.
- **S19** · axis A · §5 BR-003 — a versioned hook does not act without `core.hooksPath`. Sort : corrigé — `bench.sh` installs it, criterion adjusted.
- **S20** · axis B · §BR-022 — `server.py` does not log the token fingerprint; on Gitea, a static token makes the control empty. Sort : corrigé — addition to the log, reading by path.
- **S21** · axis D · §Out of Scope — outbound OAuth to the bench alone now included; upstream registration, performance, multi-architecture not excluded. Sort : différé — lines added in §Out of Scope.
- **S22** · axis E · §4, BR-019 — duplicate of BR-008 except for the subject ("the trial"); a v2 of BR-008 would avoid a rule. Sort : rejeté — operator arbitration (stack decision), flagged in §12; settled at the freeze: BR-008 v2.
- **S23** · axis C · §12 — the corrected scope (two upstreams, outbound OAuth, T11) likely exceeds 3 to 5 d. Sort : accepté — nothing to remove without losing 16/16; the exit of H5 bounds it; risk in §12.
- **S24** · axis F · §BR-006 — the runner additions depart from "without modification" read literally; the clarification cannot absolve it. Sort : accepté — `warning` rule, free gap, list and comparability in the report; risk in §12.
- **S25** · axis A · §BR-016, §BR-019, §BR-022 — `behavioral` rules with no response to their violation. Sort : corrigé — response added to each `Rule`.

## 13. Changelog

| Date | Author | Change |
|------|--------|--------|
| 2026-10-27 | Claude (product) | Spec creation (draft) |
| 2026-10-27 | Claude (product, stress-test) | Stress-test: bench upstream in scope, BR-018 split (BR-023), BR-001 and BR-004 to v2, H3 verified, BR-013/016/017/021/022 clarified |
| 2026-10-27 | Claude (product, freeze) | Freeze on operator decisions: BR-019 abandoned in favor of BR-008 v2; 5 d threshold of BR-023 kept; BR-020 requires copy-paste runbooks (`docs/runbooks/`); capability map created at closure; `ready-for-dev` |
| 2026-10-27 | Claude (implementation) | Implemented: acceptance run 16/16 (bench `9f8f2eb`), verdict "go, Claude.ai / iOS reservation open"; BR-012..018, 020..023 extracted; BR-001, 004, 008 to v2; BR-002, 003, 005, 006 clarified (v2); `capability-map.md` created; `implemented` |
| 2026-11-16 | Codex (implementation-coder, model:gpt-6-astra) | F-022 slice 1, freeze `10d166e`: S11 guide references, P1 private trial references and F1 private procedure mentions only where applicable. |
| 2026-11-16 | Codex (implementation-coder, model:gpt-6-astra) | F-022 slice 2, freeze `10d166e` (retouch 2 `7aac9e6`): §3.2 substitutions only; inline rule clarifications without version where applicable. |
