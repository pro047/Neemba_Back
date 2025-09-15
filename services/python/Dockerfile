FROM python:3.12-slim

WORKDIR /app/services/python

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    curl ca-certificates jq unzip gcc g++ make \
    mecab libmecab-dev mecab-ipadic-utf8; \
  rm -rf /var/lib/apt/lists/*

ARG NATS_VERSION=0.1.7
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends unzip; \
    ARCH="$(dpkg --print-architecture)"; \
    case "$ARCH" in \
      amd64)  NATS_ARCH=amd64 ;; \
      arm64)  NATS_ARCH=arm64 ;; \
      *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac; \
    VER="$(curl -s https://api.github.com/repos/nats-io/natscli/releases/latest | jq -r .tag_name | sed 's/^v//')"; \
    URL="https://github.com/nats-io/natscli/releases/download/v${VER}/nats-${VER}-linux-${NATS_ARCH}.zip"; \
    echo "Downloading $URL"; \
    curl -fL "$URL" -o /tmp/nats.zip; \
    unzip -o /tmp/nats.zip -d /tmp; \
    install -m 0755 "/tmp/nats-${VER}-linux-${NATS_ARCH}/nats" /usr/local/bin/nats; \
    rm -rf /var/lib/apt/lists/* /tmp/*; \
    which nats; nats --version

RUN python -m pip install --upgrade pip

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir nats-py kss mecab-python3

COPY . /app

EXPOSE 8000

CMD ["uvicorn", "main:app","--host", "0.0.0.0", "--port", "8000", "--lifespan", "on", "--log-level", "info", "--reload", "--reload-dir", "/app/services/python", "--reload-include", "*.py", "--reload-exclude", "__pycache__/*", "--reload-exclude", "*.pyc"]