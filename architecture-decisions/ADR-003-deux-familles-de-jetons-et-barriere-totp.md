---
id: ADR-003
title: Two token families, and the operator's TOTP barrier
status: accepted
date: 2026-10-29
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
---

# ADR-003 — Two token families, and the operator's TOTP barrier

## Context and Problem Statement

"How a caller proves who it is" to an MCP access point of the gateway. Two client populations coexist: Claude Code and clients without OAuth, which accept a fixed `Authorization` header; Claude.ai, iOS and Desktop, which require OAuth 2.1 with an audience equal to the access point's URL. And no OAuth code must be issued unless the operator, the system's only human, has authorized it: the original specification (l. 773-774) asks for administrator authentication on the consent page, by passkey or TOTP; the passkey arrives with the web administration (HULL-11). Decision taken at the freeze of F-005 and confirmed by the operator's arbitration of 2026-10-28 (decision 4a, barrier folded into this ADR).

## Considered Options

- **A single family, JWT for everyone**: Claude Code's tokens would be long JWTs issued by the CLI; but a path token (`mpx/cairn`, spec l. 363) covers several access points and cannot have a single audience, and a JWT can only be revoked through a registry.
- **A single family, opaque tokens for everyone**: contrary to RFC 9068, which the Claude clients expect, and the audience would no longer exist.
- **Two families** (chosen): opaque operator tokens (`clo_` + 256 bits, SHA-256 fingerprint alone in the database, set on a node, coverage by segment prefix, refused in a sensitive environment above the role); OAuth access tokens as RFC 9068 JWTs signed ES256 (key in a mounted secret), exact audience, `jti` in the registry, renewal lineage with rotation.
- For the barrier: **shared authorization secret** (POC); **TOTP RFC 6238** (chosen); **passkey** (HULL-11).

## Decision Outcome

Two families, judged by the same authorization order (F-005 BR-055) which plugs resolvers in at step 4; "option 1" reading of the audience: exact for OAuth tokens, path prefix for all, role token required in a sensitive environment. TOTP barrier before any code, whatever the client's admission mode: secret in a mounted file, strictly increasing time steps kept in the database (never two codes of the same step), global window of 5 failures in 15 minutes.

### Consequences

- Good: Claude Code needs no interactive flow; the Claude clients get the OAuth they expect; no bearer secret in clear in the database (BR-072).
- Good: the barrier depends on no web session or account; a code cannot be replayed.
- Bad: two resolvers and two life cycles (CLI on one side, `/token` and `/revoke` on the other); `jeton lister` and `jeton revoquer` cover both.
- Bad: global failure window (no reliable client address): a third party can block new authorizations without obtaining any (F-005 risk §12), lifted by the passkey (HULL-11).
- Bad: a single signing key; changing it invalidates the access tokens in flight (rotation: HULL-23).

### Confirmation

Tests `test_br_050` to `test_br_055`, `test_br_060`, `test_br_072`, `test_br_073`; `v1` bench: checks `BR-026` (N1, N2, sensitive) and `H-2`.

## More Information

F-005 (BR-050 to BR-055, BR-060, BR-072, BR-073); code `src/hullgate/tokens.py`, `src/hullgate/access_tokens.py`, `src/hullgate/keys.py`, `src/hullgate/oauth.py`; ADR-002.
