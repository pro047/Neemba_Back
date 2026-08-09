FROM node:22-bullseye

# yt-dlp / streamlink 는 초기 유튜브 수집 시절의 잔재라 제거했다. 둘 다 이미
# 죽어 있었다 — bullseye 의 시스템 파이썬이 3.9 라 yt-dlp 는 실행 즉시
# ImportError(3.10+ 요구), streamlink 는 저장소 고정판 2.0.0(현재 7.x).
# 매 빌드마다 깨진 것을 받아오고 있었고, 설치돼 있다는 사실이 "쓸 수 있다"는
# 오해를 만든다. 유튜브 수집은 IP 차단으로 기각됐다 —
# 근거와 되살릴 때의 주의사항은 docs/youtube-ingest-rejected.md.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    jq \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
