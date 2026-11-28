## 1.0.0 — 2026-11-19

### One gateway, one MCP access point per role

hullgate 1.0.0 is the first release. It sits between your AI clients and the MCP servers they use, and gives every role its own MCP access point: a client connected to one role cannot reach another role's tools, another account, or a tool outside its scope. Access points live in a tree (organisation › client › project › environment › role) whose settings are inherited, and every act and refusal is written to an audit log without secrets.

Isolation is checked, not assumed: the 16-test isolation acceptance run passes 16 out of 16 against this version, relay and a real upstream MCP server included.

### Connecting clients

- Each access point has its own OAuth 2.1 authorization server, so MCP clients such as Claude.ai connect with a standard sign-in flow; operator tokens issued from the console or the CLI cover clients configured with a bearer token, such as Claude Code, and scripts.
- Sensitive environments accept only tokens placed on them.

### Connecting upstream servers

- A connection library declares the services and accounts hullgate calls on your behalf, with outbound OAuth and an encrypted vault for upstream tokens and secrets.
- The relay serves each role only the tools of the connections placed on it, and an action policy decides which tools a role sees and which argument values it may send; a pinned tool whose upstream definition changes is suspended on that placement until reviewed.

### Administering from a browser

The admin console, served on a separate administration listener, covers the tree, the library, tokens, policy and the audit log. Administrators sign in with a passkey only. Destructive acts show their impact before they run, settings changes can be undone from the history, and a YAML export can be previewed and re-imported. A setup guide walks a new operator through the first five steps.

### Running it

- One container image for amd64 and arm64, non-root and read-only except its data directory, configured by environment variables and mounted secrets.
- Deployment behind an existing Traefik, with rate limits on the public listener, client addresses read only through declared proxies, request-body deadlines, and hot SQLite backups with a restore check.
- Guides for installation, update, backup and restore, keys and upstreams, and administration are in `docs/guides/`.

### Before you deploy

- `TRUSTED_PROXIES` is required in the production composition: list the proxies whose forwarding headers hullgate may trust.
- `ADMIN_URL` must be the exact origin of the console: a hostname, `https` except on `localhost`.
- This is the first release; there is no earlier version to upgrade from.
