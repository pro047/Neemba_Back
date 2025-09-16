FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/services/python

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      curl ca-certificates mecab mecab-ipadic-utf8 unzip; \
    rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh -s -- -y
ENV PATH="/root/.local/bin:${PATH}"

ARG NATS_VERSION=0.1.7
ARG TARGETARCH
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64)  NATS_ARCH=amd64 ;; \
      arm64)  NATS_ARCH=arm64 ;; \
      *) echo "Unsupported arch: $TARGETARCH" && exit 1 ;; \
    esac; \
    curl -fL "https://github.com/nats-io/natscli/releases/download/v${NATS_VERSION}/nats-${NATS_VERSION}-linux-${NATS_ARCH}.zip" -o /tmp/nats.zip; \
    unzip -o /tmp/nats.zip -d /tmp; \
    install -m 0755 "/tmp/nats-${NATS_VERSION}-linux-${NATS_ARCH}/nats" /usr/local/bin/nats; \
    rm -rf /tmp/*; \
    nats --version

RUN useradd -m appuser
USER appuser

FROM base AS deps
COPY --chown=appuser:appuser pyproject.toml uv.lock* /app/

RUN uv sync --frozen --no-dev --system

FROM deps AS runtime
COPY --chown=appuser:appuser . /app

ENV PYTHONPATH=/app:/app/services/python

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \ 
CMD curl -fsS http://127.0.0.1:8000/health || exit 1

FROM runtime AS dev
CMD [ "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--lifespan", "on", "--log-level", "info", "--reload", "--reload-dir", "/app/services/python", "--reload-include", "*.py", "--reload-exclude", "__pycache__/*", "--reload-exclude", "*.pyc" ]

FROM runtime AS prod
ENV GUNICORN_WORKERS=2 \
    GUNICORN_TIMEOUT=60
CMD [ "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-W", "2", "-b", "0.0.0.0:8000", "--timeout", "60", "main:app"]