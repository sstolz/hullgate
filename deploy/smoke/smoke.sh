#!/bin/sh
# make prod-smoke (session only, never on the production server): plays the production composition
# `deploy/compose.prod.yaml` unchanged behind a local Traefik, in the session's dockerd.
#
#   1. throwaway clone of the repository (.smoke/clone) with the test .env deploy/smoke/env.test;
#   2. `make deploy REF=<HEAD>` in the clone                     -> DEPLOY: OK <sha>       (BR-040)
#   3. requests through Traefik and reading of its API           -> SMOKE traefik-route    (BR-041)
#   4. /admin, services and ports                                 -> SMOKE admin-not-routed (BR-034)
#   5. two clients, forged XFF, audit and credential budgets    -> SMOKE client-address (BR-315)
#   6. throwaway commit whose /readyz stays 503, `make deploy`    -> SMOKE deploy-ko-readable (BR-040)
#   7. not counted: back to HEAD, `make verify` (instrument of F-004's AC-OP) and, on a test
#      tree created by the CLI, `make verify-auth ROLE=…` (F-005's AC-OP), local certificate accepted.
#   8. raw edge forms: normalisation readings, 404/no Location -> SMOKE edge-404 (BR-161 v2)
#
# Mounted secrets (F-005): throwaway key and TOTP secret, generated in the clone (`secrets/`), readable
# by the image's UID 10001 (0444, session values destroyed with the clone).
#
# Prerequisite: 127.0.0.1 of the machine running the command reaches the ports published by the dockerd
# (in the session: toolbox started with --network host). Only the HEAD commit is played.

set -u

ROOT=$(pwd)
WORK="$ROOT/.smoke"
CLONE="$WORK/clone"
ENVF="$ROOT/deploy/smoke/env.test"
KO_TIMEOUT=${SMOKE_KO_TIMEOUT:-20}
ADMIN_HOST=${SMOKE_ADMIN_HOST:-}
# A local test name only: SMOKE_ADMIN_HOST=admin.example.test (or MCP_HOST for shared-name coverage).

say() { printf '%s\n' "$*"; }
envv() { sed -n "s/^$1=//p" "$ENVF" | tail -n 1; }

NET=$(envv TRAEFIK_NETWORK)
HOST=$(envv MCP_HOST)
EP=$(envv TRAEFIK_ENTRYPOINT)
RES=$(envv TRAEFIK_CERTRESOLVER)
ADMIN=$(envv ADMIN_LOCAL_PORT)
IMAGE=$(envv HULLGATE_IMAGE)
# The smoke's image is the env file's: a caller's HULLGATE_IMAGE (make test-container sets one) would win
# over --env-file in compose and build under another tag than the one probed.
unset HULLGATE_IMAGE TRUSTED_PROXIES TRAEFIK_ADDRESS
TRAEFIK="docker compose -p hullgate-smoke-traefik -f $ROOT/deploy/smoke/compose.traefik.yaml --env-file $ENVF"
PROD="docker compose -f deploy/compose.prod.yaml --env-file .env"
if [ -n "$ADMIN_HOST" ]; then
  PROD="$PROD -f deploy/compose.admin.yaml"
fi

ok=0
result() {
  if [ "$2" = OK ]; then ok=$((ok + 1)); say "SMOKE $1 OK"; else say "SMOKE $1 KO $3"; fi
}

cleanup() {
  if [ -d "$CLONE" ]; then (cd "$CLONE" && $PROD down -v --remove-orphans >/dev/null 2>&1); fi
  $TRAEFIK down -v --remove-orphans >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker image rm "$IMAGE" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  say "SMOKE: warning, working tree modified — only the HEAD commit is played"
fi
SHA=$(git rev-parse HEAD)
say "== prod-smoke at $SHA"
rm -rf "$WORK" && mkdir -p "$WORK" || exit 1
git clone --quiet --no-hardlinks "$ROOT" "$CLONE" || exit 1
cp "$ENVF" "$CLONE/.env"
if [ -n "$ADMIN_HOST" ]; then
  printf '\nADMIN_HOST=%s\nADMIN_URL=https://%s\n' "$ADMIN_HOST" "$ADMIN_HOST" >> "$CLONE/.env"
fi
mkdir -p "$CLONE/secrets" || exit 1
for kind in signing-key totp-secret connections-key; do
  f=signing_key; [ "$kind" = totp-secret ] && f=authorize_totp
  [ "$kind" = connections-key ] && f=connections_key
  uv run --locked --no-sync python -m hullgate.keys "$kind" "$CLONE/secrets/$f" >/dev/null || exit 1
  chmod 0444 "$CLONE/secrets/$f"
done
docker network create --subnet "$(envv TRAEFIK_SUBNET)" "$NET" >/dev/null || exit 1
$TRAEFIK up -d --quiet-pull 2>&1 | tail -n 1

# 2. Nominal deployment ----------------------------------------------------------------------
say "== make deploy REF=$SHA (throwaway clone)"
(cd "$CLONE" && make --no-print-directory deploy REF="$SHA") > "$WORK/deploy-ok.log" 2>&1
rc=$?
grep -E '^(DEPLOY|DOWNTIME|BACKUP)' "$WORK/deploy-ok.log"
if [ "$rc" -eq 0 ] && grep -qx "DEPLOY: OK $SHA" "$WORK/deploy-ok.log" \
   && [ "$(sed -n 1p "$CLONE/.deployed-ref")" = "$SHA" ]; then
  ok=$((ok + 1))
else
  tail -n 30 "$WORK/deploy-ok.log"
  say "SMOKE deploy-ok KO code $rc"
fi

# 3 and 4. Routing through Traefik ----------------------------------------------------------
probe_traefik() {
  docker run --rm -i --network "$NET" --entrypoint python "$IMAGE" - traefik "$HOST" "$EP" "$RES" "$ADMIN_HOST" \
    < "$ROOT/deploy/smoke/probe.py" > "$WORK/probe.log" 2>&1
}
i=0
until probe_traefik; do
  i=$((i + 1)); [ "$i" -ge 20 ] && break; sleep 1
done
(cd "$CLONE" && $PROD config --format json) | python3 "$ROOT/deploy/smoke/probe.py" config "$ADMIN" "$ADMIN_HOST" \
  >> "$WORK/probe.log" 2>&1
sed 's/^/  /' "$WORK/probe.log"
all_ok() {
  for c in "$@"; do grep -q "^CHECK $c OK" "$WORK/probe.log" || return 1; done
}
if all_ok router-declared router-entrypoint router-certresolver route-healthz no-public-port; then
  result traefik-route OK
else
  result traefik-route KO "see the CHECK lines"
fi
if [ -n "$ADMIN_HOST" ]; then
  if all_ok admin-routed admin-route-config admin-loopback-only \
     && { [ "$ADMIN_HOST" = "$HOST" ] || all_ok public-admin-404; }; then
    result admin-routed OK
  else
    result admin-routed KO "see the CHECK lines"
  fi
elif all_ok route-admin-404 services-public-only no-admin-backend admin-loopback-only no-admin-label; then
  result admin-not-routed OK
else
  result admin-not-routed KO "see the CHECK lines"
fi

# 5. Trusted client address and independent budgets through Traefik ---------------------------
CID=$(cd "$CLONE" && $PROD ps -q hullgate)
for args in "/ client Address" "address project Smoke" "address/smoke environment Test"; do
  # shellcheck disable=SC2086
  docker exec "$CID" hullgate admin tree add $args >/dev/null || exit 1
done
ADDRESS_PATH=$(docker exec "$CID" hullgate admin tree show --json \
  | sed -n 's/.*"url": "[^" ]*\(\/o\/[a-z2-7]\{12\}\/p\/address\/smoke\/test\/[^" ]*\/mcp\)".*/\1/p' | head -n 1)
[ -n "$ADDRESS_PATH" ] || { say "SMOKE client-address KO no test role"; exit 1; }
ADDRESS_ROLE=${ADDRESS_PATH#*/p/}
ADDRESS_ROLE=${ADDRESS_ROLE%/mcp}
address_ok=1
# Explicit IPs prevent a removed probe's address being reused by the next one.
for label in A B; do
  address=$(envv "PROBE_ADDRESS_$label")
  docker run --rm -i --network "$NET" --ip "$address" --entrypoint python "$IMAGE" - \
    client-address-send "$HOST" "$ADDRESS_PATH" "$address" \
    < "$ROOT/deploy/smoke/probe.py" > "$WORK/client-$label.json" || address_ok=0
done
docker exec "$CID" hullgate audit tail -n 200 --json > "$WORK/address-audit.json" || address_ok=0
if [ "$address_ok" -eq 1 ] && python3 "$ROOT/deploy/smoke/probe.py" client-address \
  "$ADDRESS_ROLE" "$(envv TRUSTED_PROXIES)" "$WORK/client-A.json" "$WORK/client-B.json" \
  "$WORK/address-audit.json"; then
  result client-address OK
else
  result client-address KO "see the CHECK line or probe failure"
fi

# 6. Readable failed deployment ----------------------------------------------------------------
# Throwaway commit in the clone: an image whose /readyz stays 503 (the probe never finds the database).
db_py="$CLONE/src/hullgate/db.py"
sed 's/            if conn is None:/            if True:/' "$db_py" > "$WORK/db.py" && cat "$WORK/db.py" > "$db_py"
git -C "$CLONE" -c user.name=smoke -c user.email=smoke@example.invalid \
  commit --quiet -am "smoke: /readyz stays 503" || exit 1
KO_SHA=$(git -C "$CLONE" rev-parse HEAD)
say "== make deploy REF=$KO_SHA (image whose /readyz stays 503, waiting ${KO_TIMEOUT} s)"
(cd "$CLONE" && DEPLOY_READY_TIMEOUT=$KO_TIMEOUT make --no-print-directory deploy REF="$KO_SHA") \
  > "$WORK/deploy-ko.log" 2>&1
rc=$?
grep -E '^(DEPLOY|BACKUP|Rollback)' "$WORK/deploy-ko.log"
if [ "$rc" -ne 0 ] \
   && grep -qx "DEPLOY: KO $KO_SHA — /readyz ≠ 200 after $KO_TIMEOUT s" "$WORK/deploy-ko.log" \
   && grep -qx "Rollback to the previous ref: make deploy REF=$SHA" "$WORK/deploy-ko.log" \
   && grep -q '^BACKUP: OK' "$WORK/deploy-ko.log" \
   && [ "$(sed -n 1p "$CLONE/.deployed-ref")" = "$SHA" ]; then
  result deploy-ko-readable OK
else
  tail -n 30 "$WORK/deploy-ko.log"
  result deploy-ko-readable KO "code $rc, .deployed-ref $(sed -n 1p "$CLONE/.deployed-ref")"
fi

# 7. Not counted: rollback and the AC-OP instrument ---------------------------------------------
say "== not counted: make deploy REF=$SHA (rollback), then make verify"
(cd "$CLONE" && make --no-print-directory deploy REF="$SHA") > "$WORK/rollback.log" 2>&1
grep -E '^(DEPLOY|DOWNTIME)' "$WORK/rollback.log"
(cd "$CLONE" && VERIFY_INSECURE=1 make --no-print-directory verify) 2>&1 | grep -E '^(VERIFY|  \|)'
CID=$(cd "$CLONE" && $PROD ps -q hullgate)
for args in "/ client Smoke" "smoke project Socle" "smoke/socle environment Dev"; do
  # shellcheck disable=SC2086
  docker exec "$CID" hullgate admin tree add $args >/dev/null
done
ROLE=$(docker exec "$CID" hullgate admin tree show --json \
  | sed -n 's/.*"url": "[^"]*\/o\/[a-z2-7]\{12\}\/p\/\([^"]*\)\/mcp".*/\1/p' | head -n 1)
(cd "$CLONE" && VERIFY_INSECURE=1 make --no-print-directory verify-auth ROLE="$ROLE") 2>&1 \
  | grep -E '^VERIFY-AUTH'

# 8. BR-161 v2: readings and end-to-end refusals through Traefik -------------------------------
# Read the stored identifier and an existing role; the probe builds all four route families.
say "== edge readings through Traefik (BR-161 v2)"
if docker exec "$CID" hullgate admin tree show / --json > "$WORK/edge-tree.json" \
   && docker run --rm -i --network "$NET" --entrypoint python "$IMAGE" - \
     edge-readings "$HOST" "$(cat "$WORK/edge-tree.json")" < "$ROOT/deploy/smoke/probe.py"; then
  result edge-404 OK
else
  result edge-404 KO "see the CHECK lines or probe failure"
fi

say "SMOKE: $ok/6 OK"
[ "$ok" -eq 6 ]
