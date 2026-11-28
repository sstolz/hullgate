# hullgate

Compartmentalized MCP gateway — v1 in Python 3.13, SDK `mcp` 2.3.0 pinned (`architecture-decisions/ADR-001-pile-python-v1.md`).
F-004 (`features/F-004-fondation.md`) lays its foundation: two HTTP listeners (public 4790, administration 4791) that serve the probes, a SQLite database under `/data`, the image, the quality gate, the production composition behind Traefik and the operations on the production server. No business function yet (#7 and following).

## Développement

Tools: `uv` 0.13.6 (pinned by `[tool.uv] required-version`), GNU `make`, Docker with `buildx` and `compose` v2.

| Command | Effect |
|----------|-------|
| `make qa` | analysis tools outside the gate (F-007): `cov` (coverage, informative), `audit` (vulnerabilities of the runtime lock), `sonar` (SKIP under `make qa`); one `QA …` line per tool, last line `QA: OK` or `QA: KO <noms>`. Roles: `QA_TOOLS` of the `Makefile`. `make sonar`: analysis of `main`, on the operator's go |
| `make gate` | full quality gate: `uv sync --locked`, then `uv lock --check`, `ruff format --check`, `ruff check`, `mypy --strict`, `pytest tests/unit tests/integration` (writes `coverage.xml`), `python scripts/sdk_guard.py`; last line `GATE: OK` or `GATE: KO <étape>` |
| `make fmt` | `ruff format` and `ruff check --fix` |
| `make lint`, `make typecheck`, `make test` | isolated steps of the gate |
| `make test-container` | builds `hullgate:dev` and the two `hullgate:multiarch-*` images, then `pytest tests/container` (required before a PR when the diff touches `Dockerfile`, `compose*.yaml`, `deploy/` or `Makefile`) |
| `make image`, `make image-multiarch` | local image; one `--load` build per architecture (`linux/amd64`, `linux/arm64`) from the same `Dockerfile` |
| `make prod-smoke` | production composition behind a local Traefik, in a disposable clone: `make deploy`, routing, unrouted administration, readable failed deployment, client address and independent refusal budgets, edge 404 refusals (`edge-404`); ends with a `SMOKE` banner reporting success for every control |
| `make clean-images` | removes the build images and the buildx cache |

Run the gateway locally: `docker compose up --build` (ports on `127.0.0.1` only), then `curl http://127.0.0.1:4790/readyz`.

Configuration (environment variables only): `PUBLIC_URL` and `ADMIN_URL` (required, absolute URLs), `PUBLIC_PORT` (4790), `ADMIN_PORT` (4791), `DATA_DIR` (`/data`). A missing or invalid variable prevents startup (code 2, message that names it).

Admin console (F-016): `ADMIN_URL` is the console's exact origin, a hostname and never an IP, `https` except on `localhost` (`http://localhost:4791` locally, `http://localhost:14791` through the tunnel on the production server); optional `ADMIN_RP_ID` (WebAuthn relying party id, default the host of `ADMIN_URL`), `TRUSTED_PROXIES` (CIDRs whose `X-Forwarded-For` is read) and `ADMIN_HOST` (public admin name, lot E). The first administrator comes from the host: `hullgate admin bootstrap <email>` prints a single-use enrolment link (runbook `docs/guides/admin.md`, API `docs/admin-api.md`). `make gate` gains a `web` step (console lockfile, typecheck, lint, unit tests, with a local Node 26.13.0 or the pinned image); `make test-browser` runs the console in Chromium against the image.

### Multi-architecture build

The `build` stage of the `Dockerfile` runs on the architecture of the build machine (`--platform=$BUILDPLATFORM`) and installs, with `uv sync --locked --no-dev --python-platform`, the wheels of the target architecture; the `runtime` stage contains no `RUN` instruction. No `qemu` emulation is therefore needed to build the arm64 image on an amd64 workstation (nor the reverse); it is needed to run it.

## Upgrading the mcp SDK

The gate refuses any version of `mcp` or `mcp-types` that the `v1` target of the isolation bench has not replayed in full (BR-036, BR-037; ADR-001 condition 2). The guard reads `poc/isolation-bench/evidence/results-v1.json`; the Python POC and its `py-gateway` target were removed by HULL-58 (git history keeps them).

1. Change the version in `pyproject.toml` (`mcp==X.Y.Z`, exact equality), then `uv lock`; check that `mcp-types` followed.
2. Commit both: the bench refuses an uncommitted configuration outside `--dev`.
3. `make bench-sdk` in the toolbox, with `/workspace/.git` mounted (the bench needs `buildx`, absent from the session; at least 10 GB free on the VM disk, BR-021): conclusive replay `./bench.sh --target v1 --purge-images` (never `--dev`), then the guard. The N3 and nominal `/revoke` harnesses run in this replay and the guard requires them.
4. Commit `poc/isolation-bench/evidence/results-v1.json` (and the two v1 harness files that the replay rewrites); `make gate` must end on `GATE: OK`.

`scripts/sdk_guard.py` checks: the `mcp==X.Y.Z` form and its resolution in `uv.lock`; then, in `results-v1.json`, `meta.target` equal to `v1`, `dev_mode` and `partial_run` false, `mcp` and `mcp-types` of `meta.module_versions` equal to those of `uv.lock`, T01 to T16 present, every recorded test and control passed (a failing identifier outside the acceptance run also blocks), a non-empty `controls` list, entirely passed, that names the N3 and nominal `/revoke` harnesses. It reads neither the v1 readings, nor `bench_commit`, nor a lock fingerprint: a change to the gateway or to another package does not require a replay here. The other packages that carry isolation (PyJWT, Starlette, Authlib) are upgraded without an imposed replay (F-004, H13).

## Deployment behind Traefik

Use an existing Traefik and a clone built on the target. The default installation directory is
`/opt/hullgate`; choose your values from the [parameter table](docs/guides/install.md#parameters).
`make deploy REF=…`, `backup`, `restore-check`, `verify`, `status` and `logs` use `deploy/ops.sh`.

- [Install](docs/guides/install.md) — prerequisites, configuration and first deployment, O1–O9.
- [Update](docs/guides/update.md) — update, rollback and optional fresh start.
- [Backup and restore](docs/guides/backup-restore.md) — backup scheduling, restore check and recovery.
- [Admin](docs/guides/admin.md) — bootstrap, SSH tunnel, passkeys and optional public routing.
- [Keys and upstreams](docs/guides/keys-and-upstreams.md) — secret creation, recovery and upstream setup.

Optional Sonar analysis uses `SONAR_HOST_URL` and either `SONAR_TOKEN` or `SONAR_TOKEN_FILE`.
Missing configuration prints `SKIP`; `make qa` never sends a Sonar analysis.

## Historical references

`HULL-NN` identifiers refer to the project's private tracker before publication; they are
historical references, not GitHub issue numbers. The same applies to `#NN` issue and
pull-request numbers and review identifiers (such as `test_review_594*.py`). Citations of
workshop reports under `.cairn/runs/` and `.cairn/learnings/` are historical; those paths
are not published.
