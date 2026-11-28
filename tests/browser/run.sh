#!/bin/sh
# `make test-browser` (F-016, BR-035 v2): the console in Chromium against the real image.
#   sh tests/browser/run.sh <gateway image> <pinned Playwright image>
# 1. a throwaway CA and a certificate for the fake provider (gitea.example.test), written by
#    openssl into a gitignored directory of the repository (`.ct-secrets-*`), and a network of
#    the run on which the fake provider (tests/browser/provider.py, in the gateway image) answers
#    on a private address, declared as an internal host (BR-084); the gateway trusts that CA only,
#    mounted over OpenSSL's compiled CA file (BR-084 ignores SSL_CERT_FILE); a fake MCP upstream
#    (tests/browser/upstream.py, plain HTTP, slice 3) answers on the same network as
#    mcp.example.test, an internal MCP host of the services that use it (BR-106);
# 2. starts the gateway image, disposable, ADMIN_URL=http://localhost:4791, no signing key yet
#    (the vault-down banner is read first; the tests then generate a disposable key inside the
#    container's own volume, which never leaves it);
# 3. builds the runner (tests/browser/Dockerfile) and runs pytest in it, in the gateway's network
#    namespace, so that `localhost:4791` is the console and WebAuthn's secure context holds;
# 4. removes the containers, the network, the volume and the certificates on every exit path.
#    Captures land in docs/captures/F-016/; the last line is the pytest count line.
set -eu
IMAGE=$1
PLAYWRIGHT=$2
ROOT=$(pwd)
NAME="hullgate-browser-$$"
PROVIDER="hullgate-browser-provider-$$"
UPSTREAM="hullgate-browser-upstream-$$"
MCP_HOST=mcp.example.test
MCP_PORT=8080
NET="hullgate-browser-net-$$"
TLS="$ROOT/.ct-secrets-browser-$$"
HOST=gitea.example.test
PORT=8443
RUNNER="hullgate-browser-runner:local"
CAPTURES="$ROOT/docs/captures/F-016"
CA_FILE=/usr/lib/ssl/cert.pem  # ssl.get_default_verify_paths().openssl_cafile of the image

cleanup() {
  docker rm -f "$NAME" "$PROVIDER" "$UPSTREAM" >/dev/null 2>&1 || true
  docker volume rm -f "$NAME" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$TLS"
}
trap cleanup EXIT INT TERM

mkdir -p "$CAPTURES" "$TLS"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 \
  -subj "/CN=hullgate browser run CA" -keyout "$TLS/ca.key" -out "$TLS/ca.pem" 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$HOST" \
  -keyout "$TLS/key.pem" -out "$TLS/req.csr" 2>/dev/null
printf 'subjectAltName=DNS:%s\nbasicConstraints=CA:FALSE\n' "$HOST" > "$TLS/ext.cnf"
openssl x509 -req -in "$TLS/req.csr" -CA "$TLS/ca.pem" -CAkey "$TLS/ca.key" -CAcreateserial \
  -days 1 -extfile "$TLS/ext.cnf" -out "$TLS/cert.pem" 2>/dev/null
chmod 0755 "$TLS"
chmod 0644 "$TLS"/*.pem  # throwaway, read by the containers' own users

docker build -q -t "$RUNNER" --build-arg "PLAYWRIGHT_IMAGE=$PLAYWRIGHT" tests/browser >/dev/null
docker network create "$NET" >/dev/null
docker run -d --name "$PROVIDER" --read-only --network "$NET" --network-alias "$HOST" \
  -v "$TLS:/tls:ro" -v "$ROOT/tests/browser:/browser:ro" --entrypoint python "$IMAGE" \
  /browser/provider.py /tls/cert.pem /tls/key.pem "$HOST:$PORT" >/dev/null
docker run -d --name "$UPSTREAM" --read-only --network "$NET" --network-alias "$MCP_HOST" \
  -v "$ROOT/tests/browser:/browser:ro" --entrypoint python "$IMAGE" \
  /browser/upstream.py "$MCP_PORT" >/dev/null
docker run -d --name "$NAME" --read-only --network "$NET" -v "$NAME:/data" \
  -v "$TLS/ca.pem:$CA_FILE:ro" \
  -e PUBLIC_URL=https://gw.browser.test -e ADMIN_URL=http://localhost:4791 \
  -e SIGNING_KEY_FILE=/data/signing_key -e CONNECTIONS_KEY_FILE=/data/connections_key \
  "$IMAGE" >/dev/null
# The first test navigates at once: wait for the admin listener (30 s at most).
ready=0
for _ in $(seq 1 60); do
  if docker exec "$NAME" python -c \
    "import urllib.request as u; u.urlopen('http://127.0.0.1:4791/healthz', timeout=1)" \
    >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
[ "$ready" = 1 ] || { echo "gateway $NAME: admin listener not up after 30 s" >&2; exit 1; }
# The caller's uid writes the captures; the socket's group drives the gateway container.
docker run --rm --network "container:$NAME" --user "$(id -u):$(id -g)" \
  --group-add "$(stat -c %g /var/run/docker.sock)" -e HOME=/tmp \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$ROOT/tests/browser:/browser:ro" -v "$CAPTURES:/captures" \
  -e GATEWAY="$NAME" -e CAPTURES=/captures -e CONSOLE=http://localhost:4791 \
  -e PROVIDER="https://$HOST:$PORT" -e MCP="http://$MCP_HOST:$MCP_PORT" \
  -w /browser "$RUNNER" \
  python -m pytest -c /browser/pytest.ini -p no:cacheprovider /browser
