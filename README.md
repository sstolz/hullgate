# hullgate

A self-hosted MCP gateway that compartmentalizes what AI agents can reach: one URL per work
context, behind which an agent sees only the accounts and tools of that context.

## What hullgate is

hullgate sits between MCP clients (Claude on iOS, the web, Desktop, Claude Code) and the MCP
servers of the services you use (Gitea, GitHub, Notion, Sentry and others). It is for a
freelancer or a small business working for several clients, with several accounts of the same
provider, that lets AI agents work on its projects.

Without a gateway, each client receives wide-scope service tokens, and an agent working on one
project can reach another client's accounts or write to production. hullgate organises the work
as a tree — organisation › client › project › environment › role — and gives each role its own
MCP endpoint:

- **Inbound OAuth 2.1**: each endpoint has its own authorization server; a token opens one node of
  the tree and nothing else.
- **Connections**: the gateway holds the service credentials (outbound OAuth or provided tokens,
  encrypted at rest), so clients never see them.
- **Relay**: a role sees the tools of the connections placed on it, renamed with a per-connection
  prefix, and only those.
- **Action policy**: per-role modes and lists, tool pinning, writes closed by default on sensitive
  environments.
- **Audit and limits**: every issuance, refusal, change and tool call is audited without secrets;
  rate limits protect each endpoint.
- **Administration**: a command line in the container and a web console (passkeys) on a separate
  listener.

## Status

Version 1.0.0 is the first public release. It covers the tree, inbound and outbound OAuth, the
connection library, the relay with its action policy, the audit, rate limiting and the
administration console, packaged as one container image deployed behind Traefik. The version in
progress is tracked by its milestone in the issues.

This GitHub repository is the project's repository of record: code, issues, pull requests and
releases. Its history starts at the 1.0.0 release; development before it happened in a private
repository (see [Historical references](#historical-references)).

## Quick start

Requirements: Docker with Compose v2.

```sh
docker compose up --build
curl http://127.0.0.1:4790/readyz
```

The composition publishes the public listener on `127.0.0.1:4790` and the administration listener
on `127.0.0.1:4791`, with the data in a named volume. The first administrator is created from
the host:

```sh
docker compose exec hullgate hullgate admin bootstrap <email>
```

It prints a single-use enrolment link for the console at `http://localhost:4791`; the
[admin guide](docs/guides/admin.md) walks through passkey enrolment.

## Deployment

hullgate runs as one container behind an existing Traefik, from a clone built on the target. The
deployment guides cover the whole life of an installation:

- [Install](docs/guides/install.md) — prerequisites, configuration and first deployment.
- [Update](docs/guides/update.md) — update, rollback and optional fresh start.
- [Backup and restore](docs/guides/backup-restore.md) — backup scheduling, restore check and
  recovery.
- [Admin](docs/guides/admin.md) — bootstrap, SSH tunnel, passkeys and optional public routing.
- [Keys and upstreams](docs/guides/keys-and-upstreams.md) — secret creation, recovery and upstream
  setup.

`make deploy REF=…`, `backup`, `restore-check`, `verify`, `status` and `logs` drive an
installation through `deploy/ops.sh`.

## Configuration

The gateway is configured by environment variables only; a missing or invalid variable prevents
startup (exit code 2, with a message naming it).

| Variable | Meaning |
|---|---|
| `PUBLIC_URL`, `ADMIN_URL` | Required, absolute URLs of the public and administration listeners. `ADMIN_URL` is the console's exact origin: a hostname, never an IP, `https` except on `localhost`. |
| `PUBLIC_PORT`, `ADMIN_PORT` | Listening ports (default `4790`, `4791`). |
| `DATA_DIR` | Directory of the SQLite database and mounted keys (default `/data`). |
| `ADMIN_RP_ID` | WebAuthn relying party id of the console (default: the host of `ADMIN_URL`). |
| `TRUSTED_PROXIES` | CIDRs whose `X-Forwarded-For` is read. |
| `ADMIN_HOST` | Optional public name of the console. |

The [install guide's parameter table](docs/guides/install.md) lists the deployment values, and
the [administration API](docs/admin-api.md) documents the console's JSON API.

## Development

Tools: `uv` (the version pinned by `[tool.uv] required-version` in `pyproject.toml`), GNU
`make`, Docker with `buildx` and Compose v2. Python 3.13 and the dependencies are installed by
`uv sync --locked`; the console's checks use a local Node at the `Makefile`'s pinned version or
its pinned Docker image.

| Command | Effect |
|---|---|
| `make gate` | The quality gate: lockfile, `ruff format --check`, `ruff check`, `mypy --strict`, unit and integration tests, the SDK guard and the console's web checks. Last line `GATE: OK` or `GATE: KO <step>`. |
| `make qa` | Analysis tools outside the gate (coverage, dependency audit, secret scanner, console audit, Sonar when configured), one `QA …` line per tool, last line `QA: OK` or `QA: KO <names>`. Roles: `QA_TOOLS` in the `Makefile`. |
| `make fmt` | `ruff format` and `ruff check --fix`. |
| `make lint`, `make typecheck`, `make test` | Single steps of the gate. |
| `make test-container` | Builds the images and runs `tests/container`. |
| `make test-browser` | Runs the console in Chromium (Playwright) against the image. |
| `make image`, `make image-multiarch` | Local image; one build per architecture (`linux/amd64`, `linux/arm64`). |
| `make prod-smoke` | The production composition behind a local Traefik, in a disposable clone. |

The MCP SDK is pinned by exact version; an upgrade goes through `make bench-sdk`, which replays
the isolation acceptance run (`poc/isolation-bench/`) before the gate accepts the new version.
Specifications live under `features/`, business rules under `business-rules/` and architecture
decisions under `architecture-decisions/`.

## Contributing

Bugs, requests and pull requests are welcome: [CONTRIBUTING.md](CONTRIBUTING.md) says how a
change is proposed, what it must pass and how it is reviewed.

## Security

Report vulnerabilities privately, never in an issue: see [SECURITY.md](SECURITY.md).

## License

hullgate is released under the [MIT licence](LICENSE).

## Historical references

`HULL-NN` names a ticket or pull request of the project's private tracker before publication: a
historical reference, never a GitHub issue number. `#N` names a GitHub issue or pull request of
this repository, except in the tree of tag `v1.0.0`, which still writes the private tracker's
tickets and pull requests that way. `GH-N`, in a branch name or in a field cairn composes (a
specification's `ticket:` key, the `Ticket` column of `features/index.md`, a rule's
`introduced-by`, a derogation's `ref`, the first cell of a rule's Traceability row, the ticket in
a rule's Versioning reason), names GitHub issue #N. Review identifiers (such as
`test_review_594*.py`) and citations of workshop reports under `.cairn/runs/` and
`.cairn/learnings/` are historical: those reviews and reports are not published.
