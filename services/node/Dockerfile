FROM node:22-bullseye

RUN apt-get update && apt-get install -y \
    python3 \
    ffmpeg \
    jq \
    curl \
    git \
    ca-certificates \
    streamlink \
    && rm -rf /var/lib/apt/lists/*

RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
-o /usr/local/bin/yt-dlp && chmod a+rx /usr/local/bin/yt-dlp

# 필수 도구
RUN apt-get update && apt-get install -y curl gnupg ca-certificates && rm -rf /var/lib/apt/lists/*

# 구글 키를 keyring으로 설치
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg

# 리포 추가 (signed-by 지정 중요)
RUN echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
  > /etc/apt/sources.list.d/google-cloud-sdk.list

# 설치
RUN apt-get update && apt-get install -y google-cloud-cli && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    # 최신 버전 태그 가져오기
    VER=$(curl -s https://api.github.com/repos/nats-io/natscli/releases/latest | jq -r .tag_name | sed 's/^v//'); \
    case "$ARCH" in \
      amd64)  DEB="nats-${VER}-amd64.deb" ;; \
      arm64)  DEB="nats-${VER}-arm64.deb" ;; \
      *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac; \
    curl -fL -o /tmp/nats.deb "https://github.com/nats-io/natscli/releases/download/v${VER}/${DEB}"; \
    apt-get update && apt-get install -y --no-install-recommends /tmp/nats.deb || (dpkg -i /tmp/nats.deb && apt-get -f install -y); \
    rm -rf /var/lib/apt/lists/* /tmp/*

ENV PATH="/root/.local/bin:${PATH}"
ENV PATH="/app/node_modules/.bin:${PATH}"

WORKDIR /app/services/node

COPY package.json package-lock.json ./

RUN npm ci

COPY . .

RUN npm run build

ENV NODE_ENV=production

CMD [ "npm", "run", "start" ]
