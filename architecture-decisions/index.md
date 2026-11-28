# Architecture decisions index

> Maintained by the role that writes the ADR, at creation and at every change of `status` (`conventions/adr.md` §The index).

| ID | Title | Status | Date |
|----|-------|--------|------|
| [ADR-001](./ADR-001-pile-python-v1.md) | Python for the gateway's v1, mcp SDK pinned | accepted | 2026-10-28 |
| [ADR-002](./ADR-002-serveur-autorisation-par-point-d-acces.md) | One OAuth authorization server per access point | superseded | 2026-11-15 |
| [ADR-003](./ADR-003-deux-familles-de-jetons-et-barriere-totp.md) | Two token families, and the operator's TOTP barrier | accepted | 2026-10-29 |
| [ADR-004](./ADR-004-gestionnaires-oauth-propres.md) | In-house OAuth handlers, mcp SDK for the MCP entry only | accepted | 2026-10-29 |
| [ADR-005](./ADR-005-client-mcp-amont-maison.md) | Upstream MCP client written on the bounded outbound client's mechanism, without the SDK's client | accepted | 2026-10-30 |
| [ADR-006](./ADR-006-console-spa-json-api-admin-listener.md) | The admin console is a Svelte SPA over a JSON API, served by the admin listener under /admin | accepted | 2026-11-14 |
| [ADR-007](./ADR-007-administrators-passkeys-sessions.md) | Console administrators are host-bootstrapped accounts that sign in with a passkey only, on short server-side sessions | accepted | 2026-11-14 |
| [ADR-008](./ADR-008-organisation-url-scheme.md) | Organisation-scoped URLs under /o/<id>, with one authorization server per access point | accepted | 2026-11-15 |
