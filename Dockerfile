# Multi-arch (amd64/arm64): every base image and locked wheel is available for both.
# Plain Dockerfile syntax (no BuildKit-only flags) so the classic builder works too.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DATA_DIR=/data \
    PUID=1000 \
    PGID=1000

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# No USER line: the entrypoint starts as root only to fix /data ownership, then drops
# to PUID:PGID with setpriv. With compose `user:` it runs unprivileged from the start.
ENTRYPOINT ["docker-entrypoint.sh"]
