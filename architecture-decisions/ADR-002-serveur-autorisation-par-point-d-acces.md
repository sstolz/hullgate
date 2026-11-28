---
id: ADR-002
title: One OAuth authorization server per access point
status: superseded
superseded-by: ADR-008
date: 2026-11-15
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
---

# ADR-002 — One OAuth authorization server per access point

## Context and Problem Statement

The v1 must admit the Claude clients (Claude.ai, iOS, Desktop), which accept only OAuth 2.1 and require an audience equal to the MCP server's URL (RFC 8707), while guaranteeing that a token obtained for one role never holds for another (isolation acceptance run, T02, T05, T14). The gateway serves one access point per role (`/p/<client>/<projet>/<env>/<rôle>/mcp`, F-005 BR-044), potentially hundreds. Ticket HULL-7 asks for "one authorization server per access point"; the original specification (l. 749-753) fixes the layout: one logical authorization server per access point, under `/oauth/p/<chemin>`, a single code base, parameterized URLs. Decision taken at the freeze of F-005 and confirmed by the operator's arbitration of 2026-10-28 (targeted stress test, decision 4a).

## Considered Options

- **A single issuer** for the whole instance, the isolation relying on the audience alone. Simpler (one RFC 8414 metadata pair), but a code or a renewal issued for one role can still be presented to the same `/token` for another: the isolation depends on an audience comparison at the right place in every route, and the RFC 9728 metadata of all roles points to the same server.
- **One logical authorization server per access point** (chosen): issuer `${PUBLIC_URL}/oauth/p/<chemin>`, RFC 8414 metadata by path insertion, routes `authorize`, `consent`, `token`, `revoke`, `register` under that issuer; a single code base, the issuer and the role being request parameters.
- **One process or one configuration per role**: physical isolation, operating cost out of proportion for a lone operator.

## Decision Outcome

Chosen option: **one logical authorization server per access point**. A code, an access token or a renewal carries the role of its authorization server (lineage set on the role, `iss` and `aud` of the JWT); any other server refuses it (`invalid_grant`), any other access point too (step 6, `audience_mismatch`). Discovery is exact per access point (T14: own `resource_metadata` challenge, no global fallback resource).

### Consequences

- Good: the isolation between roles holds by construction at two independent levels (authorization server, access point); a Claude client configured with a role's URL discovers its server on its own.
- Good: no additional data: the tree is enough to resolve the issuer.
- Bad: changing a role's path (rename, move, F-005 BR-046) or `PUBLIC_URL` invalidates its OAuth authorizations; clients re-authorize.
- Bad: a client must be admitted by each role's server; a pre-registration on a node holds for the roles of its subtree (core extension, BR-061), CIMD and DCR remain per access point.
- Neutral: `PUBLIC_URL` without a path (BR-032 v2), otherwise the RFC 8414 / 9728 path insertion would become ambiguous.

### Confirmation

`tests/integration/test_br_056_discovery.py`; isolation bench, target `v1`: T14 and check `H-2` (`evidence/results-v1.json`).

## More Information

F-005 (BR-055, BR-056, BR-058, BR-061); code `src/hullgate/oauth.py`; ADR-003 (token families); ADR-004 (in-house OAuth handlers).
