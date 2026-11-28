# syntax=docker/dockerfile:1
# Single image of the hullgate gateway (F-004): linux/amd64 and linux/arm64 from this one file.
#
# - Bases pinned by multi-architecture index digest (BR-029): Python 3.13.18 (the one of the POC
#   judged by the bench), uv 0.13.6.
# - `build` stage played on the architecture of the build machine ($BUILDPLATFORM):
#   `uv sync --locked --no-dev` installs the wheels of the target architecture ($TARGETARCH); no
#   emulation is needed and the `runtime` stage holds no RUN instruction.
# - `uv` stays in the `build` stage; the `runtime` stage receives only the environment (BR-029).
# - Numeric user 10001:10001 (BR-030); only /data is writable (BR-031).
# - F-016 (BR-029 v2, BR-212): stage `console` builds the admin console on $BUILDPLATFORM from
#   `web/package-lock.json` (`npm ci`), Node 26.13.0 pinned by index digest; only the static
#   bundle reaches the runtime stage, at /app/console. The runtime image holds no Node.

FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM --platform=$BUILDPLATFORM node@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072 AS console
ENV npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM --platform=$BUILDPLATFORM python@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2 AS build
ARG TARGETARCH
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3.13 \
    UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
COPY src ./src
# Wheel platform: the target architecture named by buildx, translated into a manylinux tag.
RUN case "$TARGETARCH" in \
      amd64) platform=x86_64-manylinux_2_36 ;; \
      arm64) platform=aarch64-manylinux_2_36 ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && uv sync --locked --no-dev --no-editable --python-platform "$platform" \
 && mkdir -p /out/data

FROM python@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2 AS runtime
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PUBLIC_PORT=4790 \
    ADMIN_PORT=4791
COPY --from=build /app/.venv /app/.venv
COPY --from=build --chown=10001:10001 /out/data /data
COPY --from=console /web/dist /app/console
WORKDIR /app
USER 10001:10001
EXPOSE 4790 4791
ENTRYPOINT ["python", "-m", "hullgate"]
