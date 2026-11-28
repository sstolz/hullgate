# Backup and restore

## Step mapping

Source roles refer to the private server runbooks. Identifiers are preserved; the merged
core keys, connections and relay namespaces do not collide.

| Source role | Runbook step | Guide step |
|---|---|---|
| backup and restore | S1 | S1 |
| backup and restore | S2 | S2 |
| backup and restore | S3 | S3 |
| backup and restore | S4 | S4 |

Replace the documented placeholders using the [parameter table](install.md#parameters).
Commands run as your SSH account on the target unless marked as workstation or browser actions.
Use the documented defaults below, or consistently substitute your own installation paths.


Goal: back up the database hot, schedule the backup, check that a backup restarts the gateway without touching production, and — as a last resort — restore in place.

- `make backup` copies the running database with `VACUUM INTO` (no downtime), checks the copy (`PRAGMA integrity_check` = `ok`) before storing it as `backups/hullgate-<timestamp>.db`, and keeps the 14 most recent (ordered by timestamp; a file with another prefix is neither rotated nor chosen by `restore-check`). Service stopped: the copy goes through a throwaway container of the same image. Database absent: message, code 0, no file.
- `make restore-check [FILE=…]` restores the backup (the most recent by default) into a throwaway volume, starts a read-only throwaway container on it, requires `/readyz` 200 and the same number of rows per table, then destroys the volume and the container. The service and the volume `hullgate_data` are not touched.
- `deploy`, `backup` and `restore-check` take the lock `backups/.lock`: a second command during the first exits with an error without doing anything.
- Backups stay on the server; their off-site copy is not covered by F-004.

Each step: a block to paste, the expected result, what to do if it differs.

---

## Step S1 — Manual backup

```sh
cd /opt/hullgate && make backup && ls -l backups/ | tail -n 5
```

**Expected result**: `BACKUP: OK backups/hullgate-<timestamp>.db integrity=ok tables={…}`, then the file in the list. Beyond 14 files, lines `BACKUP: rotation, removed <file>.db` name the oldest ones deleted (`hullgate-….db`, or a file under the former name for a backup from before the cutover).

**If the result differs**: `BACKUP: no database` → nothing has been deployed yet, or the volume has another name (`HULLGATE_VOLUME` in `.env`). `BACKUP: KO VACUUM INTO / integrity_check` → the running database may be damaged: deploy nothing, keep the existing backups and inspect the diagnostic locally. `BACKUP: KO copy to backups` → disk space (`df -h <compose-root>`) or permissions on `backups/`. `lock held` → another operation is running: paste again later. In every failure case, no existing backup was deleted.


## Step S2 — Schedule the daily backup (proposal)

```sh
( crontab -l 2>/dev/null | grep -v 'hullgate && make backup'; echo '15 3 * * * cd /opt/hullgate && make backup >> backups/backup.log 2>&1' ) | crontab - && crontab -l | grep 'hullgate && make backup'
```

**Expected result**: the scheduled line is displayed. The next day, `tail -n 3 /opt/hullgate/backups/backup.log` shows a `BACKUP: OK` line.

**If the result differs**: `crontab: command not found` → `sudo apt-get install -y cron`. The log shows `lock held` one night → a deployment was running at 3:15: the next night's backup catches up (and `make deploy` backs up anyway before replacing the container).


## Step S3 — Restore check (non-destructive)

With no argument, the most recent backup is checked; for another one, replace the line with `make restore-check FILE=backups/hullgate-<timestamp>.db`.

```sh
cd /opt/hullgate && make restore-check
```

**Expected result**: `RESTORE-CHECK: OK backups/hullgate-<timestamp>.db readyz=200 connections=<n> (key_absent) tables={…}`.

**If the result differs**: `RESTORE-CHECK: KO step restore (integrity_check)` → this backup is corrupted: check the previous one with `FILE=`; `KO step migration (…)` → the current code refuses a value of this backup (BR-132): retain the diagnostic locally and do not restore this file; `KO step readyz` → the current image does not start on this database: inspect `make logs`; `KO no backup` → play S1. The service was not touched, whatever the result.


## Step S4 — In-place restore (destructive: never played at acceptance)

Play only if the running database is lost or damaged, after a successful `make restore-check FILE=…` on the chosen backup. The service is stopped during the operation; the current database is first backed up (if possible), then **replaced**. Replace `<backup-file>` with the chosen backup (`backups/hullgate-<timestamp>.db`):

```sh
cd /opt/hullgate && F=<backup-file> && C="docker compose -f deploy/compose.prod.yaml --env-file .env" && V=$(sed -n 's/^HULLGATE_VOLUME=//p' .env) && I=$(sed -n 's/^HULLGATE_IMAGE=//p' .env) && if make restore-check FILE="$F"; then make backup; $C stop hullgate && docker run --rm -i --user 10001:10001 --network none -v "${V:-hullgate_data}:/data" --entrypoint sh "${I:-hullgate:current}" -c 'rm -f /data/hullgate.db /data/hullgate.db-wal /data/hullgate.db-shm && python -m hullgate.backup restore - /data/hullgate.db' < "$F" && $C start hullgate && sleep 10 && make verify; else echo "RESTORE NOT RUN: check failed"; fi
```

**Expected result**: `RESTORE-CHECK: OK …`, a `BACKUP: OK` (or `KO` if the current database is unreadable: that is the case that justifies the restore), the container stop, a `{…}` line (number of rows per restored table), the restart, then `VERIFY: 6/6 OK`.

**If the result differs**: the initial `restore-check` fails → nothing was touched: choose another backup. The restore fails after the stop (`BACKUP-STEP KO …`) → the database was erased but not replaced: paste the block again with another checked backup; the service stays stopped until a restore has succeeded. `make verify` in `KO` → `install.md`, step O7.
