# MCP gateway isolation bench (F-001)

Reusable bench to play the isolation acceptance run (16 tests, technical review of 26 October 2026, section 6) against an MCP gateway.
First use: ContextForge (HULL-2). Reusable against the Go POC (HULL-5) then the v1 by replacing only the adapter (BR-006).

## Run

```
./bench.sh                 # secrets jetables -> conteneurs -> provisionnement -> 16 tests -> teardown
./bench.sh --keep --dev    # mise au point : garde le banc, tolère une config non committée (résultats non probants)
./bench.sh --only T02,T05  # sous-ensemble (non probant)
./bench.sh --purge-images  # supprime aussi les images tirées
./bench.sh --teardown      # détruit seulement
```

Prerequisites: docker with compose, python3 >= 3.9 (standard library only), access to the registries. Disk footprint: < 1 GB of images.

## Composition

| Component | Role |
|-----------|------|
| `compose.yaml`, `cf.env` | ContextForge (image by digest), Postgres, upstream; single configuration of the 16 tests (BR-011) |
| `policy.json` | acceptance-run policies fixed before it: refusal of ambiguous requests, refusal by default when the vault is down, cut-off delay |
| `upstream/server.py` | instrumented upstream: OAuth authorization server + MCP server, two witness accounts, call log (BR-002) |
| `runner/recette.py` | runner of T01..T16, HTTP/OAuth only; fails if the count is not 16 (BR-001) |
| `runner/adapter_contextforge.py` | the only ContextForge-specific component: provisioning, revocation, outage, token forgery, function inventory |
| `runner/secrets_guard.py` | secret population and sweep (BR-003): `python3 runner/secrets_guard.py scan` before each commit |
| `runner/report_check.py` | structure check of the report (BR-004, BR-009) |

## Rules held

- Secrets generated at each run in `.run/` and destroyed at teardown (BR-010); only SHA-256 fingerprints survive, in `.digests/` (ignored by git), to sweep a commit made afterwards.
- `policy.json`, `cf.env` and `compose.yaml` must be committed and unchanged during the acceptance run; the runner refuses to play a modified configuration and records their fingerprints in `evidence/results.json` (BR-011).
- A refusal passes only if the upstream log (MCP and authorization) is empty in the test's window (BR-002).
- A test whose function is absent is « non jouable » with its evidence cited (BR-009); a bench difficulty stays « non joué ».

## Reuse against another gateway

Write an adapter exposing: `provision()`, `issue_token`, `clone_profile`, `revoke_token`, `delete_profile`, `add_account`, `reauthorize`, `profile_tool_names`, `read_foreign_connection`, `store_down`/`store_up`, `forge`, `claims`, `capability_evidence`. The client-side OAuth scenarios of T03/T04 (renewal with resource) are written only for the "authorization server absent" case; to complete when a gateway exposes one.

## Target « gateway » (F-002, Go POC)

```sh
./bench.sh --target gateway --purge-images   # construit la passerelle (poc/go-gateway), joue T01..T16 lus au rôle, contrôles, teardown
python3 runner/report_check.py --target gateway   # structure du rapport docs/trials/poc-go.md (BR-004 v2, BR-018, BR-020, BR-023)
python3 runner/check_scope.py                     # BR-008 v2 : project.stack intact
```

- Composition: `gateway.compose.yaml` (gateway, bench upstream, throwaway Gitea, Gitea MCP server, logging front); `internal` network without egress for Gitea, the Gitea MCP server and the front (BR-016).
- Acceptance run → role mapping: `mapping.json` (BR-012); adapter: `runner/adapter_gateway.py`; inbound OAuth check (H-2): `runner/oauth_checks.py`.
- Before the first test: completeness of both logs (BR-013). After: Gitea relay and inbound token (BR-015, BR-022), network (BR-016), pre-commit hook (BR-003, installed by `core.hooksPath`).
- Disk (BR-021): refusal under 10 GB free, failure beyond 4 GB of growth, purge by identifier and by difference (`docker image ls -a`), never `--rmi all`.
- Evidence: `evidence/results-gateway.json` (`--dev` or `--only` runs write `evidence/dev-*.json`, ignored by git).

## Python POC target (removed)

The `py-gateway` target (F-003) and `poc/py-gateway/` were removed by HULL-58; git history keeps them. Its two harness evidence files stay as records: `evidence/py-gateway-n3-results.json`, `evidence/py-gateway-revoke-nominal-results.json`. The v1 target (F-005, `./bench.sh --target v1`) keeps the core requirements (`runner/socle_checks.py`) and the N3 and nominal `/revoke` harnesses (`runner/harness/v1-harness.py`, BR-071).
