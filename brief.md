# Brief — hullgate (multi-project MCP gateway)

Source: specification "Multi-project MCP gateway — initial specification" (revised on 26 and 27 October 2026) and OpenAI technical review of 26 October 2026, both attached to ticket HULL-1.

## What we are building

A self-hosted MCP gateway that gives each Claude client (iOS, web, Desktop, Claude Code) one URL per work context — `/o/<id>/p/<client>/<project>/<env>/<role>/mcp`, `<id>` the organisation's public identifier — behind which it sees only the accounts and tools of that context.

## For whom

A freelancer or a small business that works for several clients and has Claude agents work on their projects, with several accounts of the same provider.

## The problem solved

Without a gateway, each Claude client receives wide-scope service tokens (for example in clear text in an `.mcp.json`): an agent working on one project can reach another client's accounts, or write to production. The gateway compartmentalizes: a token opens only one node of the organisation › client › project › environment › role tree, and a role sees only the tools that the policy grants it.

## Constraints

- Isolation proven by the isolation acceptance run (16 tests, transposed to the role in the specification) before any stack decision.
- OAuth 2.1 on both sides: authorization server per access point toward Claude, OAuth client per connection toward the services.
- Sensitive environments: role-level tokens, short lifetimes, writes closed by default and opened tool by tool.
- Stack deferred: Go proposed, Python overlay of ContextForge if ContextForge passes the acceptance run.
- Hosting: container on the production server behind Traefik, compose project in `<compose-root>` with a Makefile (build, backup); local development in the session's dockerd.

## Out of scope (v1)

Differentiated administration roles; write confirmation (elicitation); openapi and wasm upstream types; MCP resources, prompts, sampling and subscriptions; rules on argument values; multi-account merging (`account` parameter). All deferred to v2 by the specification.

## Planned parts

1. Trial of ContextForge against the isolation acceptance run — go / no-go verdict.
2. Behavior of the Claude clients (resource, CIMD, tool list refresh, tool names, MCPB).
3. Matrix of the targeted official servers (OAuth, sessions, tool grouping).
4. Go POC (inbound OAuth + ping tool, end-to-end Gitea relay) — conditional on the verdict of part 1.
5. Foundation: build, quality gate, CI, hosting, deployment.
6. Core: tree, tokens, inbound OAuth, authorization order.
7. Connection library and outbound OAuth.
8. Relay: aggregation, prefixed renaming, sessions, v1 upstream types.
9. Action policy: modes, lists, pinning, guardrails on responses.
10. Web administration.
11. Audit, statistics, alerts and delivery.
12. Cutover of Cairn onto the gateway.

## Processing order

Parts 1 to 4 decide the stack; the foundation (5) opens the construction, which follows the order 6 → 12.
