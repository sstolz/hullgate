# Capability Map — hullgate

> What the project does today. The rules that hold it live in `business-rules/`,
> the history of how it got there in `features/index.md`.

**Last sync**: 2026-11-18 · F-024
**Coverage**: partial

## core

How the v1 gateway lays out its MCP access points, issues and judges the tokens that give access to them, keeps a trace of it, and spells the names others depend on.

| Capability | What it does | Rules | Introduced | Last touched |
|---|---|---|---|---|
| **Access point tree** | Organizes access points in an inheriting tree, with scoped tokens, settings, locks and export. | `BR-044`, `BR-045`, `BR-046`, `BR-047`, `BR-048`, `BR-049`, `BR-070`, `BR-160` | `F-005` | `F-020` |
| **Operator tokens** | Issues through the CLI opaque tokens placed on a node, shown once, kept as a fingerprint, with a capped lifetime in sensitive environments, valid for the node and its subtree; the database keeps no bearer secret in clear text. | `BR-050`, `BR-051`, `BR-052`, `BR-053`, `BR-072` | `F-005` | `F-016` |
| **Authorization order** | Judges each request of an access point in a fixed order — path under the instance's own identifier, vault and key, token, revocation, range, sensitivity, scope — and refuses by default on failure (503). | `BR-055`, `BR-064`, `BR-065`, `BR-161` | `F-005` | `F-014` |
| **MCP access point of a role** | Serves stateless MCP in the five protocol versions behind the authorization order, and serves the relay's tools; no HTTP administration route. | `BR-067`, `BR-068` | `F-005` | `F-016` |
| **Audit log** | Records acts and refusals without secrets, coalesces limit refusals, and retains authentic-credential refusals with their client address. | `BR-066`, `BR-122`, `BR-312`, `BR-313` | `F-005` | `F-020` |
| **Per-access-point authorization server** | Serves OAuth per role with a TOTP or usable passkey barrier, admitted clients and rotating tokens; host approval lets one pending request pass a closed barrier. | `BR-054`, `BR-056`, `BR-057`, `BR-058`, `BR-059`, `BR-060`, `BR-061`, `BR-062`, `BR-063`, `BR-073`, `BR-310`, `BR-316` | `F-005` | `F-024` |
| **English contract vocabulary** | Every closed vocabulary a client, script or runbook reads (CLI, JSON and YAML keys, wire and audit values, make targets, infrastructure names, the identifiers of data at rest) is English, under the product name hullgate. | `BR-131`, `BR-132` | `F-010` | `F-016` |

## operations

How the v1 gateway is built, verified before merge and against the isolation acceptance run, deployed on the production server behind Traefik and backed up.

| Capability | What it does | Rules | Introduced | Last touched |
|---|---|---|---|---|
| **Gateway image** | Builds from a single `Dockerfile` an amd64 and arm64 image, pinned bases and dependencies from the lock alone, non-root, read-only except `/data`, configured by environment variables and mounted secrets (including the connections key), schema migrated at startup, with a public listener (probes `/healthz`, `/readyz` "key loaded") and a separate administration listener serving the console bundle, the image holding no Node. | `BR-028`, `BR-029`, `BR-030`, `BR-031`, `BR-032`, `BR-033`, `BR-034`, `BR-069`, `BR-212` | `F-004` | `F-016` |
| **v1 quality gate** | Judges a branch with lock, format, lint, strict typing, tests and SDK checks; rejects operator infrastructure outside the private workshop, and reports the result as `GATE: OK` or a failing step. | `BR-035`, `BR-101`, `BR-325` | `F-004` | `F-022` |
| **Quality analysis tools** | Runs pinned tools with declared roles and QA verdicts: coverage, dependency audits, optional environment-configured SonarQube and a blocking secret scan with an exact fingerprint allowlist. | `BR-098`, `BR-099`, `BR-100`, `BR-102`, `BR-103`, `BR-104`, `BR-105`, `BR-326` | `F-007` | `F-022` |
| **mcp SDK version guard** | Refuses at the gate any version of `mcp` or `mcp-types` that is not pinned or that the `v1` target of the isolation bench has not replayed in full (16 tests and all checks passed, N3 and nominal `/revoke` harnesses included). | `BR-036`, `BR-037` | `F-004` | `F-009` |
| **Deployment behind Traefik** | Deploys a reference behind existing Traefik using generic guides and named parameters, with health checks and rollback; administration stays on loopback with an optional public route. | `BR-040`, `BR-041`, `BR-042`, `BR-043`, `BR-230`, `BR-328`, `BR-329` | `F-004` | `F-022` |
| **Public listener rate limiting** | Bounds tool calls, OAuth traffic, registrations, unknown-credential audit writes, CIMD fetches and request bodies per token, connection or source. | `BR-303`, `BR-304`, `BR-305`, `BR-306`, `BR-307`, `BR-308`, `BR-309`, `BR-311`, `BR-314` | `F-020` | `F-020` |
| **Trusted client address** | Reads the client address through declared proxies; production requires proxy trust and the smoke proves forged headers cannot merge client budgets. | `BR-300`, `BR-301`, `BR-302`, `BR-315` | `F-020` | `F-020` |
| **Backup and restore check** | Backs up SQLite hot, verifies and rotates backups, and proves a backup restarts the gateway in a disposable container; generic guides cover scheduling, restore checks and in-place restore. | `BR-038`, `BR-039`, `BR-328`, `BR-329` | `F-004` | `F-022` |
| **MCP gateway isolation acceptance run** | Plays the 16 isolation tests of the review against an MCP gateway in one command (targets ContextForge, POC Go, v1), evidence by the upstream log, replays the checks outside the acceptance run and the core requirements, and renders a go / no-go report, comparative between POCs included; against v1, the full run gives 16 passed out of 16, relay and real `gitea-mcp` included. | `BR-001`, `BR-002`, `BR-003`, `BR-004`, `BR-005`, `BR-006`, `BR-009`, `BR-010`, `BR-011`, `BR-012`, `BR-013`, `BR-014`, `BR-024`, `BR-025`, `BR-026`, `BR-027`, `BR-071` | `F-001` | `F-015` |

## connections

How the gateway keeps the accounts it uses at the upstream services, authorizes them, keeps their tokens encrypted and makes them valid for the relay.

| Capability | What it does | Rules | Introduced | Last touched |
|---|---|---|---|---|
| **Connection library** | Declares services and connections `<client>/<nom>` (OAuth or provided token) under a tool prefix, places them on roles with traced confirmation of any share, shows where each one serves without secret, revokes or removes them, gives the relay a valid token or a typed error, and diagnoses the upstream of a placed connection (`connexion outils`). | `BR-074`, `BR-075`, `BR-076`, `BR-077`, `BR-078`, `BR-079`, `BR-080`, `BR-085`, `BR-086` | `F-006` | `F-016` |
| **Outbound OAuth client** | Authorizes an account at a provider through OAuth 2.1 (PKCE, single-use `state`, declared application or dynamic registration), pins its endpoints at declaration, bounds each call, renews on use under lease, classifies refusals and honors `Retry-After`, without silently switching accounts, on a single outbound client shared by the process. | `BR-081`, `BR-082`, `BR-083`, `BR-084`, `BR-089`, `BR-090`, `BR-091`, `BR-092`, `BR-093`, `BR-094`, `BR-095`, `BR-096`, `BR-097` | `F-006` | `F-016` |
| **Encrypted connection vault** | Encrypts upstream tokens and secrets (AES-256-GCM, versioned envelope, bound to their place) with a key mounted outside the database, checked by a key check value; without the key, nothing is decrypted or written and the gateway stays ready. | `BR-087`, `BR-088` | `F-006` | `F-010` |

## relay

How the gateway serves, on a role's access point, the tools of the upstream MCP servers that its connections carry, without the client's token or another role's context crossing.

| Capability | What it does | Rules | Introduced | Last touched |
|---|---|---|---|---|
| **MCP tool relay** | Exposes permitted tools and relays calls after policy and token/connection rate checks, preserving arguments and results. | `BR-109`, `BR-110`, `BR-111`, `BR-112`, `BR-113`, `BR-114`, `BR-115`, `BR-116`, `BR-117`, `BR-120`, `BR-121`, `BR-123`, `BR-126`, `BR-127`, `BR-128`, `BR-129` | `F-008` | `F-020` |
| **Action policy** | Decides per placement the tools a role sees and the values it sends (classes, mode, lists, argument rules, pinning, response cap), rights preview included. | `BR-170`, `BR-171`, `BR-172`, `BR-173`, `BR-174`, `BR-175`, `BR-176`, `BR-177`, `BR-178`, `BR-179`, `BR-180`, `BR-181`, `BR-182`, `BR-183`, `BR-184`, `BR-185`, `BR-186`, `BR-187`, `BR-188` | `F-015` | `F-015` |
| **Upstream MCP client** | Joins the MCP server of a declared service, in the protocol generation it negotiates, under hard bounds of duration, size, concurrency and rate, with a silently reopened session, without redirection or environment. | `BR-106`, `BR-107`, `BR-108`, `BR-118`, `BR-119`, `BR-124`, `BR-125` | `F-008` | `F-010` |

## administration

How an operator administers the gateway from a browser, on the same rules as the CLI.

| Capability | What it does | Rules | Introduced | Last touched |
|---|---|---|---|---|
| **Admin console** | Administers the tree and library through CLI operations, previews rights, edits policy, shows expiry, and offers organisation setup and host recovery behind a passkey. | `BR-214`, `BR-215`, `BR-216`, `BR-217`, `BR-228`, `BR-229`, `BR-230`, `BR-232`, `BR-238` | `F-016` | `F-023` |
| **Administrator sign-in** | The host creates the first administrator, who enrols a passkey by a single-use link and signs in with it alone to a short server-side session; attempts are rate-limited, security acts re-verify it. | `BR-200`, `BR-201`, `BR-202`, `BR-203`, `BR-204`, `BR-207`, `BR-208`, `BR-210` | `F-016` | `F-016` |
| **Change history and undo** | Reverses settings, locks, sensitivity, placements, prefixes and policy through ordinary operations; preserves history and refuses stale state. | `BR-219`, `BR-220`, `BR-221`, `BR-066` | `F-016` | `F-016` |
| **YAML import** | Previews export-format YAML without writes, applies a confirmed plan atomically, deletes by opt-in and restores connections without credentials. | `BR-222`, `BR-223`, `BR-224`, `BR-225`, `BR-236` | `F-016` | `F-016` |

