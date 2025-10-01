FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/services/python

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      curl ca-certificates mecab mecab-ipadic-utf8 unzip; \
    rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

RUN useradd -m appuser

ARG NATS_VERSION=0.2.4
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


FROM base AS deps

COPY pyproject.toml uv.lock* /app/

ENV PATH="/usr/local/bin:${PATH}"
ENV PYTHONPATH=/app:/app/services/python

RUN uv --version \ 
    && uv export --frozen --no-dev --format requirements-txt > /tmp/req.lock.txt \
    && uv pip install --system -r /tmp/req.lock.txt \
    && rm -f /tmp/req.lock.txt

FROM deps AS runtime

COPY . /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
CMD curl -fsS http://127.0.0.1:8000/health || exit 1

FROM runtime AS dev
CMD [ "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--lifespan", "on", "--log-level", "info", "--reload"]

FROM runtime AS prod
ENV GUNICORN_WORKERS=1 \
    GUNICORN_TIMEOUT=60
CMD [ "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "1", "-b", "0.0.0.0:8000", "--timeout", "60", "main:app"]