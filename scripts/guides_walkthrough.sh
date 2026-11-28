#!/usr/bin/env bash
# AC-25: play guide blocks on a disposable target in a LOCAL dockerd. Never use a real host.
# Orchestrator: commit the candidate first, then (from the repo root):
#   flock /tmp/claude-1000/hullgate-docker.lock bash scripts/guides_walkthrough.sh
# Requires Docker 25+, Compose v2/buildx, sudo, make, git, curl, python3, openssl,
# coreutils, ss and flock. Ports 443 and 14791 must be free. No SSH or provider requests.
# Logs survive under the printed temp directory; generated keys and Docker resources do not.
# Defaults exercised: image hullgate:current, volume hullgate_data, ports 443/14791,
# key basenames/mode/UID. Adaptations: temp install root, local clone URL, .test host,
# local Traefik self-signed TLS (VERIFY_INSECURE=1), no real DNS/ACME/browser/provider.
set -Eeuo pipefail
umask 077
ROOT=$(git rev-parse --show-toplevel)
REF=$(git rev-parse HEAD)
export ROOT REF
[[ -z $(git status --porcelain --untracked-files=no) ]] || {
  echo 'WALKTHROUGH: FAIL commit the candidate before running'; exit 1;
}
# Refuse remote Docker endpoints, including endpoints selected through a context.
if [[ -n ${DOCKER_CONTEXT:-} ]]; then
  endpoint=$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}')
else
  endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
fi
[[ $endpoint == unix://* ]] || { echo 'WALKTHROUGH: FAIL local Unix dockerd required'; exit 1; }
export DOCKER_HOST=$endpoint
unset DOCKER_CONTEXT
for project in hullgate-walkthrough hullgate-walkthrough-traefik; do
  if [[ -n $(docker ps -aq --filter "label=com.docker.compose.project=$project") ]]; then
    echo "WALKTHROUGH: FAIL existing project $project must not be touched"; exit 1
  fi
done
if docker network inspect hullgate-walkthrough-upstreams >/dev/null 2>&1; then
  echo 'WALKTHROUGH: FAIL upstream network already exists'; exit 1
fi
for tool in sudo make git curl python3 openssl base32 base64 stat ss flock; do command -v "$tool" >/dev/null; done
sudo -n true
for port in 443 14791; do
  if ss -ltnH | awk '{print $4}' | grep -Eq ":${port}$"; then
    echo "WALKTHROUGH: FAIL port $port is occupied"; exit 1
  fi
done
for volume in hullgate_data; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "WALKTHROUGH: FAIL existing volume $volume must not be touched"; exit 1
  fi
done
if docker image inspect hullgate:current >/dev/null 2>&1; then
  echo 'WALKTHROUGH: FAIL existing hullgate:current must not be replaced'; exit 1
fi
WORK=$(mktemp -d /tmp/hullgate-guides.XXXXXX)
CLONE=$WORK/target
export CLONE
mkdir "$WORK/blocks" "$WORK/logs"
export COMPOSE_PROJECT_NAME=hullgate-walkthrough
export VERIFY_INSECURE=1
export NO_PROXY='*' no_proxy='*'
export EDITOR=true  # O5 supplies the fixture values; S4/C3 still check the actual file.
# Do not let the orchestrator's deployment environment override fixture .env values.
while IFS='=' read -r name _; do
  if [[ $name =~ ^[A-Z_]+$ ]]; then unset "$name"; fi
done < "$ROOT/deploy/env.example"
unset TRAEFIK_ADDRESS
NET=hullgate-walkthrough-traefik
UPSTREAMS_NETWORK=hullgate-walkthrough-upstreams
export UPSTREAMS_NETWORK
NETWORK_CREATED=0
TRAEFIK_STARTED=0
DEPLOY_STARTED=0
ok=0
skipped=0
current=setup
trap 'printf "WALKTHROUGH %s FAIL (logs: %s/logs)\n" "$current" "$WORK"; exit 1' ERR
cleanup() {
  rc=$?
  trap - ERR
  set +e
  if [[ $DEPLOY_STARTED == 1 ]]; then
    (cd "$CLONE" && docker compose -f deploy/compose.prod.yaml --env-file .env down -v --remove-orphans) >/dev/null 2>&1
    docker image rm hullgate:current >/dev/null 2>&1
  fi
  if [[ $TRAEFIK_STARTED == 1 ]]; then
    docker compose -p hullgate-walkthrough-traefik -f "$WORK/traefik.yaml" --env-file "$WORK/traefik.env" down -v >/dev/null 2>&1
  fi
  if [[ $NETWORK_CREATED == 1 ]]; then docker network rm "$NET" >/dev/null 2>&1; fi
  # Only generated fixture keys, inside this mktemp directory.
  if [[ -d $CLONE/secrets ]]; then sudo -n rm -rf -- "$CLONE/secrets"; fi
  printf 'WALKTHROUGH: %s; %s passed, %s named skips; logs %s/logs\n' \
    "$([[ $rc == 0 ]] && echo OK || echo FAIL)" "$ok" "$skipped" "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Extract the guides themselves, so a changed command is exercised on the next run.
python3 - "$WORK/blocks" <<'PY'
import os
import re
import sys
from pathlib import Path
out = Path(sys.argv[1])
for guide in (Path(os.environ['ROOT']) / 'docs/guides').glob('*.md'):
    parts = re.split(r'^## Step ([A-Z]\d+) — [^\n]*\n', guide.read_text(), flags=re.M)
    for i in range(1, len(parts), 2):
        step, body = parts[i:i+2]
        body = body.split('\n## ', 1)[0]
        blocks = re.findall(r'^```sh\n(.*?)\n```', body, re.M | re.S)
        for j, block in enumerate(blocks):
            replacements = {
                '/opt/hullgate': os.environ['CLONE'],
                '<repository-url>': os.environ['ROOT'],
                '<compose-root>': str(out.parent / 'projects'),
                '<previous-ref>': '${PREVIOUS_REF:?Run update M1 first}',
                '<ref>': os.environ['REF'],
                '<role-path>': 'recette-socle/essai/dev/principal',
                '<MCP_HOST>': 'mcp.example.test',
            }
            for old, new in replacements.items():
                block = block.replace(old, new)
            # Keep install O5/P1/M1 on the chosen local candidate, not another branch's main.
            block = block.replace('origin/main', os.environ['REF'])
            (out / f'{guide.stem}-{step}-{j}.sh').write_text(block + '\n')
PY
# Local Traefik fixture reuses the project's pinned image and disabled external ACME.
sed 's/hullgate-smoke/hullgate-walkthrough/g' "$ROOT/deploy/smoke/compose.traefik.yaml" > "$WORK/traefik.yaml"
cat > "$WORK/traefik.env" <<'ENV'
TRAEFIK_NETWORK=hullgate-walkthrough-traefik
TRAEFIK_ADDRESS=172.30.248.2
TRAEFIK_HTTPS_PORT=443
ENV
if docker network inspect "$NET" >/dev/null 2>&1; then echo 'WALKTHROUGH: FAIL network exists'; exit 1; fi
docker network create --subnet 172.30.248.0/24 "$NET" > "$WORK/logs/network.log"
NETWORK_CREATED=1
TRAEFIK_STARTED=1
docker compose -p hullgate-walkthrough-traefik -f "$WORK/traefik.yaml" --env-file "$WORK/traefik.env" up -d > "$WORK/logs/traefik.log" 2>&1
mkdir -p "$WORK/projects/fixture"
cat > "$WORK/projects/fixture/compose.yaml" <<'YAML'
services:
  example:
    labels:
      traefik.docker.network: hullgate-walkthrough-traefik
      traefik.http.routers.example.entrypoints: websecure
      traefik.http.routers.example.tls.certresolver: smoke-acme
YAML

# Source each selected block in this shell: O3's URL and M1's previous ref survive.
block() { source "$WORK/blocks/$1-$2-$3.sh"; }
step() {
  current=$1
  shift
  "$@" > "$WORK/logs/$current.log" 2>&1
  # Match observable success before printing OK; a shell's final zero alone is insufficient.
  case "$current" in
    install-O5|keys-S4|keys-C3) grep -q 'CONFIG OK' "$WORK/logs/$current.log" ;;
    admin-P1) grep -q 'P1: OK composition and TRUSTED_PROXIES CIDRs' "$WORK/logs/$current.log" ;;
    install-O6|keys-S5|keys-C4|update-M2) grep -q "DEPLOY: OK $REF" "$WORK/logs/$current.log" ;;
    install-O7|install-O9|keys-S6|update-M3|admin-A1) grep -q 'VERIFY: 6/6 OK' "$WORK/logs/$current.log" ;;
    keys-S8) grep -q 'VERIFY-AUTH: 4/4 OK' "$WORK/logs/$current.log" ;;
    keys-C5) grep -q 'connections key: usable' "$WORK/logs/$current.log" ;;
    update-M4)
      grep -q "DEPLOY: OK $PREVIOUS_REF" "$WORK/logs/$current.log"
      grep -q 'VERIFY: 6/6 OK' "$WORK/logs/$current.log" ;;
    backup-S1) grep -q 'BACKUP: OK' "$WORK/logs/$current.log" ;;
    backup-S3) grep -q 'RESTORE-CHECK: OK' "$WORK/logs/$current.log" ;;
    admin-A2) grep -q 'VERIFY-ADMIN: 4/4 OK' "$WORK/logs/$current.log" ;;
  esac
  ok=$((ok + 1))
  printf 'WALKTHROUGH %s OK\n' "$current"
}
skip() {
  skipped=$((skipped + 1))
  printf 'WALKTHROUGH %s SKIP %s\n' "$1" "$2"
}

step install-O1 block install O1 0
skip install-O2 'local .test hostname; DNS and real ACME require an operator-owned domain'
step install-O3 block install O3 0
step install-O4 block install O4 0
# O5's editor is replaced by deterministic example parameters, not appended declarations.
cp "$CLONE/deploy/env.example" "$CLONE/.env"
python3 - <<'PY'
import os
from pathlib import Path
p = Path(os.environ['CLONE']) / '.env'
values = dict(MCP_HOST='mcp.example.test', TRAEFIK_NETWORK='hullgate-walkthrough-traefik',
              TRAEFIK_ENTRYPOINT='websecure', TRAEFIK_CERTRESOLVER='smoke-acme',
              TRUSTED_PROXIES='172.30.248.2/32', UPSTREAMS_NETWORK='hullgate-walkthrough-upstreams')
s = p.read_text().replace('/opt/hullgate', os.environ['CLONE'])
p.write_text('\n'.join(line.split('=', 1)[0]+'='+values[line.split('=', 1)[0]]
                      if line.split('=', 1)[0] in values else line
                      for line in s.splitlines())+'\n')
PY
install_env() { block install O5 0; block install O5 1; block install O5 2; }
step install-O5 install_env
step keys-S1 block keys-and-upstreams S1 0
# Never record the generated TOTP secret/URI in the transcript or log.
create_totp() { block keys-and-upstreams S2 0 >/dev/null; test -s "$CLONE/secrets/authorize_totp"; }
step keys-S2 create_totp
# Authenticator enrolment is an explicit unobserved portion of S2.
key_permissions() {
  block keys-and-upstreams S3 0
  test "$(stat -c '%u:%g %a' "$CLONE/secrets/signing_key")" = '10001:10001 400'
  test "$(stat -c '%u:%g %a' "$CLONE/secrets/authorize_totp")" = '10001:10001 400'
}
step keys-S3 key_permissions
step keys-S4 block keys-and-upstreams S4 0
connection_key() {
  block keys-and-upstreams C2 0
  test "$(stat -c '%u:%g %a' "$CLONE/secrets/connections_key")" = '10001:10001 400'
}
# Installation O5 requires C2/C3 before the first deployment; C1 runs once healthy.
step keys-C2 connection_key
step keys-C3 block keys-and-upstreams C3 0
predeploy() { block admin P1 0; block admin P1 1; }
step admin-P1 predeploy
DEPLOY_STARTED=1
step install-O6 block install O6 0
verify_install() {
  block install O7 0
  # Workstation readyz uses only local routing and a fixture certificate.
  curl --noproxy '*' -ksSf --resolve mcp.example.test:443:127.0.0.1 https://mcp.example.test/readyz
}
step install-O7 verify_install
skip install-O8 'persistent cron is outside this disposable target; backup command runs at O7 and backup S1'
step install-O9 block install O9 0
step keys-S5 block keys-and-upstreams S5 0
step keys-S6 block keys-and-upstreams S6 0
step keys-S7 block keys-and-upstreams S7 0
step keys-S8 block keys-and-upstreams S8 0
step keys-S9 block keys-and-upstreams S9 0
for id in S10 S11 S12; do skip "keys-$id" 'external Claude clients and enrolled authenticator unavailable; S2 enrolment also unobserved'; done
step keys-C1 block keys-and-upstreams C1 0
step keys-C4 block keys-and-upstreams C4 0
step keys-C5 block keys-and-upstreams C5 0
for id in C6 C7 C8 C9 C10 C11 C12 C13; do skip "keys-$id" 'requires provider OAuth connection; key-loss/revocation branches require that connection'; done
for id in R1 R2 R3 R4 R5 R6 R7 R8 R9 R10 F1 F2 F3 F4 F5 F6; do skip "keys-$id" 'requires Gitea upstream, authorized provider connection and/or external Claude client'; done
step update-M1 block update M1 0
step update-M2 block update M2 0
step update-M3 block update M3 0
# Exercise rollback after a successful deployment using M1's saved SHA; same candidate
# is redeployed so this test does not assume an older schema can consume the new database.
step update-M4 block update M4 0
for id in F1 F2 F3 F4 F5 F6; do skip "update-$id" 'optional destructive legacy-volume migration; no legacy installation in this fresh target'; done
step backup-S1 block backup-restore S1 0
skip backup-S2 'persistent cron omitted; daily scheduling is an operator observation'
step backup-S3 block backup-restore S3 0
skip backup-S4 'guide explicitly excludes destructive in-place restore from acceptance'
step admin-A1 block admin A1 0
step admin-A2 block admin A2 0
for id in A3 A4 A5 A6 A7 B1 B2 B3 B4 C1 C2 C3 D1 D2 D3; do
  skip "admin-$id" 'requires SSH/browser/passkeys/provider state or optional public-admin origin; local read-only console checked at A2'
done
current=coverage
[[ $((ok + skipped)) == 82 ]]
