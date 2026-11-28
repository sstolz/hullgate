# Update and rollback

## Step mapping

Source roles refer to the private server runbooks. Identifiers are preserved; the merged
core keys, connections and relay namespaces do not collide.

| Source role | Runbook step | Guide step |
|---|---|---|
| update | M1 | M1 |
| update | M2 | M2 |
| update | M3 | M3 |
| update | M4 | M4 |
| update | F1 | F1 |
| update | F2 | F2 |
| update | F3 | F3 |
| update | F4 | F4 |
| update | F5 | F5 |
| update | F6 | F6 |

Replace the documented placeholders using the [parameter table](install.md#parameters).
Commands run as your SSH account on the target unless marked as workstation or browser actions.
Use the documented defaults below, or consistently substitute your own installation paths.


Goal: deploy a release tag or SHA and, if the deployment fails, go back to the previous one. Prerequisites: the installation (`install.md`) is done and `make verify` has given `VERIFY: 6/6 OK` at least once.

`make deploy` chains: backup (if a database exists), `git fetch`, extraction of the reference, image build, container replacement, wait for the healthy state (`/readyz` 200, 60 s at most). It writes `.deployed-ref` only after success and never goes back on its own: on failure, it prints the return command. The service is cut during the container replacement (a few seconds, `DOWNTIME` line).

Each step: a block to paste, the expected result, what to do if it differs.

---

## Step M1 — Read the deployed reference and choose the new one

```sh
cd /opt/hullgate && make status &&
PREVIOUS_REF=$(sed -n '1p' .deployed-ref) && test -n "$PREVIOUS_REF" &&
printf 'Healthy rollback SHA: %s\n' "$PREVIOUS_REF" &&
git fetch origin && git log --oneline -5 origin/main
```

**Expected result**: the `hullgate` container `Up … (healthy)`, `deployed ref: <ref>`, `previous: <previous-ref>`, then the last five commits of `main`. Choose the release tag or SHA you intend to run; retain the printed healthy SHA before M2, even if you open another shell.

**If the result differs**: `.env absent` → you are not in the clone, or the installation is not done. The container is not `healthy` → look at `make logs` before deploying over it.


## Step M2 — Deploy the new reference

Before deploying, complete the pre-deployment checks in `docs/guides/admin.md`
step P1 for the chosen SHA (network reading, `TRUSTED_PROXIES` and read-only validation).
Replace `<ref>` with that SHA:

```sh
cd /opt/hullgate && time make deploy REF=<ref>
```

**Expected result**: `BACKUP: OK backups/hullgate-<timestamp>.db …` (prior backup), the build, `DOWNTIME <s>` then `DEPLOY: OK <ref>`.

**If the result differs**: `DEPLOY: KO <ref> — prior backup failed` → nothing was built or replaced: the service still runs on the old reference; read the `BACKUP: KO` line (disk space: `df -h <compose-root>`). `TRUSTED_PROXIES required in .env` followed by `DEPLOY: KO <ref> — image build failed` → the running service is unchanged: follow `docs/guides/admin.md` step P1, then retry M2. Other `image build failed` errors also leave the running service unchanged: fix the build error and retry. `DEPLOY: KO <ref> — /readyz ≠ 200 …` → the service is not healthy on the new reference: go to step M4. `ref not found` → the SHA or tag does not exist on `origin`: check it with M1. `lock held` → a scheduled backup is running: paste again in a minute.


## Step M3 — Verify

```sh
cd /opt/hullgate && make verify && docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin connection status | tail -n 1
```

**Expected result**: six lines `VERIFY … OK …` and `VERIFY: 6/6 OK`; `deployed-ref` shows the new reference. Since F-006, the last line says `connections key: usable`: `/readyz` stays green without a connections key (BR-088), only this line shows it.

**If the result differs**: a `KO` line → `install.md`, step O7, reading of the `KO`; if the service is degraded, go to M4. `connections key: key_absent`, `key_invalid` or `key_different` → `keys-and-upstreams.md`, step C5.


## Step M4 — Return to the previous reference (if M2 or M3 failed)

The output of a failed `make deploy` prints the exact command (`Rollback to the previous ref: make deploy REF=<ref>`). Copy it, or:

```sh
cd /opt/hullgate && make deploy REF=<previous-ref> && make verify
```

Use the healthy SHA recorded at M1. `.deployed-ref` changes only after deployment success: after a failed M2 its first line still holds that SHA; after a successful M2 followed by a failed M3, use the prior SHA you recorded, not the new first line.

**Expected result**: `DEPLOY: OK <previous-ref>` then `VERIFY: 6/6 OK`. The backup taken at the start of M2 stays in `backups/`; this command does not restore the database. Use only a previous release compatible with the current schema; otherwise stop and follow the checked backup recovery procedure.

**If the result differs**: the return itself fails → stop there, record the diagnostic locally; restoring a backup is described in `backup-restore.md` (step S4), to be played only after reading.


---

# Fresh start under the hullgate names (F-011, once only)

Use this optional procedure only when you deliberately want a fresh empty database on
`hullgate_data`, replacing a differently named old volume. It is destructive and migrates no
data. Keep the old database, its matching code reference and its encryption key together.
Do not use it for a normal version upgrade. Run as your SSH account in bash in the clone.
The old volume stays until F5 succeeds and you explicitly run F6. A scheduled backup may
report no database between F2 and F4; a held lock means retry later.

## Step F1 — Keep the last backup of the old database

Keep a backup of the old database before changing anything. For a legacy migration, the backup to keep is the one named by the `BACKUP: OK backups/hullgate-<timestamp>.db` line of M2: taken by the previous `ops.sh` before extraction, it is the last copy of the old database. It is not the most recent: the `make verify` of M3 added a backup of the empty database. Replace `<timestamp>` with the timestamp of that line:

```sh
cd /opt/hullgate && TS=<timestamp> && ( echo "$TS" | grep -Eqx '[0-9]{8}T[0-9]{6}Z' || { echo "F1: KO malformed timestamp : $TS"; exit 1; }; test -s "backups/hullgate-$TS.db" || { echo "F1: KO backups/hullgate-$TS.db absent"; exit 1; }; test ! -e .env.pre-rebuild || { echo "F1: KO .env.pre-rebuild already exists"; exit 1; }; cp "backups/hullgate-$TS.db" "backups/pre-fresh-start-$TS.db" && cp -p .env .env.pre-rebuild && ls -l "backups/pre-fresh-start-$TS.db" .env.pre-rebuild && echo "F1: OK" )
```

**Expected result**: the two files listed (the database copy, `.env.pre-rebuild` as `-rw-------`), then `F1: OK`.

**If the result differs**: `malformed timestamp` or `absent` → reread the `BACKUP: OK` line of M2 (`ls -l backups/ | tail -n 5`) and paste again with the right timestamp. `.env.pre-rebuild already exists` → F1 was already played: do not overwrite it, go to F2.


## Step F2 — Name the new volume in .env

```sh
cd /opt/hullgate && ( if grep -q '^HULLGATE_VOLUME=' .env; then sed -i 's/^HULLGATE_VOLUME=.*/HULLGATE_VOLUME=hullgate_data/' .env; else printf 'HULLGATE_VOLUME=hullgate_data\n' >> .env; fi; test "$(grep -c '^HULLGATE_VOLUME=' .env)" = 1 || { echo "F2: KO duplicate lines HULLGATE_VOLUME"; exit 1; }; grep '^HULLGATE_VOLUME=' .env && echo "F2: OK" )
```

**Expected result**: `HULLGATE_VOLUME=hullgate_data` then `F2: OK`. The volume is not created here: Compose creates it in F4, and that is what makes the project warning of each deployment disappear. Do not run `docker volume create`.

**If the result differs**: `duplicate lines HULLGATE_VOLUME` → open `.env` (`${EDITOR:-nano} .env`), keep a single line `HULLGATE_VOLUME=hullgate_data`, paste again.


## Step F3 — Set the old connections key aside, then create a new one (C2)

The old key sealed the old data and stays with the backup kept in F1. Step C2 of `keys-and-upstreams.md` refuses to overwrite an existing key: this block therefore sets the old file aside and prints the list; creation remains that of C2, played by you right after. No session reads this file.

```sh
cd /opt/hullgate && ( test -e secrets/connections_key || { echo "F3: KO secrets/connections_key absent"; exit 1; }; test ! -e secrets/connections_key.pre-rebuild || { echo "F3: KO secrets/connections_key.pre-rebuild already exists"; exit 1; }; mv secrets/connections_key secrets/connections_key.pre-rebuild && ls -ln secrets/ && echo "F3: OK — now run C2" )
```

Then play the block of step C2 of `keys-and-upstreams.md`, as is.

**Expected result**: the list of `secrets/` shows `connections_key.pre-rebuild` (10001, `-r--------`) and no more `connections_key`, then the F3 success line; after C2, `10001:10001 400`.

**If the result differs**: `connections_key absent` → the key was already set aside: check with `ls -ln secrets/` and play C2. `connections_key.pre-rebuild already exists` → F3 was already played: do not overwrite it; if `connections_key` also exists, C2 is already done, go to F4.


## Step F4 — Restart on the new volume and rebuild the test tree

```sh
cd /opt/hullgate && docker compose -f deploy/compose.prod.yaml --env-file .env up -d --force-recreate 2>&1 | tee backups/fresh-start-f4.log; echo "warning lines : $(grep -cE 'already exists|was created for project' backups/fresh-start-f4.log)"; docker volume ls --format '{{.Name}}' | grep -x hullgate_data; CID=$(docker compose -f deploy/compose.prod.yaml --env-file .env ps -q hullgate); V=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$CID"); if [ "$V" = hullgate_data ]; then echo "F4: OK container mounted on $V"; else echo "F4: KO container mounted on ${V:-none}, not on hullgate_data"; fi
```

`--force-recreate` is necessary: depending on the Compose version, a plain `up -d` creates the volume `hullgate_data` but keeps the container running, still mounted on the old volume.

**Expected result**: a line `Volume hullgate_data … Created`, the `hullgate` container recreated (`Recreated`) then started, `warning lines : 0`, `hullgate_data`, then `F4: OK container mounted on hullgate_data`.

Then, once `F4: OK` is printed, by hand, in this order:

1. At Gitea, logged in with the account that owns the OAuth application (**Settings → Applications → Manage OAuth2 Applications**): rename the application to `hullgate`. Use the account that owns your application.
2. The test tree, through the existing steps: S7 and S9 of `keys-and-upstreams.md`; C6 to C9 of `keys-and-upstreams.md` (C7 rereads the client identifier after the renaming); the upstream and the placement of `keys-and-upstreams.md` (R5, R6).
3. Operator tokens are issued from scratch (they start with `hg_`); reconnect Claude.ai (S10).

**If the result differs**: `F4: KO container mounted on <old-volume>` → the container was not recreated: do not rebuild the tree, paste the block again (it is `--force-recreate` that moves the mount) until `F4: OK`. `F4: KO container mounted on rien` → the container is not running: `make logs`. A line `already exists` or `was created for project` → the volume already existed: `docker volume inspect hullgate_data` and record the diagnostic locally before going further. No `hullgate_data` → reread `grep HULLGATE_VOLUME .env` (F2). The container does not start → `make logs`; a missing secret file makes creation fail (C2, F3).


## Step F5 — Check the new instance

Replace `<role-path>` with the role from S7 and `<connection-name>` with the connection from C9:

```sh
cd /opt/hullgate && make verify && make verify-auth ROLE=<role-path> && make verify-connections CONNECTION=<connection-name>
```

Then play the first block of step R7 of `keys-and-upstreams.md`: its token is created after F4, and its line `VERIFY-RELAY list: OK` proves that an `hg_` token authenticates.

**Expected result**: `VERIFY: 6/6 OK`, `VERIFY-AUTH: 4/4 OK`, `VERIFY-CONNECTIONS: 4/4 OK`; for R7, `VERIFY-RELAY list: OK` and `VERIFY-RELAY: 5/5 OK`, or `VERIFY-RELAY: 4/5 OK` with `sensitive: SKIP` (no sensitive role on the server).

**If the result differs**: a `KO` line from `make verify` → `install.md`, step O7. `connections key: key_absent` → C2 was not played (F3). `VERIFY-RELAY list: KO` → R5, R6, then R7.


## Step F6 — Delete the old volume (on your confirmation, alone in its block)

Paste this block only once F5 is green. It reads the old name in `.env.pre-rebuild`, checks that it is not empty, prints it, checks that it differs from the one in `.env` and that no container uses it, then deletes it:

```sh
cd /opt/hullgate && ( OLD=$(sed -n 's/^HULLGATE_VOLUME=//p' .env.pre-rebuild | tail -n 1); NEW=$(sed -n 's/^HULLGATE_VOLUME=//p' .env | tail -n 1); test -n "$OLD" || { echo "F6: KO no HULLGATE_VOLUME in .env.pre-rebuild"; exit 1; }; echo "F6: old volume $OLD"; test "$OLD" != "$NEW" || { echo "F6: KO $OLD is the current volume"; exit 1; }; test -z "$(docker ps -q --filter volume=$OLD)" || { echo "F6: KO a container uses $OLD"; exit 1; }; docker volume rm "$OLD" && echo "F6: OK $OLD removed" )
```

**Expected result**: `F6: old volume <name>`, the name repeated by `docker volume rm`, then `F6: OK <name> removed`; `docker volume ls` no longer shows it.

**If the result differs**: a `F6: KO` line → nothing was deleted: read the reason, fix, paste again. `volume is in use` → a stopped container keeps it: `docker ps -a --filter volume=<name>`, record the diagnostic locally without deleting anything.

Left to you, unusable after the fresh start: in `backups/`, the files with a prefix other than `hullgate-` (rotation no longer reads them), the F-010 leftovers `backups/pre-hullgate-<timestamp>.db`, `backups/cutover-step10.log`, `.env.pre-hullgate` and the image tagged `:pre-hullgate`. The copy kept in F1 is restored only with the previous commit and the old key (`connections_key.pre-rebuild`): keep both together, or delete both.
