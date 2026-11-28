---
id: ADR-001
title: Python for the gateway's v1, mcp SDK pinned
status: accepted
date: 2026-10-28
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
---

# ADR-001 — Python for the gateway's v1, mcp SDK pinned

## Context and Problem Statement

The original specification (attached to ticket #1) deferred the stack choice until after trials: Go proposed for a new development (single binary in a small image, official `go-sdk`, `ory/fosite`), a Python overlay on ContextForge if ContextForge held the isolation, and "Python acceptable for a throwaway POC, not for the target". BR-008 v2 reserves the decision to the operator: no trial or POC writes `project.stack`.

Three trials were judged on the same isolation bench (`poc/isolation-bench/`, 16-test acceptance run, runner `bc87ce9` unchanged):

- ContextForge 1.1.0 (F-001, HULL-2): **no-go**, 7/16, three in-core gaps (no authorization server for MCP clients, no per-resource audience check, loss of the upstream OAuth session on concurrent renewals), 31.5 d of gaps.
- Go POC (F-002, HULL-5): **go, Claude.ai / iOS reservation open**, 16/16, 11/11 isolation lines; remaining gaps 3.5 d (2026-07-28 MCP entry written by hand 2 d, in-memory OAuth storage 1 d, renewal margin 0.5 d); core requirements N1, N2, N4, N13 not met.
- Python POC (F-003, HULL-16): **go, Claude.ai / iOS reservation open**, 16/16, 11/11 isolation lines, strict comparison; remaining gaps 1.0 d (renewal margin 0.5 d, glue on SDK internals 0.5 d); N1, N2, N4, N13 met; checks outside the acceptance run N3 16/16 and nominal `/revoke` 14/14.

The foundation (#6) and the core (#7) cannot start without a stack: tooling, image, quality gate and libraries depend on it.

## Considered Options

- **Go** — `go-sdk` v1.4.0 + `ory/fosite` v0.49.0 + SQLite without cgo; static binary. Holds the isolation, but the 2026-07-28 MCP entry is in-house (the `go-sdk` does not serve it), `fosite`'s OAuth storage is in memory, JWT revocation and the 503 of `/revoke` are glue, four core requirements remain to be written.
- **Python** — official `mcp` SDK 2.3.0 (`mcp.server.auth`, Starlette, Uvicorn), Authlib 1.9.1 for outbound OAuth, PyJWT, SQLite; dependencies pinned by `uv.lock`. Holds the isolation; the MCP entry for five versions including 2026-07-28, DCR, `insufficient_scope` and the 400 without `token` come from the SDK; persistent storage is in-house (SQLite provider), the OAuth glue on `resource` is equivalent to Go's.
- **Python overlay on ContextForge** — rejected: F-001's no-go, in-core gaps that cannot be fixed in an overlay.

Rust, PHP and JavaScript were not tried: the original specification excluded the last two and proposed Rust only if memory safety took precedence over lead time.

## Decision Outcome

Chosen: **Python**, the operator's decision (human:sstolz, 2026-10-28, BR-008 v2) on the recommendation drawn from the two POCs: at equal isolation (16/16 identically, identical runner), Python gets from libraries what Go wrote by hand (2026-07-28 MCP entry, DCR, per-role tool list, `insufficient_scope`), with 1.0 d of remaining gaps against 3.5 d and the four known core requirements already met. The original specification's reservation ("Python not for the target") was about Go strengths the POCs did not measure; it is lifted by the measurement of isolation and cost, not by a measurement of those strengths.

Pinned version: Python 3.13, `uv` (project described by `pyproject.toml`, `uv.lock` lock committed), SDK **`mcp==2.3.0`** (the Python POC's version, read from `poc/py-gateway/pyproject.toml` and `uv.lock`).

### Consequences

- Good, because the MCP entry of both generations (2024-11-05 to 2025-11-25 with session, 2026-07-28 without session) is a library function, not in-house code to maintain.
- Good, because DCR (RFC 7591), `RequireAuthMiddleware` (403 `insufficient_scope`) and the audience check of the `BearerAuthBackend` come from the official SDK.
- Good, because the POC's SQLite provider (codes, lineages, renewals as fingerprints, `jti`, `BEGIN IMMEDIATE`) is a measured starting point for the core (#7).
- Bad, because the gateway depends on internal behaviors of a young SDK: two internals are replaced at load time in the POC (`RevocationRequest`, `StreamableHTTPTransport.handle_get_stream`, gap E-02) and `TokenHandler` does not compare `resource` at renewal or at code exchange; a version upgrade can break the isolation with no visible error. Hence the conditions below.
- Bad, because `/revoke` for an unknown client answers `unauthorized_client` instead of `invalid_client` (`fosite` held it): glue to keep.
- Bad, because the image carries an interpreter and its dependencies: heavier than a static Go binary (order of magnitude from the original specification: 150 MB and more against 20 to 40 MB), not measured.
- Neutral, because neither the single binary, nor the memory footprint, nor behavior under load was measured for either stack: these strengths the original specification lends to Go remain hypotheses, not findings.
- Neutral, because the Claude.ai / iOS reservation (line H-7 of both POCs, HULL-3) remains open; it concerns the POCs' OAuth, not the stack.

**Conditions attached by the operator**:

1. **Pin the `mcp` SDK version**: exact constraint (`==`) in `pyproject.toml`, `uv.lock` lock committed, installation by `uv sync --locked`. No version range.
2. **Replay the isolation bench at every SDK upgrade** (`poc/isolation-bench/`, 16-test acceptance run and checks outside the acceptance run N3 and nominal `/revoke`, T10): an upgrade is merged only if the acceptance run is replayed and holds, with evidence.
3. **Propose the two SDK fixes upstream**: `resource` forwarded and compared at renewal (`TokenHandler` → `exchange_refresh_token`); `/revoke` for a public client (`RevocationRequest.client_secret` without a default value). The corresponding glue stays in place until the version that publishes them, then is removed under condition 2.

**Review**: this decision is to be reviewed if an `mcp` SDK upgrade makes an isolation line fail with no reasonable glue fix, if the SDK stops being maintained, or if a load or footprint measurement of v1 falls outside the needs of a freelancer or a small organisation (the brief's target).

### Confirmation

- The project's `pyproject.toml` declares `mcp==<version>` and `uv.lock` is committed; the quality gate fails on an out-of-sync lock (`uv lock --check` or `uv sync --locked`).
- Every PR that changes the `mcp` version in `uv.lock` carries evidence of a replay of the isolation bench (16/16 and checks outside the acceptance run); implementing this guard is the object of the foundation (#6).
- `project.stack` of `.cairn/project-context.yaml` takes up this stack.

## More Information

- Reports: `docs/trials/contextforge.md` (F-001), `docs/trials/poc-go.md` (F-002), `docs/trials/poc-python.md` (F-003, sections "Go vs Python", "Gaps", "Verdict").
- Specs: `features/F-001-essai-contextforge.md`, `features/F-002-poc-go.md`, `features/F-003-poc-python.md`.
- Rules: BR-008 (the stack decision belongs to the operator), BR-023 (POC verdict criterion).
- Reference code: `poc/py-gateway/` (Python POC, glue marked `COLLE`), `poc/isolation-bench/` (isolation bench).
- Tickets: #1 (original specification), #3 (Claude.ai / iOS reservation), #6 (foundation), #7 (core).
