---
id: ADR-004
title: In-house OAuth handlers, mcp SDK for the MCP entry only
status: accepted
date: 2026-10-29
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
---

# ADR-004 — In-house OAuth handlers, mcp SDK for the MCP entry only

## Context and Problem Statement

ADR-001 retains the pinned `mcp` SDK 2.3.0 for the MCP entry and its OAuth handlers (`mcp.server.auth`). The Python POC (F-003, `docs/trials/poc-python.md` H-5, E-02) measured what these handlers do not do the way the v1 requires: `resource` read through `dict()` (a single value, the ambiguous request passes), `RevocationRequest.client_secret` mandatory (a public client cannot revoke), `unauthorized_client` instead of `invalid_client` for an unknown client, provider exception translated into a 302 `server_error` instead of a 503, a single issuer per application; the POC partly fixed them by replacing module objects, which F-005 forbids (technical constraint: "no glue by replacing a module object"). The F-005 spec leaves the form open: "subclasses or in-house handlers passed explicitly". The operator's arbitration of 2026-10-28 (decision 4a) asks for this choice to be recorded.

## Considered Options

- **Replacing module objects** (the POC's form): rejected by the spec (E-02).
- **Subclasses of the SDK's handlers**: keeps the SDK's structure, but each gap concerns the handlers' internal API (form reading, pydantic models, exception translation, issuer fixed at construction); almost every method would have to be overridden, tied to the internals of a pinned version.
- **Specialized OAuth library** (e.g. Authlib): security code maintained by specialists and widely proven, at the price of a dependency to bend to the core's requirements (one authorization server per access point, audience per access point, barrier). **Not evaluated** at the time of writing; comparison planned after the v1 (backlog ticket). CIMD retrieval (BR-062) would remain to be written in this option.
- **In-house handlers** (chosen): the five OAuth routes and the two metadata families written in `src/hullgate/oauth.py` (about 1,350 lines with the barrier), on Starlette and PyJWT; the SDK remains the MCP entry (`StreamableHTTPSessionManager`, `Server`), with a single explicit extension (`supported` of the five versions, BR-067).

## Decision Outcome

**In-house handlers.** No SDK object is replaced or subclassed for OAuth; the form is read in full (repeated parameters counted), any vault or key failure becomes a JSON 503 before any processing, each authorization server has its issuer. The metadata bodies are written by hand (the SDK's `AnyHttpUrl` models normalize URLs).

### Consequences

- Good: the v1 depends on no SDK internal for OAuth; an SDK upgrade touches only the MCP entry, guarded by the bench (BR-037) and `test_br_067`.
- Good: each behavior F-005 requires (BR-056 to BR-064) reads in a single module, with no correction layer.
- Bad: more code to maintain than with thin subclasses; the handlers do not benefit from the SDK's future fixes.
- Neutral: the isolation bench (target `v1`, checks `H-2`, N3 harness and nominal) judges this code as it judged the SDK at the POC.

### Confirmation

`grep` for an assignment to an attribute of an `mcp.*` module in `src/`: none; tests `test_br_056` to `test_br_064`; `v1` bench.

## More Information

ADR-001; F-005 §9 "Technical Constraints", "Pitfalls Discovered" (batch B); ADR-002.
