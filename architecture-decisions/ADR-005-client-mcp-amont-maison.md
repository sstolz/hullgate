---
id: ADR-005
title: Upstream MCP client written on the bounded outbound client's mechanism, without the SDK's client
status: accepted
date: 2026-10-30
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
---

# ADR-005 — Upstream MCP client written on the bounded outbound client's mechanism, without the SDK's client

## Context and Problem Statement

F-008 (batch A) makes the gateway talk to upstream MCP servers. BR-107 (`strict`) requires of every exchange a hard deadline on the whole exchange, name resolution included (15 s, 60 s), at most 1 MiB read, no redirect followed, no environment variable read (proxy, certificate store), addresses all verified before the connection and then joined in the resolver's order, and a closed list of outbound headers (amended on 2026-10-30 for `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*`). BR-108 separates the relay's threads from those of the OAuth client, BR-118 and BR-119 impose two generations and sessions per (role, connection). The spec leaves the choice between extending `hullgate.outbound` and a client of the `mcp==2.3.0` SDK (H12), and rejects it if it reads the environment, follows a redirect or does not join the verified address.

## Considered Options

- **SDK client** (`mcp.client.streamable_http`, `ClientSession`): complete and maintained protocol, but an `httpx2` transport that reads the environment by default (`trust_env`), resolves and connects by itself (no verified address joined without a custom transport), and whose deadline covers the read and not the whole exchange. Measured at the POC (inbox F-003): seven upstream requests per call in "one session per call" use, including a GET stream and a `DELETE` outside the count. Constraining it in a proven way would require an in-house `httpx` transport, session internals settings and watching every SDK upgrade (ADR-001).
- **In-house client on the mechanism of `hullgate.outbound`** (chosen): `http.client`, `socket`, `ssl`, one worker thread per exchange cut at the deadline, connection to the admitted address (class `_Pinned` of BR-084, reused as is and extended to clear text for the internal host), and a short protocol layer (`server/discover`, `initialize`, `tools/list`, `tools/call`, SSE on read) in `src/hullgate/upstream_mcp.py`. The 2026-07-28 headers are built by the public functions of `mcp.shared.inbound` (`x_mcp_header_map`, `encode_header_value`), the ones the SDK's servers use to validate.
- **Extending `Outbound` itself** (one more bounds profile): a single set of threads, against BR-108; and `Outbound` keeps whole responses in memory with no SSE reading.

## Decision Outcome

**In-house client** in `hullgate.upstream_mcp` (`Transport`: bounds, addresses, concurrency 4 / 32, "not before" date; `UpstreamClient`: generations, sessions, pagination), reusing the verified-address connection of `hullgate.outbound` without modifying it; no SDK object is replaced or subclassed.

### Consequences

- Good: each bound of BR-107 reads and is tested in one module (fake upstream per generation, `tests/fake_upstream.py`); no environment variable reaches the relay by construction (`http.client` reads none, TLS context from OpenSSL's compiled paths).
- Good: an SDK upgrade does not change the upstream transport; the run against the SDK's real server (`test_br_118_generations.py`) keeps the 2026-07-28 protocol compatibility.
- Bad: the protocol layer must be maintained by hand against MCP's evolutions (new generation, new headers); the `server/discover` probe costs one more exchange at session opening of a handshake upstream.
- Neutral: upstream → client requests (sampling, elicitation, roots) received in a stream are read and discarded in batch A; BR-121's error response belongs to batch B.

### Confirmation

`tests/unit/test_br_107_upstream_bounds.py` (bounds, addresses, environment, headers); `tests/unit/test_br_108_concurrency.py`; `grep -n "httpx" src/hullgate/upstream_mcp.py`: no line.

## More Information

F-008 §9 (H12, H23), BR-107, BR-108, BR-118, BR-119; ADR-001 (pinned SDK), ADR-004 (same stance for OAuth); F-006 BR-084 and HULL-32 (address fallback).
