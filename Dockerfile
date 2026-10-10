# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS base

FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv-build

FROM ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv

FROM --platform=$BUILDPLATFORM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS assets

RUN uname -m

COPY --from=uv-build /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl make \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project

COPY . .
RUN make css i18n-compile

FROM base AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project

COPY README.md LICENSE ./
COPY . .
COPY --from=assets /build/src/assetboard/static/css/app.css src/assetboard/static/css/app.css
COPY --from=assets /build/src/assetboard/locales/en/LC_MESSAGES/messages.mo src/assetboard/locales/en/LC_MESSAGES/messages.mo
COPY --from=assets /build/src/assetboard/locales/pl/LC_MESSAGES/messages.mo src/assetboard/locales/pl/LC_MESSAGES/messages.mo

RUN uv build --wheel --out-dir /wheels \
    && uv pip install --python /opt/venv/bin/python --no-deps /wheels/*.whl

FROM base AS runtime

ARG VERSION=0.0.0
ARG SOURCE_URL
ARG REVISION

LABEL org.opencontainers.image.title="AssetBoard" \
      org.opencontainers.image.description="A self-hosted personal finance web app" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PATH="/opt/venv/bin:${PATH}" \
    ASSETBOARD_DATA_DIR=/data \
    ASSETBOARD_ENV=prod \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp

WORKDIR /app

RUN mkdir -p /data /licenses/AssetBoard/vendor \
    && chown 10001:10001 /data

COPY --from=builder --chown=10001:10001 /opt/venv /opt/venv
COPY --from=builder /build/LICENSE /licenses/AssetBoard/LICENSE
COPY --from=builder /build/src/assetboard/static/vendor/*-LICENSE.txt /licenses/AssetBoard/vendor/
COPY --from=builder /build/src/assetboard/static/vendor/ECHARTS-NOTICE.txt /licenses/AssetBoard/vendor/

VOLUME ["/data"]
EXPOSE 8000
USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["assetboard", "serve", "--host", "0.0.0.0", "--port", "8000"]
