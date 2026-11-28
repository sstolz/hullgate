# Runner of tests/browser (F-016, BR-035 v2): the pinned Playwright image (its browsers), the
# runner's Python packages installed from hashes only, and a Docker CLI to drive the gateway
# container (`hullgate admin bootstrap` is host only, BR-201). Built by tests/browser/run.sh.
ARG PLAYWRIGHT_IMAGE
FROM docker@sha256:5e6baf67325a79f64ed3bed6863954d5e05d0f3d4e57cbdb14809346814bbfdb AS dockercli
FROM ${PLAYWRIGHT_IMAGE}
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY requirements.txt /tmp/requirements.txt
RUN PIP_BREAK_SYSTEM_PACKAGES=1 pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt
# axe-core (MPL-2.0, test tooling only, never in the gateway image): BR-232's audit, reported as a
# warning. Pinned by version and by SHA-256; the build fails on any other content.
ARG AXE_URL=https://cdn.jsdelivr.net/npm/axe-core@4.10.3/axe.min.js
ARG AXE_SHA256=880970c081707360e64f34cea25ff91892f5bc95675b0776925b9709dd8a68bb
RUN python3 -c "import hashlib,sys,urllib.request as u; d=u.urlopen(sys.argv[1], timeout=60).read(); assert hashlib.sha256(d).hexdigest()==sys.argv[2], 'axe-core: unexpected sha256'; open('/opt/axe.min.js','wb').write(d)" "$AXE_URL" "$AXE_SHA256"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AXE_JS=/opt/axe.min.js
