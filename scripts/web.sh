#!/bin/sh
# Console steps of the session (F-016, BR-035 v2, BR-231), called by the Makefile:
#   sh scripts/web.sh <node-version> <node-image> version   one line: the Node that will run
#   sh scripts/web.sh <node-version> <node-image> gate      lockfile in sync (npm ci), typecheck,
#                                                           lint, unit tests
#   sh scripts/web.sh <node-version> <node-image> audit     `npm audit --json`, production then
#                                                           all dependencies, to stdout
# A local Node of the pinned version is used when present, otherwise the pinned image through
# Docker, on a copy of web/ (no node_modules is written into the tree); neither: exit 1.
set -u
VERSION=$1
IMAGE=$2
ACTION=$3
WEB=$(pwd)/web

local_node() {
  [ "$(node --version 2>/dev/null)" = "v$VERSION" ]
}

docker_node() {
  docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE" >/dev/null 2>&1
}

steps() {
  case "$ACTION" in
    gate) echo 'npm ci --no-audit --no-fund --loglevel=error && npm run --silent check && npm run --silent lint && npm run --silent test' ;;
    audit) echo 'npm ci --no-audit --no-fund --loglevel=error --ignore-scripts >/dev/null 2>&1; npm audit --omit=dev --json > /tmp/prod.json; npm audit --json > /tmp/all.json; printf "{\"production\": %s, \"all\": %s}\n" "$(cat /tmp/prod.json)" "$(cat /tmp/all.json)"' ;;
    *) echo "unknown action: $ACTION" >&2; exit 2 ;;
  esac
}

if [ "$ACTION" = version ]; then
  if local_node; then echo "node v$VERSION (local)"; exit 0; fi
  if docker_node; then echo "node v$VERSION (image $IMAGE)"; exit 0; fi
  echo "no node v$VERSION: neither local nor through docker"
  exit 1
fi

SCRIPT=$(steps) || exit 2
if local_node; then
  cd "$WEB" && exec sh -c "$SCRIPT"
fi
if ! docker_node; then
  echo "web: no node v$VERSION, neither local nor the image $IMAGE through docker" >&2
  exit 1
fi
exec docker run --rm -e npm_config_update_notifier=false -v "$WEB:/src:ro" \
  -v hullgate-npm-cache:/root/.npm "$IMAGE" sh -c \
  "mkdir /w && cd /src && tar cf - --exclude=./node_modules --exclude=./dist . | (cd /w && tar xf -) && cd /w && $SCRIPT"
