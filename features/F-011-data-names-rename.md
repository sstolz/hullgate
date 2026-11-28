---
id: F-011
title: Rename the names bound to data, by a fresh start — data volume, database file, key-check label, token prefix, OAuth application
ticket: HULL-66
status: implemented
closed: 2026-11-14
domain: [contracts, operations]
br-introduced: []
br-touched: [BR-038, BR-053, BR-131]
decisions: []
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
capabilities:
  extends: ["Operator tokens", "Backup and restore check", "English contract vocabulary"]
  introduces: []
---

# F-011 · Rename the names bound to data

> **Former names (HULL-58).** In this spec `<old-name>` (upper case `<OLD-NAME>`) stands for the product's former name and `<old-prefix>` for the former ticket prefix; ticket ids keep their numbers under `HULL`. The repository spells neither former name.

## Metadata

| Field | Value |
|-------|-------|
| **Priority** | P1 |
| **Branch** | `feat/HULL-66-data-names-rename` |
| **Last updated** | 2026-11-14 |

## 1. Context & Objective

### Current State

F-010 renamed the product but kept five names on purpose, each bound to data on the production server (F-010 §1 decisions 2 and N3, BR-131 v1). They are the last functional uses of the old product name. Read at `a8fa639`:

| Name | Where it lives |
|---|---|
| Data volume | `deploy/compose.prod.yaml:67` (default), `deploy/env.example:29`, `deploy/ops.sh:51`, `.env` on the production server |
| Database file | `src/hullgate/config.py:19`, `deploy/ops.sh:20` |
| Key-check label | `src/hullgate/cipher.py:34`, pinned digest in `tests/unit/test_br_088_key_check_label.py` |
| Token prefix | `src/hullgate/tokens.py:21`, gate at `tokens.py:261`, patterns at `verify_relay.py:41` and `tests/unit/cli_output_tour.py:21` |
| Gitea OAuth2 application | Gitea UI, `docs/guides/keys-and-upstreams.md:91` |

The operational documents also carry a stale path: the production server directory was renamed on 2026-11-01, yet 58 lines of six runbooks, four lines of `deploy/env.example`, `Makefile:3` and `README.md:42` still name the old directory (`git grep -c`, the F-010 cutover files and POC paths aside).

### Objective

After this feature, none of the five names exists in code, deployment files or operational commands, and the production server runs on `hullgate_data` / `hullgate.db`, rebuilt from scratch.

**Operator facts, 2026-11-01**: nobody uses the tool, breaking changes are free, all data on the production server is disposable. So no data moves: the instance starts fresh under the new names.

### Expected Impact

- Four exceptions leave BR-131 (volume, file, label, prefix; the fifth name, the OAuth application, was on F-010's list only); its kept-names list loses the data-at-rest entry and the directory entry.
- The Compose warning about the volume's project stops at each deploy.
- HULL-58's purge is left with text only: no name it touches is bound to data.

### Open Product Questions

None.

## 2. Scope

- The five names are replaced in code and configuration (§3.1), with no compatibility path.
- `deploy/ops.sh` and rotation read `hullgate-<ts>.db` only (BR-038 v2).
- BR-131 v2: exception removed, kept-names and sweeps updated, sweep H added (§3.4).
- The operational runbooks and `deploy/env.example`: the five names and the production server directory, in commands and expected results.
- Short operator steps for the fresh start on the production server (§3.2), a new section of `docs/guides/update.md`.
- `deploy/smoke/cutover.sh` and `make prod-smoke-cutover` are deleted: they stop being playable here.
- The docstrings and comments that describe a renamed name become false at this PR and are rewritten here: `db.py:1`, `tokens.py:3`, `verify_relay.py:14`, the `ops.sh` and `cipher.py` comments, and any other line sweep H hits.

This spec owns the key-check label; HULL-57 must not touch it (order HULL-66, HULL-57, HULL-58).

## Out of Scope

- **Any data migration**: no volume copy, no conversion of the stored key check, no schema migration. The schema stays at version 6.
- **Free text mentions of the old name**, French or English, that describe no renamed name: other docstrings and comments, the `.gitignore` comment, `brief.md`, `capability-map.md`'s title, `pyproject.toml`'s description, ADRs, BR archives, CHANGELOG history, spec text, `HULL-*` paths: HULL-58 (comment 11145). `src/hullgate/__init__.py:1` is French prose (a French adjective built on the former name), not the name: HULL-57.
- **Deleting `docs/runbooks/hullgate-cutover.md`**: HULL-58.
- **The ssh alias, deploy-key and repository-name tokens** of the F-010 kept list (`gitea-…`, `<repository-url>`, `…_deploy`) and the POC containers: HULL-58, which corrects the runbooks for HTTPS at the same time.
- **Runbook prose in French**: HULL-58.

## 3. Functional Spec

### 3.1 The name table (SCR-01)

The only table of this spec that spells the old names.

| Name | Old | New | How |
|---|---|---|---|
| Docker volume | `<old-name>_data` | `hullgate_data` | code default, `.env` on the production server (§3.2) |
| Database file | `<old-name>.db` | `hullgate.db` | code; a fresh volume |
| Key-check label | `<old-name>/connexions/controle-de-cle/v1` | `hullgate/connections/key-check/v1` | replaced in `cipher.py` |
| Operator-token prefix | `clo_` | `hg_` | replaced, §3.3 |
| Gitea OAuth2 application | `<old-name>` | `hullgate` | operator gesture, §3.2 F4 |
| The production server directory in documents | `<compose-root>/<old-name>` | `<install-dir>` | text; the production server already renamed |

The new label's check of the fixed key `bytes(range(32))` is `ddb55de08262ebfd99acddf9c37f5ad3` (computed while drafting; the old label's gives `42371c4f6be05d193537af8d0d79d19e`, which matches the current pin, so the computation is sound). This digest replaces the pin in `tests/unit/test_br_088_key_check_label.py`.

### 3.2 Fresh start on the production server (SCR-02), operator steps

Played by the operator, as `<user>` in bash, in the clone, after the ordinary deploy steps M1 to M3 of the same runbook: the merged code already runs, on the old volume, with an empty `hullgate.db`. The section spells no old name: it reads it from `.env`. **Form**: six steps `## Étape F1 — …` to `F6`, each with its code block, `**Résultat attendu**`, `**Si le résultat diffère**` and `**À reporter sur #6**`, the literal marker `test_br_042_runbooks.py:23` requires of this runbook; its line says to report on #66. Sessions act on the production server only under BR-043 v2; the secret steps and the removal stay the operator's.

- **F1 Last backup kept**: the file named by M2's `BACKUP: OK backups/hullgate-<ts>.db` line is copied aside as `backups/pre-fresh-start-<ts>.db`; `cp .env .env.pre-rebuild`. That pre-deploy backup ran the previous `ops.sh` before the checkout, so it is the last snapshot of the old file. Not the newest one: M3's `make verify` adds a backup of the empty database.
- **F2 New volume**: `.env` gets `HULLGATE_VOLUME=hullgate_data`. Compose creates it at the next `up`, which is what removes the warning.
- **F3 New connections key**: generated by the operator, per step C2 of `keys-and-upstreams.md`; the old key file is moved aside first, since that step refuses to overwrite. F3's block moves the file and prints the listing; generation stays C2's. This act is the operator's and no session reads the file.
- **F4 Restart and rebuild**: `docker compose -f deploy/compose.prod.yaml --env-file .env up -d`. Expected: `Volume hullgate_data Created`, the container recreated, no line matching `already exists|was created for project`. Then the Gitea application is renamed `hullgate`, in the account that owns it (Settings > Applications > Manage OAuth2 applications), and the test tree is rebuilt by the existing steps: S7 and S9 of `keys-and-upstreams.md`, C6 to C9 of `keys-and-upstreams.md`, upstream and placement of `keys-and-upstreams.md`. Operator tokens are issued anew, `hg_`; Claude.ai reconnects (S10).
- **F5 Check**: `make verify`, `make verify-auth`, `make verify-connections`, then `make verify-relay` under a token created after F4 (R7 of `keys-and-upstreams.md`): its `VERIFY-RELAY list: OK` line is what proves an `hg_` token authenticates.
- **F6 Old volume deleted, on the operator's confirmation**, its own block: reads the old name from `.env.pre-rebuild`, asserts it is not empty, prints it, asserts it differs from the current `.env`'s value and that `docker ps -q --filter volume=<name>` is empty, then `docker volume rm`. Left to the operator, unusable after the fresh start: files of `backups/` under the old prefix (rotation no longer reads them), F-010's leftovers `backups/pre-hullgate-<ts>.db`, `backups/cutover-step10.log`, `.env.pre-hullgate` and the image tag `pre-hullgate`.

The nightly backup cron (fact, arbitration X2) needs no change: between F2 and F4 it prints `aucune base`, then it runs on the new volume. A step colliding with 03:15 ends on `verrou occupé`: play it again.

**Hypotheses** read as the steps go (operator memory, 2026-11-01): the clone is `<install-dir>` and `.env`'s secret paths name it; the Gitea application is owned by `hullgate-test`. C7 reads the client id after the rename.

### 3.3 The token prefix (SCR-03)

`PREFIX = "hg_"`; value `hg_` + 256 bits in base64url; `_LEAK` and the output-tour pattern follow, anchored so that the shorter prefix cannot match inside another word: `(?<![A-Za-z0-9_-])hg_[A-Za-z0-9_-]{20,}`. No other pattern spells the prefix in `src/`, `scripts/`, `deploy/` or `poc/` (`git grep` at `a8fa639`); BR-053 says the prefix lets BR-003's secret guard recognise a value, and nothing in the repository implements that recognition by this prefix beyond these two patterns.

### 3.4 Sweeps and the BR-131 list (SCR-04), in `tests/unit/test_br_131_contract_surface.py`

- `KEPT["G"]` loses the data-at-rest entry and the production server's directory entry; the ssh, repository, deploy-key and POC entries stay for HULL-58.
- `test_br_131_key_check_label_is_kept` is replaced by a test that the old label's stem is absent from `src/`.
- **Sweep H** (new, positive fixture): its pattern, assembled from fragments (`"clo" + "isonne_data"`) so that it spells no former name, has five alternatives: the volume, the file, the label's stem, the prefix `\bclo_` (which HULL-58's `<old-name>|<old-prefix>` pattern would not catch) and the production server directory except its `-poc-go` POC paths. In the sweep paths outside `EXCLUSIONS` it returns no line. The application's bare name cannot be swept without firing on HULL-58's text: it is read at `keys-and-upstreams.md:91`. Every other new test builds a former name the same way.
- `EXCLUSIONS` gains `tests/unit/test_br_042_runbooks.py` (it pins `hullgate-cutover.md`'s text at lines 250 and 255, where sweeps G and H fire; it goes with that file in HULL-58); `deploy/smoke/cutover.sh`'s entry leaves with the file; the count stays 7.
- Pinned tables: the volume default enters; `prod-smoke-cutover` leaves; `RUNBOOKS`' CLI count of `update.md` stays `1`: the F steps cite others by id and quote no `hullgate admin` call. `test_br_042_cutover_rehearsal_plays_every_block` goes with the script.

Tests name `hullgate.db` wherever they named the old file (`tests/support.py:82`, `tests/container/conftest.py:141`, 38 lines). `tests/unit/test_br_038_backup_order.py` keeps a foreign-prefix fixture, under a neutral word, to assert rotation ignores it.

## 4. Business Rules

### 4.1 Overlap scan (`br-identifiers.md` §Overlap)

No new rule: the fresh start removed the three candidates of the first draft (conversion of the key check, refusal of a misnamed database, intactness of the source). BR-133 to BR-139 stay in F-011's range, unused; a number freed is not reused. With no candidate the filter has nothing to run on. The three evolutions below target rules that state a changed token; each is a **new version (C2)**: a state that satisfied the rule no longer does. BR-088 is not touched: it names a « fixed constant », not the label; only its test's pin moves. BR-033 and BR-069 are not touched: no reason and no migration is added.

### BR-131 · The gateway's closed vocabularies and its product name are English (v2)

> **Extracted on 2026-11-14.** Current version: [`business-rules/BR-131.md`](../business-rules/BR-131.md).
> The content below reflects the state of this BR at the time of feature F-011 (v2).

*`behavioral` · `strict` · Candidate state: `verified` — existing control entries, plus sweep H.*

**Rule** — As v1, with three changes: the closed-vocabulary clause reads « the names of compose projects, networks, volumes, files and environment keys »; a sentence is added: « The identifiers of data at rest are English: the key-check label `hullgate/connections/key-check/v1` and the operator-token prefix `hg_`. »; and the closing sentence becomes: Excepted, and only this: the isolation bench's verdict vocabulary (BR-037, BR-071), until the ticket that migrates it.

**Clarifications** — the clarification on the key-check label is removed. « Historical data keeps its spelling » stays, widened under §Rules touched.

**At closure** — three lines of the rule file turn false and are rewritten at `close-feature` Phase 3: the clarification « the pinned tables are those of F-010 §3.2 » (F-011 §3.1 adds the volume default); the control note on `test_br_088_key_check_label.py` (« so the kept label cannot be renamed silently »); the Implementation References « `cipher.py` (kept key-check label) » and « Docs: `hullgate-cutover.md` ».

### BR-053 · Operator token: value shown once, only the fingerprint is kept (v3)

> **Extracted on 2026-11-14.** Current version: [`business-rules/BR-053.md`](../business-rules/BR-053.md).
> The content below reflects the state of this BR at the time of feature F-011 (v3).

*Classification unchanged · Candidate state: `verified` — `tests/integration/test_br_053_operator_token.py`, its value regex moved to `^hg_[A-Za-z0-9_-]{43}$`.*

**Rule** — Unchanged.

**Clarifications** — the value's prefix is `hg_`. A token under any other prefix is refused as unknown.

### BR-038 · Consistent hot backup, verified, without losing the previous ones (v2)

> **Extracted on 2026-11-14.** Current version: [`business-rules/BR-038.md`](../business-rules/BR-038.md).
> The content below reflects the state of this BR at the time of feature F-011 (v2).

*Classification unchanged · Candidate state: `verified` — `tests/unit/test_br_038_backup_order.py`, a foreign prefix now ignored.*

**Rule** — Unchanged.

**Clarifications** — the stopped-service case reads « the data volume ». Rotation and `restore-check`'s default selection read `hullgate-<ts>.db` only, ordered by timestamp; a file of another prefix is neither rotated nor selected.

### Rules touched

- `BR-131`, in the v2 above, no version of its own — The clarification « Historical data keeps its spelling » reads « audit rows written before HULL-57's values were deployed (BR-066 v4) » in place of « audit rows written before migration 6 (BR-066 v4) »: HULL-57 writes English audit values for new rows, the French rows written since migration 6 stay as they are, and BR-066 (`strict`) forbids editing them. Read by hand at closure.

## 5. Acceptance Criteria

*Names*

- [ ] **AC-01** (BR-131) — `cipher.key_check(bytes(range(32))).hex()` is `ddb55de08262ebfd99acddf9c37f5ad3`, and `grep -c '^_CHECK_LABEL = ' src/hullgate/cipher.py` prints `1`.
- [ ] **AC-02** (BR-131) — `Config.db_path` ends in `hullgate.db`; `deploy/ops.sh` `DB` agrees; Compose resolves the volume to `hullgate_data` with `HULLGATE_VOLUME` unset, and `env_get`'s default agrees.
- [ ] **AC-03** (BR-038) — a directory of `hullgate-<ts>.db` and `other-<ts>.db` files: rotation removes only the former past 14; `restore-check` selects the newest `hullgate-` one.

*Token*

- [ ] **AC-04** (BR-053) — `token create` prints a value matching `^hg_[A-Za-z0-9_-]{43}$`; it authenticates; only its SHA-256 is stored.
- [ ] **AC-05** (BR-053) — a stored token whose value does not start with `hg_` is refused like an unknown one.
- [ ] **AC-06** (BR-053) — `verify_relay`'s leak check holds output with an `hg_` token back and does not fire on `xhg_` + 30 characters.

*Sweeps and documents*

- [ ] **AC-07** (BR-131) — sweep H returns nothing over its paths, its positive fixture matches, `test_br_131_kept_tokens_are_live` passes with the shortened list.
- [ ] **AC-08** (BR-131) — `grep -c '^[A-Z_]*_PATH=<secrets-dir>/' deploy/env.example` prints `3`.
- [ ] **AC-09** — `make gate`, `make test-container`, `make prod-smoke` pass; `make -n prod-smoke-cutover` fails and `deploy/smoke/cutover.sh` is absent.
- [ ] **AC-10** (BR-042) — `test_br_042_every_step_is_complete` passes with steps F1 to F6 in the runbook, sweep H finds nothing there; read by hand at review: F6's removal is alone in its block.

*On the production server, played and reported by the operator (BR-043 v2)*

- [ ] **AC-OP-01** — after F5: `VERIFY: 6/6 OK`; `VERIFY-AUTH: 4/4 OK`; `VERIFY-CONNECTIONS: 4/4 OK`; `VERIFY-RELAY list: OK` under a token created after F4, and `VERIFY-RELAY: 5/5 OK`, or `4/5 OK` with `sensitive: SKIP` (no sensitive role on the production server); F4's `up` output holds no line matching `already exists|was created for project`; `docker volume ls` shows `hullgate_data`, and the old volume no more after F6; application named `hullgate`.

## 6. Diagrams

Skipped: the steps of §3.2 are linear, with no user flow, no exchange between layers, no state and no schema change.

## 7. States & Captures

- **Package-status**: absent (no visual surface).

## 9. Implementation Notes

### Dependencies (prerequisite features)

- F-010 — English contracts (implemented). Its cutover has been played on the production server.
- Downstream: HULL-57 must not touch the label (already renamed here); HULL-58 purges text. HULL-58 owns `deploy/smoke/fake_gitea.py`, orphaned here.

### Technical Constraints

- A published migration is never modified (BR-069); no migration is added.
- The old names may appear in this spec's §3.1 and §3.4; nowhere in new code, test or document material: sweep H and the new tests build them from fragments.
- The ordinary deploy precedes F1 and is harmless: it starts an empty `hullgate.db` on the old volume, the old file beside it untouched, and F4's `up -d` moves to the new volume.

### Architecture Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| Data | Fresh start, nothing migrated (operator, 2026-11-01; arbitration Z1) | nobody uses the tool, all data on the production server is disposable |
| Key-check label | Simply replaced, no version, no conversion | an empty database has no stored check |
| Operator tokens | Issued anew at the rebuild; clean break (F-010 decision 7) | no live client |
| Delivery | One PR; a deployment after merge is ordinary | arbitration Z1 |
| Where the steps live | A section of `update.md`, no dedicated runbook | short, one-time; the tree rebuild reuses existing steps |
| New connections key (F3) | Kept, though an empty database would adopt the old key at its first ciphertext (operator, arbitration Z1) | hygiene: the old key sealed the old data and stays with the kept backup |
| Who creates the volume | Compose (F4) | the warning's disappearance depends on it |
| Old-prefix backups | Left to the operator; rotation stays strict | a name-agnostic pattern would let rotation delete foreign files |
| New label's spelling | `hullgate/connections/key-check/v1` | the ticket asks an English `hullgate/…` label; path shape kept |
| Scope of runbook edits | The five names and the directory, in commands and results; prose stays French | ticket's « the runbooks »; HULL-58 translates |
| F-010's rehearsal | Deleted here (arbitration X3) | it stops being playable here |
| Former names in sweeps | Built from fragments (arbitration X5) | no sweep or test spells one; HULL-58 inherits nothing to rewrite |
| Capabilities (written at the freeze, step 24) | Candidate: `extends: [Jetons opérateur, Sauvegarde et contrôle de restauration, English contract vocabulary]`, `introduces: []`, the map rows citing the `br-touched` rules | INV-047 |

No new `status` value is proposed (step 23a: nothing to apply). No architectural decision in the sense of `conventions/adr.md`: `decisions` stays `[]`. No term coined.

### Data Model Impact

- Migration: no. `SCHEMA_VERSION` stays 6.

### Components to Create / Modify

**Modified**
- `src/hullgate/cipher.py` — label
- `src/hullgate/config.py` — `DB_FILENAME`
- `src/hullgate/tokens.py`, `src/hullgate/verify_relay.py`, `src/hullgate/db.py` — prefix, pattern, docstrings
- `deploy/ops.sh`, `deploy/compose.prod.yaml`, `deploy/env.example`, `Makefile`, `.gitignore` (`/.env.pre-rebuild` added, `/.smoke-cutover/` removed)
- `docs/runbooks/`: `keys-and-upstreams.md`, `install.md`, `update.md` (with §3.2), `backup-restore.md`, `keys-and-upstreams.md`, `keys-and-upstreams.md`; `README.md:42`
- `tests/unit/test_br_131_contract_surface.py`, `tests/unit/test_br_088_key_check_label.py`, `tests/unit/cli_output_tour.py`, `tests/support.py`, and the tests that spell the old file or prefix
- `CHANGELOG.md` `[Unreleased]`: the prefix break and the fresh start

**Deleted**
- `deploy/smoke/cutover.sh`, the `prod-smoke-cutover` target of the `Makefile`

### Pitfalls Discovered

Implementation deltas (2026-11-14, HULL-66 implementation act):

- **The isolation bench opened the old database file.** `poc/isolation-bench/runner/adapter_v1.py` and `harness/v1-harness.py` chmod and open `/data/<file>` inside the gateway image; §9's component list did not name them, sweep H (whose paths include `poc/isolation-bench/runner`) did. Renamed to `hullgate.db`: the bench would otherwise target a file the new image never creates.
- **`( set -e; … ) || echo KO` guards nothing in bash**: `set -e` is ignored inside a command whose status `||` tests, so the block runs to its end. The F blocks use one explicit `check || { echo "Fn: KO …"; exit 1; }` per verification, inside a subshell with no outer `||`.
- **Rotation under one prefix**: `backups_newest_first` is now `grep '^hullgate-<ts>.db$' | sort -r`; with a single prefix the name order is the timestamp order, so F-010's reorder step is gone.
- **Pinned volume tables**: `compose.yaml` (development) declares an unnamed `data` volume; the pinned table holds it as `{"data": ""}` beside production's default.
- **Test token values**: the unknown-token fixtures `clo_inconnu` read `hg_unknown`; the leak-test case id `clo` reads `prefix`.
- **F3** moves the old key to `secrets/connections_key.pre-rebuild`; **F4** keeps its `up` output in `backups/fresh-start-f4.log` for the warning count.
- **F4 is `up -d --force-recreate`, not `up -d`** (review 554, B1): once `.env` names a new volume, a plain `up -d` creates `hullgate_data` but, under Compose v5.7.1 (measured by the review), keeps the running container mounted on the old volume; v5.6.1 (the toolbox) recreates it. `--force-recreate` ends on the new volume under both, which `tests/container/test_br_131_fresh_start_volume.py` pins. Every F4 and F5 check would then pass on the old volume and only F6 would refuse. F4 therefore recreates the container and prints the volume it mounts (`F4: OK conteneur monté sur hullgate_data`, else `F4: KO`); do not drop the flag.
- **The production server directory split from its path** (review 554, B2, B3): O4's `git clone … <old> && cd <old>`, its `destination path` error, the backup cron filter and C7's application name spell the old name without `<compose-root>/`, so sweep H missed them; it now carries those forms too.

## 10. Phases

### Phase 1 — Code and documents

- [x] Names, prefix, sweeps, pinned tables, BR-131 list
- [x] Runbooks, `env.example`
- [x] `make gate`, `make test-container`, `make prod-smoke`

### Phase 2 — Fresh start (operator, after merge)

- [ ] F1 to F6 on the operator's confirmation
- [ ] AC-OP-01 reported on the ticket

## 11. Tests

### unit

| Test | BR / Concern | File |
|------|--------------|------|
| label pin and its negative control | BR-131 | `tests/unit/test_br_088_key_check_label.py` |
| sweep H, shortened `KEPT`, pinned tables | BR-131 | `tests/unit/test_br_131_contract_surface.py` |
| rotation and selection ignore a foreign prefix | BR-038 | `tests/unit/test_br_038_backup_order.py` |

### integration

| Test | BR / Concern | File |
|------|--------------|------|
| value shape, refused other prefix, leak pattern | BR-053 | `tests/integration/test_br_053_operator_token.py`, `test_verify_relay.py` |

### container

| Test | BR / Concern | File |
|------|--------------|------|
| volume name resolved by Compose | BR-131 | `tests/container/test_br_131_network.py` |
| database path in the running image | BR-131 | `tests/container/test_br_038_backup.py`, `tests/container/test_br_039_restore_check.py` |

## 12. Risks & Watchpoints

- **The last backup kept at F1 reads `key_different` under the new code**: it is restorable only by the previous commit with the old key. It is a keepsake: the operator keeps the old key file with it or deletes both.
- **`hg_` is short**: a leak pattern without its left guard would match inside other words (AC-06).
- **F-010's `hullgate-cutover.md`** spells names that no longer exist; it stays excluded until HULL-58 deletes it.

## Stress-test

**Passe du 2026-11-01** — model:claude-fable-5-1 (strongest tier, fresh context), six axes. Arbitration: orchestrator, `milestone-1.0.0-arbitration-1.md`; report: `.cairn/runs/acts/HULL-66-stress-test-1.md`.
**24 findings — 2 critical, 9 major, 13 minor ; 24 retenus, 0 écartés.**

Critiques :
- **C1** · axe F · §3.2 C4 vs BR-135 Rule — C4's snapshot writes in the source volume, against the Rule as worded. Sort : corrigé — the Rule bears on the source database file; `test_br_135`'s assertion rewritten (`volume rm` in one block, no unguarded mount outside C4).
- **C2** · axe F · §3.6 fault 1 vs §3.2 C7 — with C7 reading its destination from `.env`, a stale `.env` writes into the source. Sort : corrigé — C7 spells `hullgate_data` and guards against `SRC_VOLUME`; fault 1 now fails at C8.

Majors :
- **M1** · axe A · §3.2 C9, AC-OP-01 — `verify-auth` reads no token. Sort : corrigé — a `curl` probe in C9, `200` and `401`; `tools/list` rather than the report's `ping`, the form `tests/support.py:591` and `verify_relay.py` use. Clause re-read: AC-OP-01 prints a code.
- **M2** · axe F · §3.5 vs §Out of Scope — docstrings spell the names while sweep H covers `src/`. Sort : corrigé — they are rewritten here (§2, §Out of Scope; arbitration X5).
- **M3** · axe F · §4 BR-088 v3 — `blocking` against the file's `strict`. Sort : corrigé — classification unchanged, `strict`.
- **M4** · axe E · §12, BR-135 — nothing enforced the deploy order. Sort : corrigé — option (a), step C0 (arbitration X1); the accidental deploy's recovery in §12.
- **M5** · axe B · §3.2 Rollback — a second attempt stops at C7. Sort : corrigé — block C5a, played by the rehearsal, AC-18.
- **M6** · axe B · §3.2 C13 — `SRC_VOLUME` lost or mis-set days later. Sort : corrigé — read from `.env.pre-data-rename`, printed, asserted.
- **M7** · axe B · §3.3, BR-133 — a right key that cannot prove itself reads `key_different` for good. Sort : corrigé — exit named in §3.3, BR-133 and the connections runbook; `connection status` names the rows; AC-20.
- **M8** · axe A · §4 BR-135 — three obligations in one Rule, no response. Sort : corrigé — one obligation with its response; the others are clarifications.
- **M9** · axe A · §5 AC-08, AC-16, AC-17 — clauses with no instrument. Sort : corrigé — each paired with sweep H, a `grep` or `test_br_135`'s pinned list; AC-08's fixture clause marked read by hand. Clause re-read.

Minors :
- **m1** · axe A · BR-133 Rule — two obligations. Sort : corrigé — the transaction moved to the clarifications.
- **m2** · axe A · AC-OP-01 — `DOWNTIME` absent, warning without a pattern. Sort : corrigé — both written.
- **m3** · axe B · §3.2 R3 — `BACKUP: aucune base` not expected there. Sort : corrigé.
- **m4** · axe F · §3.2 vs §12 — C1 and §12 disagreed on the hypotheses. Sort : corrigé — H5 (cron) became a fact (arbitration X2), the former H6 is H5, read at C11.
- **m5** · axe F · §6.2 — `v1 --> none` missing. Sort : corrigé.
- **m6** · axe B · §3.3 — two converters. Sort : corrigé — version re-read inside the transaction, AC-05.
- **m7** · axe B · §3.3 — the `status` call on an unopened database. Sort : corrigé.
- **m8** · axe A · §3.2 C10 — a loose pattern caught the aside copy. Sort : corrigé — one-word pattern.
- **m9** · axe A · §3.5 — the bare name cannot be swept. Sort : corrigé — four names plus the directory; the application read at C11.
- **m10** · axe A · §5 — declaration term. Sort : corrigé — yes, written in §9 by the orchestrator, no criterion (arbitration X17).
- **m11** · axe F · frontmatter — `capabilities`. Sort : corrigé — candidate and derivation in §9; the key is written at step 24.
- **m12** · axe C · §9 Q2 — a third option. Sort : corrigé — option (c) listed with its cost.
- **m13** · axe E · §3.5, §12 — sweep from fragments. Sort : corrigé — done here (arbitration X5), the §12 item removed.

**Passe du 2026-11-01** — model:claude-fable-5-1 (strongest tier, fresh context), second pass, targeted after arbitration Z1. Arbitration: `milestone-1.0.0-arbitration-1.md` §Pass 4 (A3, A14, default rule); report: `.cairn/runs/acts/HULL-66-stress-test-2.md`.
**17 findings — 1 critical, 4 major, 12 minor ; 17 retenus, 0 écartés.**

Critiques :
- **C1** · axe F · §3.2 intro vs F1, F4 — after the ordinary deploy, `make backup` snapshots the empty database. Sort : corrigé — arbitration A3: F1 copies the file M2's `BACKUP: OK` line named, F4 is `up -d` alone.

Majors :
- **M1** · axe A · §3.2, AC-10 vs BR-042 — the steps had no form and AC-10 no instrument. Sort : corrigé — six `## Étape` steps with BR-042's markers, AC-10 reads `test_br_042_every_step_is_complete`, CLI count stays 1. Clause re-read.
- **M2** · axe A · §3.2 F4 — `hullgate-test` appears in no document. Sort : corrigé — F4 says the owning account; the name moved to the Hypotheses with its source.
- **M3** · axe B · §3.2 F4, AC-OP-01 — the expected output was the deploy's. Sort : corrigé — F4's expected result written, AC-OP-01 reads F4's `up` output.
- **M4** · axe F · §4 BR-131 v2 vs §3.3 — the prefix read as carrying the product's name. Sort : corrigé — volumes join the closed-vocabulary clause; label and prefix are named as English identifiers.

Minors :
- **m1** · axe A · §3.2 F3 — « vault key ». Sort : corrigé — « connections key ».
- **m2** · axe C · §3.2 F3 — no reason given. Sort : corrigé — decision row in §9.
- **m3** · axe B · §3.2 F6 — empty name. Sort : corrigé — asserted non-empty first.
- **m4** · axe A · AC-OP-01 vs F5 — readings incomplete. Sort : corrigé — the three lines written, `hg_` proven by `VERIFY-RELAY list: OK`. Clause re-read.
- **m5** · axe A · AC-01 — no expression. Sort : corrigé — the `grep -c`. Clause re-read.
- **m6** · axe A · §3.4 — decidable condition. Sort : corrigé — written as a fact, count 7.
- **m7** · axe F · §3.2 Hypotheses — the client-id hypothesis protects nothing. Sort : corrigé — « C7 reads the client id after the rename ».
- **m8** · axe F · §1 — five exceptions. Sort : corrigé — four.
- **m9** · axe F · §4 BR-131 v2 — lines turning false. Sort : corrigé — listed for closure.
- **m10** · axe D · §3.2 — the cron left the text. Sort : corrigé — arbitration A14, one line.
- **m11** · axe D · F6 — F-010's leftovers. Sort : corrigé — listed, left to the operator.
- **m12** · axe C · §9 — dead `.gitignore` entry. Sort : corrigé — removed in the same edit.

Addendum 2026-11-01, retouch 6 (operator decision, no new pass played): HULL-57 and HULL-58 stay plain tickets with no spec and no new rule, their citations here now name the ticket, in the trace too; the BR-131 clarification on historical audit rows that the HULL-57 spec carried moves to §Rules touched and lands in v2, so it adds no version.

## 13. Changelog

| Date | Author | Change |
|------|--------|--------|
| 2026-11-01 | model:claude-opus-5-5 | Draft, HULL-66, delegated drafting act. Phases 2 to 4 under delegated autonomy; assumptions in §9. Stress-test not yet played |
| 2026-11-01 | model:claude-opus-5-5 | Stress-test pass 1 applied per the milestone arbitration (X1 to X5, X17), `ready-for-freeze` |
| 2026-11-01 | model:claude-opus-5-5 | Rework, arbitration Z1: fresh start replaces the cutover; BR-133 to BR-135, migration 7 and the rehearsal removed; Q1 and Q2 closed; back to `draft`. Pass 1's trace kept: its outcomes describe the text before this rework |
| 2026-11-01 | model:claude-opus-5-5 | Stress-test pass 2 applied (arbitration Pass 4), `ready-for-freeze` |
| 2026-11-14 | model:claude-opus-5-5 | Implementation (HULL-66): `in-development`; §4 rules extracted (BR-131 v2, BR-053 v3, BR-038 v2), §9 Pitfalls written. `make gate`, `make test-container`, `make prod-smoke` not yet played: the session's Docker engine refused every container |
| 2026-11-14 | model:claude-opus-5-5 | Close-feature (HULL-66) after the engine came back: gate, container tests and prod smoke green; container backup fixtures moved to the `hullgate-` prefix (BR-038 v2); `implemented` |
| 2026-11-16 | Codex (implementation-coder, model:gpt-6-astra) | F-022 slice 1, freeze `10d166e`: S11 guide references, P1 private trial references and F1 private procedure mentions only where applicable. |
| 2026-11-16 | Codex (implementation-coder, model:gpt-6-astra) | F-022 slice 2, freeze `10d166e` (retouch 2 `7aac9e6`): §3.2 substitutions only; inline rule clarifications without version where applicable. |
