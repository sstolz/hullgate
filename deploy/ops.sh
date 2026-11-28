#!/bin/sh
# Operations of hullgate (F-004): called by the Makefile, at the root of the clone.
#   deploy <ref> | backup | restore-check [file] | verify | verify-auth <path>
#   | verify-connections <client>/<name> | verify-relay <path> [<sensitive>] [<tool>] | status | logs
#
# Host prerequisites: sh, git, docker (compose v2, buildx), flock, curl, date, mktemp, sed, sort.
# deploy, backup, restore-check and verify take the exclusive lock backups/.lock (BR-038).
# The whole script is a function called on its last line: a `git checkout` during a
# deployment therefore does not change the code being executed.

set -u

ENV_FILE=${ENV_FILE:-.env}
COMPOSE_FILE=deploy/compose.prod.yaml
SERVICE=hullgate
BACKUP_DIR=${BACKUP_DIR:-backups}
BACKUP_KEEP=${BACKUP_KEEP:-14}
READY_TIMEOUT=${DEPLOY_READY_TIMEOUT:-60}
REF_FILE=.deployed-ref
DB=/data/hullgate.db
APP_UID=10001:10001

say() { printf '%s\n' "$*"; }

# Backups of the directory, newest first (BR-038 v2, F-011): only the
# `hullgate-<timestamp>.db` files are read, a file with another prefix is neither rotated nor
# chosen; under a single prefix, name order is timestamp order.
backups_newest_first() {
  ls "$1" 2>/dev/null | grep -E '^hullgate-[0-9]{8}T[0-9]{6}Z\.db$' | sort -r
}

# Rotation: only the BACKUP_KEEP most recent backups (by timestamp) remain.
backup_rotate() {
  backups_newest_first "$1" | tail -n "+$(($2 + 1))" | while read -r old; do
    rm -f "$1/$old" && say "BACKUP: rotation, removed $old"
  done
}

# Value of a key of the .env file (last occurrence, single or double quotes removed).
env_get() {
  v=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" 2>/dev/null | tail -n 1)
  v=$(printf '%s' "$v" | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")
  printf '%s' "${v:-$2}"
}

compose() {
  if [ -n "$(env_get ADMIN_HOST '')" ]; then
    docker compose -f "$COMPOSE_FILE" -f deploy/compose.admin.yaml --env-file "$ENV_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
  fi
}

require_env() {
  [ -f "$ENV_FILE" ] || { say "OPS: KO $ENV_FILE absent — copy deploy/env.example to .env and fill it in (runbook O5)"; exit 1; }
  admin_host=$(env_get ADMIN_HOST '' | tr '[:upper:]' '[:lower:]')
  if [ -n "$admin_host" ]; then
    [ "$(env_get ADMIN_URL '')" = "https://$admin_host" ] || {
      say 'OPS: KO ADMIN_URL must equal https://${ADMIN_HOST}'; exit 1;
    }
    [ -n "$(env_get TRUSTED_PROXIES '' | tr -d '[:space:],')" ] || {
      say 'OPS: KO TRUSTED_PROXIES required when ADMIN_HOST is set'; exit 1;
    }
  fi
  VOLUME=$(env_get HULLGATE_VOLUME hullgate_data)
  IMAGE=$(env_get HULLGATE_IMAGE hullgate:current)
}

try_lock() {
  mkdir -p "$BACKUP_DIR" || exit 1
  exec 9>"$BACKUP_DIR/.lock" || exit 1
  flock -n 9
}

take_lock() {
  try_lock || { say "OPS: KO lock held ($BACKUP_DIR/.lock) — another deploy, backup or restore-check operation is running; nothing was done"; exit 1; }
}

running_cid() { compose ps -q --status running "$SERVICE" 2>/dev/null | head -n 1; }

# /readyz probe of the public listener, from inside container $1 (exit 0 if 200).
probe_ready() {
  docker exec "$1" python -c "import sys, urllib.request as u
try:
    sys.exit(0 if u.urlopen('http://127.0.0.1:4790/readyz', timeout=2).status == 200 else 1)
except Exception:
    sys.exit(1)" >/dev/null 2>&1
}

health() {
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}healthy{{end}}' "$1" 2>/dev/null
}

STAGE=""
cleanup_stage() {
  if [ -n "$STAGE" ]; then docker rm -f "$STAGE" >/dev/null 2>&1; STAGE=""; fi
}

# ---------------------------------------------------------------------------------------------
# Backup (BR-038): VACUUM INTO in the volume, integrity_check, copy to backups/, rotation.
# ---------------------------------------------------------------------------------------------
backup_core() {
  if ! docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    say "BACKUP: no database (volume $VOLUME absent) — nothing to back up"
    return 0
  fi
  cid=$(running_cid)
  if [ -z "$cid" ]; then
    # Service stopped: throwaway container of the same image on the same volume, no writer.
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
      say "BACKUP: KO image $IMAGE absent, service stopped: backup impossible"
      return 1
    fi
    STAGE="hullgate-backup-$$"
    docker run -d --name "$STAGE" --read-only --user "$APP_UID" -v "$VOLUME:/data" \
      --network none --entrypoint sleep "$IMAGE" 600 >/dev/null \
      || { say "BACKUP: KO throwaway container"; cleanup_stage; return 1; }
    cid=$STAGE
    say "BACKUP: service stopped, copy through the throwaway container $STAGE"
  fi
  docker exec "$cid" python -m hullgate.backup exists "$DB"
  rc=$?
  if [ "$rc" -eq 3 ]; then
    say "BACKUP: no database ($DB absent) — nothing to back up"
    cleanup_stage
    return 0
  elif [ "$rc" -ne 0 ]; then
    say "BACKUP: KO reading the database (code $rc)"
    cleanup_stage
    return 1
  fi
  name="hullgate-$(date -u +%Y%m%dT%H%M%SZ).db"
  work="/data/.backup/$name"
  if ! counts=$(docker exec "$cid" python -m hullgate.backup snapshot "$DB" "$work"); then
    say "BACKUP: KO VACUUM INTO / integrity_check — no existing backup touched"
    cleanup_stage
    return 1
  fi
  partial="$BACKUP_DIR/$name.partial"
  # Streamed copy (not `docker cp`): `docker cp` remounts the container's mounts, and the
  # read-only remount of mounted secrets (F-005) is refused by some engines (Sysbox).
  if ! docker exec "$cid" cat "$work" > "$partial" 2>/dev/null || [ ! -s "$partial" ]; then
    rm -f "$partial"
    docker exec "$cid" python -m hullgate.backup discard "$work" >/dev/null 2>&1
    say "BACKUP: KO copy to $BACKUP_DIR — no existing backup touched"
    cleanup_stage
    return 1
  fi
  docker exec "$cid" python -m hullgate.backup discard "$work" >/dev/null 2>&1
  cleanup_stage
  mv "$partial" "$BACKUP_DIR/$name" || { rm -f "$partial"; say "BACKUP: KO storing"; return 1; }
  backup_rotate "$BACKUP_DIR" "$BACKUP_KEEP"
  say "BACKUP: OK $BACKUP_DIR/$name integrity=ok tables=$counts"
  return 0
}

# ---------------------------------------------------------------------------------------------
# Restore check (BR-039): throwaway volume and containers, service and database untouched.
# ---------------------------------------------------------------------------------------------
RC_VOL=""
RC_CTR=""
cleanup_rc() {
  [ -n "$RC_CTR" ] && docker rm -f "$RC_CTR" >/dev/null 2>&1
  [ -n "$RC_VOL" ] && docker volume rm -f "$RC_VOL" >/dev/null 2>&1
  RC_CTR=""; RC_VOL=""
}

restore_check_core() {
  file=$1
  if [ -z "$file" ]; then
    file=$(backups_newest_first "$BACKUP_DIR" | head -n 1)
    [ -n "$file" ] || { say "RESTORE-CHECK: KO no backup in $BACKUP_DIR"; return 1; }
    file="$BACKUP_DIR/$file"
  fi
  [ -f "$file" ] || { say "RESTORE-CHECK: KO file absent: $file"; return 1; }
  docker image inspect "$IMAGE" >/dev/null 2>&1 || { say "RESTORE-CHECK: KO image $IMAGE absent"; return 1; }
  RC_VOL="hullgate-restorecheck-$$"
  RC_CTR="hullgate-restorecheck-$$"
  docker volume create "$RC_VOL" >/dev/null || { say "RESTORE-CHECK: KO step volume"; cleanup_rc; return 1; }
  # Restore step: the backup comes in on standard input, checked before it is put in place.
  docker run -d --name "$RC_CTR" --read-only --user "$APP_UID" -v "$RC_VOL:/data" \
    --network none --entrypoint sleep "$IMAGE" 600 >/dev/null \
    || { say "RESTORE-CHECK: KO step restore-container"; cleanup_rc; return 1; }
  err=$(mktemp) || { say "RESTORE-CHECK: KO step restore (mktemp)"; cleanup_rc; return 1; }
  if ! expected=$(docker exec -i "$RC_CTR" python -m hullgate.backup restore - "$DB" < "$file" 2>"$err"); then
    # `backup restore` names its failed step on stderr (`BACKUP-STEP KO <step> : …`): a
    # migration abort (BR-132, schema_error) is not an integrity failure (review A of F-010, nit-1).
    step=$(sed -n 's/^BACKUP-STEP KO \([a-z_]*\) : .*/\1/p' "$err" | head -n 1)
    if [ "$step" = migrate ]; then
      say "RESTORE-CHECK: KO step migration ($(sed -n 's/^BACKUP-STEP KO migrate : //p' "$err" | head -n 1)): $file"
    else
      say "RESTORE-CHECK: KO step restore (integrity_check): $file"
    fi
    rm -f "$err"
    cleanup_rc
    return 1
  fi
  rm -f "$err"
  # Throwaway signing key, generated in the throwaway volume (BR-033 v2: without a key, /readyz 503);
  # it disappears with it. It is not the production key, never read here.
  docker exec "$RC_CTR" python -m hullgate.keys signing-key /data/.restore-check-signing-key \
    >/dev/null || { say "RESTORE-CHECK: KO step throwaway-key"; cleanup_rc; return 1; }
  docker rm -f "$RC_CTR" >/dev/null 2>&1
  # Startup step: the gateway starts read-only on the copy and answers /readyz 200.
  docker run -d --name "$RC_CTR" --read-only --user "$APP_UID" -v "$RC_VOL:/data" --network none \
    -e PUBLIC_URL=http://restore-check.invalid -e ADMIN_URL=http://localhost:4791 \
    -e SIGNING_KEY_FILE=/data/.restore-check-signing-key \
    "$IMAGE" >/dev/null || { say "RESTORE-CHECK: KO step startup"; cleanup_rc; return 1; }
  i=0
  until probe_ready "$RC_CTR"; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then say "RESTORE-CHECK: KO step readyz (≠ 200 after 30 s)"; cleanup_rc; return 1; fi
    sleep 1
  done
  # Comparison step: same row count per table as the migrated backup (BR-039, BR-132:
  # `backup restore` applies the code's migrations and counts after them; the gateway starts
  # on an already migrated copy and writes no second marker).
  got=$(docker exec "$RC_CTR" python -m hullgate.backup counts "$DB") \
    || { say "RESTORE-CHECK: KO step count"; cleanup_rc; return 1; }
  if [ "$got" != "$expected" ]; then
    say "RESTORE-CHECK: KO step comparison (backup $expected, restored $got)"
    cleanup_rc
    return 1
  fi
  # Connections step (F-006, BR-039 clarified): the copy starts without a connections key (no
  # throwaway key: it would create the `key_different` case); its connections are seen `key_absent`.
  conns=$(docker exec "$RC_CTR" python -m hullgate.backup connections "$DB") \
    || { say "RESTORE-CHECK: KO step connections"; cleanup_rc; return 1; }
  nconn=$(printf '%s' "$conns" | sed -n 's/.*"connections": \([0-9][0-9]*\).*/\1/p')
  if [ -z "$nconn" ] || { [ "$nconn" -gt 0 ] && ! printf '%s' "$conns" | grep -q '"key": "key_absent"'; }; then
    say "RESTORE-CHECK: KO step connections (expected: connections seen key_absent; read $conns)"
    cleanup_rc
    return 1
  fi
  cleanup_rc
  say "RESTORE-CHECK: OK $file readyz=200 connections=$nconn (key_absent) tables=$got"
  return 0
}

# ---------------------------------------------------------------------------------------------
# Deployment (BR-040)
# ---------------------------------------------------------------------------------------------
deploy_fail() {
  say "DEPLOY: KO $1 — $2"
  if [ -n "$3" ]; then
    say "Rollback to the previous ref: make deploy REF=$3"
  else
    say "Rollback: no previous ref (first deployment)"
  fi
  exit 1
}

deploy() {
  ref=$1
  require_env
  take_lock
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    say "DEPLOY: KO working tree modified in the clone — nothing was done (git status)"
    exit 1
  fi
  prev=$(sed -n 1p "$REF_FILE" 2>/dev/null)
  say "DEPLOY: REF=$ref, deployed ref: ${prev:-none}"
  backup_core || deploy_fail "$ref" "prior backup failed, stopped before the build (service unchanged)" "$prev"
  git fetch --quiet --tags origin || deploy_fail "$ref" "git fetch failed" "$prev"
  sha=""
  for cand in "origin/$ref" "$ref"; do
    sha=$(git rev-parse --verify --quiet "$cand^{commit}") && break
    sha=""
  done
  [ -n "$sha" ] || deploy_fail "$ref" "ref not found (tag, remote branch or SHA)" "$prev"
  git checkout --quiet --detach "$sha" || deploy_fail "$sha" "git checkout failed" "$prev"
  say "DEPLOY: building $IMAGE at $sha"
  compose build || deploy_fail "$sha" "image build failed" "$prev"
  t0=$(date +%s)
  compose up -d --remove-orphans || deploy_fail "$sha" "docker compose up failed" "$prev"
  i=0
  while :; do
    cid=$(running_cid)
    # /readyz 200 seen by the container's healthcheck: Traefik routes a container that has a
    # healthcheck only once it is "healthy"; DEPLOY: OK therefore means "routable".
    if [ -n "$cid" ] && [ "$(health "$cid")" = healthy ] && probe_ready "$cid"; then break; fi
    i=$((i + 1))
    if [ "$i" -ge "$READY_TIMEOUT" ]; then
      compose logs --tail 20 "$SERVICE" 2>&1 | sed 's/^/  | /'
      deploy_fail "$sha" "/readyz ≠ 200 after ${READY_TIMEOUT} s" "$prev"
    fi
    sleep 1
  done
  t1=$(date +%s)
  printf '%s\n%s\n' "$sha" "$prev" > "$REF_FILE.tmp" && mv "$REF_FILE.tmp" "$REF_FILE"
  say "DOWNTIME $((t1 - t0))"
  say "DEPLOY: OK $sha"
}

# ---------------------------------------------------------------------------------------------
# Acceptance checks readable by the operator (AC-OP)
# ---------------------------------------------------------------------------------------------
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" 2>/dev/null || true; }

# Match the effective origin in compose.prod.yaml and its optional admin overlay.
admin_probe_host() {
  if [ -n "$(env_get ADMIN_HOST '')" ]; then
    origin=$(env_get ADMIN_URL '')
    printf '%s' "${origin#https://}"
  else
    printf 'localhost:%s' "$(env_get ADMIN_LOCAL_PORT 14791)"
  fi
}

verify() {
  require_env
  host=$(env_get MCP_HOST "")
  port=$(env_get TRAEFIK_HTTPS_PORT 443)
  admin=$(env_get ADMIN_LOCAL_PORT 14791)
  insecure=""
  [ "${VERIFY_INSECURE:-0}" = 1 ] && insecure="-k"
  n=0
  if ! try_lock; then
    say "VERIFY: 0/6 — lock held ($BACKUP_DIR/.lock): another deploy, backup or restore-check operation is running; nothing was verified, run make verify again when it is over"
    return 1
  fi
  line() { say "VERIFY $1 $2 $3"; [ "$2" = OK ] && n=$((n + 1)); }

  ref=$(sed -n 1p "$REF_FILE" 2>/dev/null)
  if [ -n "$ref" ]; then line deployed-ref OK "$ref"; else line deployed-ref KO "absent"; fi

  # the production server's Traefik reached locally, name and SNI = MCP_HOST, without DNS or NAT hairpin
  # (same as curl --resolve $host:443:127.0.0.1 when TRAEFIK_HTTPS_PORT is 443).
  public_ok=0
  code=$(http_code $insecure --connect-to "$host:443:127.0.0.1:$port" "https://$host/readyz")
  if [ "$code" = 200 ]; then public_ok=1; line readyz-public OK "$host $code"; else line readyz-public KO "$host ${code:-000}"; fi

  code=$(http_code "http://127.0.0.1:$admin/healthz")
  if [ "$code" = 200 ]; then line admin-local OK "127.0.0.1:$admin $code"; else line admin-local KO "127.0.0.1:$admin ${code:-000}"; fi

  # Require a live public router before judging isolation or the optional console route.
  admin_host=$(env_get ADMIN_HOST '')
  code=$(http_code $insecure --connect-to "$host:443:127.0.0.1:$port" "https://$host/admin")
  if [ -n "$admin_host" ]; then
    expected=404
    [ "$admin_host" = "$host" ] && expected=200
    shell_code=$(http_code $insecure --connect-to "$admin_host:443:127.0.0.1:$port" "https://$admin_host/admin")
    api_code=$(http_code $insecure --connect-to "$admin_host:443:127.0.0.1:$port" "https://$admin_host/admin/api/v1/session")
    detail="$host/admin $code (expected $expected), $admin_host shell $shell_code API $api_code"
    if [ "$public_ok" = 1 ] && [ "$code" = "$expected" ] && [ "$shell_code" = 200 ] && [ "$api_code" = 401 ]; then
      line admin-public OK "$detail"
    else
      line admin-public KO "$detail (readyz-public must be OK)"
    fi
  elif [ "$code" = 404 ] && [ "$public_ok" = 1 ]; then
    line admin-not-public OK "$host/admin $code"
  else
    line admin-not-public KO "$host/admin ${code:-000} (readyz-public must be OK)"
  fi

  if out=$(backup_core) && last=$(printf '%s\n' "$out" | grep '^BACKUP: OK') ; then
    line backup OK "$(printf '%s' "$last" | sed 's/^BACKUP: OK //; s/ tables=.*//')"
  else
    printf '%s\n' "$out" | sed 's/^/  | /'
    line backup KO "see the output above"
  fi
  if out=$(restore_check_core ""); then
    line restore-check OK "0"
  else
    printf '%s\n' "$out" | sed 's/^/  | /'
    line restore-check KO "1"
  fi
  say "VERIFY: $n/6 OK"
  [ "$n" -eq 6 ]
}

# ---------------------------------------------------------------------------------------------
# Foundation checks (F-005, AC-OP): four lines, one banner on every exit path.
# ---------------------------------------------------------------------------------------------
verify_auth() {
  role=$1
  m=0
  sline() { say "VERIFY-AUTH $1 $2 $3"; [ "$2" = OK ] && m=$((m + 1)); return 0; }
  if [ ! -f "$ENV_FILE" ]; then
    for c in key-loaded admin-routes discovery challenge; do sline "$c" KO "$ENV_FILE absent"; done
    say "VERIFY-AUTH: 0/4 OK"
    return 1
  fi
  host=$(env_get MCP_HOST "")
  port=$(env_get TRAEFIK_HTTPS_PORT 443)
  admin=$(env_get ADMIN_LOCAL_PORT 14791)
  insecure=""
  [ "${VERIFY_INSECURE:-0}" = 1 ] && insecure="-k"
  to="$host:443:127.0.0.1:$port"
  case "$role" in
    */*/*/*) ;;
    *) role="" ;;
  esac

  code=$(http_code $insecure --connect-to "$to" "https://$host/readyz")
  if [ "$code" = 200 ]; then sline key-loaded OK "$host/readyz $code"; else
    sline key-loaded KO "$host/readyz ${code:-000} (key loaded? runbook step S3)"; fi

  # F-016 (BR-068 v2): the admin API answers 401 without a session, under the console's Host.
  code=$(http_code -H "Host: $(admin_probe_host)" "http://127.0.0.1:$admin/admin/api/v1/session")
  if [ "$code" = 401 ]; then sline admin-routes OK "127.0.0.1:$admin/admin/api/v1/session $code"; else
    sline admin-routes KO "127.0.0.1:$admin/admin/api/v1/session ${code:-000}"; fi

  # F-014 (§3.3): ROLE stays a tree path; the organisation's identifier has one source,
  # `tree show / --json`, key `public_id`.
  org=""
  if [ -n "$role" ]; then
    org=$(compose exec -T "$SERVICE" hullgate admin tree show / --json 2>/dev/null \
      | sed -n 's/^ *"public_id": "\([a-z2-7]\{12\}\)".*/\1/p' | head -n 1)
  fi
  if [ -z "$role" ]; then
    sline discovery KO "ROLE absent or malformed (client/project/env/role, read back from tree show)"
    sline challenge KO "ROLE absent or malformed"
  elif [ -z "$org" ]; then
    sline discovery KO "organisation identifier unreadable (tree show / --json, key public_id)"
    sline challenge KO "organisation identifier unreadable"
  else
    url="https://$host/o/$org/p/$role/mcp"
    md="https://$host/.well-known/oauth-protected-resource/o/$org/p/$role/mcp"
    body=$(curl -s $insecure --max-time 10 --connect-to "$to" -w '\n%{http_code}' "$md" 2>/dev/null || true)
    code=$(printf '%s' "$body" | tail -n 1)
    got=$(printf '%s' "$body" | sed -n 's/.*"resource":"\([^"]*\)".*/\1/p' | head -n 1)
    if [ "$code" = 200 ] && [ "$got" = "$url" ]; then sline discovery OK "$got"; else
      sline discovery KO "${code:-000} resource=${got:-absent}"; fi
    hdr=$(curl -s $insecure --max-time 10 --connect-to "$to" -o /dev/null -D - -X POST \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      --data '{"jsonrpc":"2.0","id":1,"method":"ping"}' "$url" 2>/dev/null | tr -d '\r' || true)
    code=$(printf '%s\n' "$hdr" | sed -n 's/^HTTP\/[0-9.]* \([0-9]*\).*/\1/p' | tail -n 1)
    if [ "$code" = 401 ] && printf '%s\n' "$hdr" | grep -qi "^www-authenticate:.*resource_metadata=\"$md\""; then
      sline challenge OK "401 resource_metadata"
    else
      sline challenge KO "${code:-000} $(printf '%s\n' "$hdr" | grep -i '^www-authenticate' | head -n 1)"
    fi
  fi
  say "VERIFY-AUTH: $m/4 OK"
  [ "$m" -eq 4 ]
}

# ---------------------------------------------------------------------------------------------
# Connection checks (F-006, AC-OP): the instrument runs in the container and prints its
# four lines and its banner; this script guarantees the banner on every other path.
# ---------------------------------------------------------------------------------------------
verify_connections() {
  name=$1
  vfail() {
    for c in state token renewal output; do say "VERIFY-CONNECTIONS $c: KO $1"; done
    say "VERIFY-CONNECTIONS: 0/4 OK"
    return 1
  }
  [ -f "$ENV_FILE" ] || { vfail "$ENV_FILE absent"; return 1; }
  case "$name" in
    */*/*|/*|*/) vfail "CONNECTION malformed (<client>/<name>, read back from connection list)"; return 1 ;;
    ?*/?*) ;;
    *) vfail "CONNECTION absent or malformed (read back from connection list)"; return 1 ;;
  esac
  out=$(compose exec -T "$SERVICE" python -m hullgate.verify_connections "$name" 2>&1)
  rc=$?
  if printf '%s\n' "$out" | grep -q '^VERIFY-CONNECTIONS: [0-9]/4 OK$'; then
    printf '%s\n' "$out"
    return "$rc"
  fi
  printf '%s\n' "$out" | tail -n 5 | sed 's/^/  | /'
  vfail "instrument not run (container stopped? make status)"
}

verify_relay() {
  role=$1
  sens=${2:-}
  tool=${3:-}
  rfail() {
    for c in list call refusal sensitive; do say "VERIFY-RELAY $c: KO $1"; done
    say "VERIFY-RELAY output: OK"
    say "VERIFY-RELAY: 1/5 OK"
    return 1
  }
  [ -f "$ENV_FILE" ] || { rfail "$ENV_FILE absent"; return 1; }
  case "$role" in
    */*/*/*/*|/*|*/) rfail "ROLE malformed (client/project/env/role, read back from tree show)"; return 1 ;;
    ?*/?*/?*/?*) ;;
    *) rfail "ROLE absent or malformed (read back from tree show)"; return 1 ;;
  esac
  # operator tokens read on standard input (BR-086), passed as they are to the container
  out=$(compose exec -T "$SERVICE" python -m hullgate.verify_relay "$role" "${sens:--}" "${tool:--}" 2>&1)
  rc=$?
  if printf '%s\n' "$out" | grep -q '^VERIFY-RELAY: [0-9]/5 OK$'; then
    printf '%s\n' "$out"
    return "$rc"
  fi
  printf '%s\n' "$out" | tail -n 5 | sed 's/^/  | /'
  rfail "instrument not run (container stopped? make status)"
}

# ---------------------------------------------------------------------------------------------
# Console checks (F-016, AC-OP-01): four read-only lines, a banner on every exit path.
# The console's Host follows the effective compose origin (ADMIN_URL, BR-206).
# ---------------------------------------------------------------------------------------------
verify_admin() {
  k=0
  aline() { say "VERIFY-ADMIN $1 $2 $3"; [ "$2" = OK ] && k=$((k + 1)); return 0; }
  admin=$(env_get ADMIN_LOCAL_PORT 14791)
  base="http://127.0.0.1:$admin"
  host=$(admin_probe_host)
  code=$(http_code "$base/healthz")
  if [ "$code" = 200 ]; then aline healthz OK "$base/healthz $code"; else
    aline healthz KO "$base/healthz ${code:-000}"; fi
  code=$(http_code -H "Host: neighbour.invalid" "$base/admin")
  if [ "$code" = 421 ]; then aline foreign-host OK "Host neighbour.invalid $code"; else
    aline foreign-host KO "Host neighbour.invalid ${code:-000} (BR-206)"; fi
  code=$(http_code -H "Host: $host" "$base/admin/api/v1/session")
  if [ "$code" = 401 ]; then aline api-session OK "/admin/api/v1/session $code"; else
    aline api-session KO "/admin/api/v1/session ${code:-000} (BR-068 v2)"; fi
  csp=$(curl -s -o /dev/null -D - --max-time 10 -H "Host: $host" "$base/admin" 2>/dev/null \
    | tr -d '\r' | sed -n 's/^[Cc]ontent-[Ss]ecurity-[Pp]olicy: //p' | head -n 1)
  case "$csp" in
    "default-src 'self';"*"frame-ancestors 'none'") aline shell-csp OK "$csp" ;;
    *) aline shell-csp KO "${csp:-absent} (BR-212)" ;;
  esac
  say "VERIFY-ADMIN: $k/4 OK"
  [ "$k" -eq 4 ]
}

main() {
  cmd=${1:-}
  case "$cmd" in
    deploy) deploy "${2:-}" ;;
    backup) require_env; take_lock; backup_core ;;
    restore-check) require_env; take_lock; restore_check_core "${2:-}" ;;
    verify) verify ;;
    verify-auth) verify_auth "${2:-}" ;;
    verify-connections) verify_connections "${2:-}" ;;
    verify-relay) verify_relay "${2:-}" "${3:-}" "${4:-}" ;;
    verify-admin) verify_admin ;;
    status) require_env; compose ps; say "deployed ref: $(sed -n 1p "$REF_FILE" 2>/dev/null || true)"; say "previous: $(sed -n 2p "$REF_FILE" 2>/dev/null || true)" ;;
    logs) require_env; compose logs --tail 200 "$SERVICE" ;;
    *) say "usage: deploy/ops.sh deploy <ref> | backup | restore-check [file] | verify | verify-auth <path> | verify-connections <client>/<name> | verify-relay <path> [<sensitive>] [<tool>] | verify-admin | status | logs"; exit 2 ;;
  esac
}

# OPS_LIB=1: functions loaded without running anything (tests of the backup order, AC-27, and of
# the verification banners, AC-23).
[ "${OPS_LIB:-}" = 1 ] || { main "$@"; exit $?; }
