# Phase 7 라이브(실환경) 검증 런북

> **목적**: 클라우드 컨테이너 독립 검증(더미값·스텁·호스트 우회)이 입증하지 못한 부분을
> **맥/실서버 + 실제 브라우저**에서 사람이 직접 확인하는 체크리스트.
> 대상 브랜치: `claude/monitor-phase7-access-retention-qx7jB`. 도메인: `neemba.app`.
>
> **왜 필요한가 (컨테이너 검증의 한계)**
> - 인증/라우팅/보존 규칙은 컨테이너에서 **구조 검증 완료**(트래커 Phase 7 참조).
> - 그러나 ① **브라우저가 WS 핸드셰이크에 캐시된 Basic 자격증명을 자동첨부**하는지(결정 #2의 핵심 전제),
>   ② 실 htpasswd(bcrypt)·실 인증서·실 시크릿로 prod 전체 스택이 뜨는지,
>   ③ pg-retention 24h 스케줄 루프가 실 데이터 위에서 도는지 —— 이 셋은 **실환경에서만** 확인 가능.
>
> **안전 원칙**: 실비밀(htpasswd 자격증명·`.env.prod`·인증서)은 **절대 커밋/공유 금지**.

---

## 0. 사전 확인

```sh
cd <repo>                                   # 서버/맥의 실제 레포 경로
ls infra/letsencrypt/live/neemba.app/       # fullchain.pem / privkey.pem 존재?
test -f .env.prod && echo ".env.prod OK"    # POSTGRES_* 5종 실값 존재?
```

- **prod 기동 방식 확정(§8-1 미해결 TODO)**: `make up-prod`는 base `docker-compose.yml`을 참조해
  레포 상태에서 깨져 있음. 아래 런북은 **`docker compose -f docker-compose.prod.yml --env-file .env.prod` 직접 실행** 기준.
  맥에 untracked base compose가 따로 있다면 그 방식으로 대체(이때도 검증 항목은 동일).
- `.env.prod` 필수 키: `POSTGRES_HOST=postgres`(서비스명) · `POSTGRES_PORT=5432` · `POSTGRES_USER` ·
  `POSTGRES_PASSWORD` · `POSTGRES_DATABASE` + 기존 NATS/DeepL/WS 키. **하나라도 빠지면 python lifespan이 require_env로 부팅 실패.**

---

## 1. 실 htpasswd 생성 (bcrypt) — 운영 1회

htpasswd 파일은 **레포 미커밋**. node/python과 동일한 시크릿 디렉터리에 둔다.

```sh
sudo mkdir -p /var/lib/neemba/secrets

# htpasswd 바이너리가 있으면:
htpasswd -B -c /var/lib/neemba/secrets/monitor.htpasswd monitor   # 비번 입력(2회)

# 없으면(예: 순정 서버) httpd 이미지로 한 줄 생성:
# docker run --rm httpd:alpine htpasswd -nbB monitor '<실비번>' \
#   | sudo tee /var/lib/neemba/secrets/monitor.htpasswd >/dev/null

sudo chmod 640 /var/lib/neemba/secrets/monitor.htpasswd
```

**확인** — bcrypt 해시(`$2y$...`)인지:
```sh
sudo cut -d: -f1 /var/lib/neemba/secrets/monitor.htpasswd      # => monitor
sudo cut -d: -f2 /var/lib/neemba/secrets/monitor.htpasswd | cut -c1-4   # => $2y$  (bcrypt)
```
> ⚠️ 컨테이너 독립 검증은 `openssl passwd -apr1`(apr1) 더미를 썼다. nginx는 apr1·bcrypt 둘 다 지원하나,
> **운영은 반드시 `-B`(bcrypt)** 로 만들 것. 이 단계가 실제 bcrypt 동작을 확정한다.

---

## 2. prod 스택 기동 + nginx가 htpasswd를 실제로 마운트했는지

```sh
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
```

**기대**: `postgres`(healthy) · `nginx` · `python` · `pg-retention` · `node` · `nats` 등 전부 Up.

```sh
# nginx가 시크릿을 ro로 보고 있는가
docker exec nginx ls -l /var/lib/neemba/secrets/monitor.htpasswd     # 파일 보임
docker exec nginx nginx -t                                           # syntax ok

# python entrypoint가 alembic upgrade head를 돌렸는가 (스키마 생성)
docker logs python 2>&1 | grep -E "alembic upgrade head|migrations applied"
docker logs python 2>&1 | grep -E "lifespan : db pool created|init done"   # 풀 배선 성공

# pg-retention 사이드카가 스케줄 모드로 떴는가
docker logs pg-retention 2>&1 | grep -E "starting \(window=30d, interval=24h"
```

> htpasswd를 1단계 이후에 만들었거나 교체했다면: `docker compose ... restart nginx`
> (또는 레포의 nginx-reloader 경유) 후 다시 확인.

---

## 3. HTTP 인증 (실 도메인 + 실 자격증명)

`<PW>` = 1단계에서 정한 실비번. 인증서가 유효하면 `-k` 불필요.

```sh
DOM=https://neemba.app

# (a) 무자격증명 → 401 (세 위치 전부)
for u in /monitor/ /monitor/app.js /api/monitor/sessions; do
  printf '%-26s %s\n' "$u" "$(curl -s -o /dev/null -w '%{http_code}' $DOM$u)"   # 기대 401
done

# (b) 오자격증명 → 401
curl -s -o /dev/null -w '%{http_code}\n' -u monitor:WRONG $DOM/monitor/         # 기대 401

# (c) 올바른 자격증명 → 200 / JS / JSON
curl -s -o /dev/null -w 'monitor %{http_code} %{content_type}\n'      -u monitor:<PW> $DOM/monitor/
curl -s -o /dev/null -w 'app.js  %{http_code} %{content_type}\n'      -u monitor:<PW> $DOM/monitor/app.js
curl -s -o /dev/null -w 'api     %{http_code} %{content_type}\n'      -u monitor:<PW> $DOM/api/monitor/sessions
curl -s -u monitor:<PW> $DOM/api/monitor/sessions | head -c 300; echo
```
**기대**: (a) 401×3 · (b) 401 · (c) `/monitor/`=200 text/html, `app.js`=200 application/javascript,
`/api/monitor/sessions`=200 application/json(세션 배열).

---

## 4. ⭐ 브라우저 WS 자격증명 자동첨부 — **결정 #2의 핵심·라이브 1순위**

> 이것이 컨테이너에서 **구조상 검증 불가**했던 항목. CLI 테스트는 Authorization 헤더를 명시적으로 실어
> "nginx가 헤더 있으면 통과"만 입증했다. 정작 확인할 것은 **브라우저 `new WebSocket()`이 `/monitor`에서
> 캐시된 same-origin Basic 자격증명을 업그레이드 핸드셰이크에 자동으로 붙이는가** 이다.

**Chrome / Firefox / Safari 각각** 수행:

1. 시크릿(프라이빗) 창에서 `https://neemba.app/monitor/` 접속 → Basic 인증 다이얼로그에 `monitor` / `<PW>` 입력.
2. 페이지 로드 후 DevTools → **Network → WS** 필터.
3. **라이브(진행중) 세션** 하나 선택 → 라이브 토글 → 프론트가 `wss://neemba.app/ws/monitor?sessionId=...` 연결 시도.
4. **확인 A**: 해당 WS 항목이 **101 Switching Protocols** (app.js 무수정 상태로 연결 성공).
5. **확인 B**: 그 WS 핸드셰이크의 **Request Headers에 `Authorization: Basic ...`가 자동으로 실렸는지**
   (브라우저가 붙인 것 — 코드가 붙인 게 아님). 라이브 마스킹 페이로드가 표에 append되는지.
6. **음성확인**: 자격증명 없는 새 시크릿 창에서 직접 `wss://neemba.app/ws/monitor?sessionId=...` 시도 → **연결 거부(401)**.

**판정**:
- 세 브라우저 전부 101 + Authorization 자동첨부 → **전제 입증, app.js 무수정 확정.**
- **어느 브라우저든 실패**(WS가 401/연결 안 됨)하면 → 결정 #2 폴백 발동:
  `/ws/monitor` 블록만 Basic 대신 `allow <사무실/VPN CIDR>; deny all;` IP 화이트리스트로 전환
  (나머지 `/monitor`·`/api/monitor/`는 Basic 유지). prod+dev conf 양쪽 동일 적용 후 재기동·재검증.
  → 이 경우 **구현 수정이 발생하므로 별도 커밋**, 본 런북에 결과 기록.

> 과거 Safari가 WS 자격증명 자동첨부에 가장 보수적이었으므로 Safari를 반드시 포함.

---

## 5. 음성검증 / 회귀 (인증이 monitor 밖으로 새지 않는지 — 실환경)

```sh
DOM=https://neemba.app
curl -s -o /dev/null -w '/health      %{http_code}\n'  $DOM/health          # 200 (무인증)
curl -s -o /dev/null -w '/api/mic     %{http_code}\n'  $DOM/api/mic          # NOT 401 (node로 라우팅)
curl -s -o /dev/null -w '/ (root)     %{http_code}\n'  $DOM/                 # NOT 401
```
- 실제 **클라이언트 mic/스트리밍 플로우**(앱/웹 클라)가 인증 없이 평소대로 동작하는지 — 회귀 없음 확인.
- 실제 **클라 `/ws`**(client-facing WebSocket)가 인증 요구 없이 연결되는지(`/ws/monitor` longer-prefix가
  `/ws`보다 먼저 매칭되므로 클라 WS는 무인증 유지) — 실제 클라로 확인.

**기대**: monitor 3위치만 401, 그 외 전부 비-401(정상 동작).

---

## 6. 보존(pg-retention) — 실 스케줄 동작 확인

**6-1. 스케줄 루프 관찰(비파괴)** — 권장 기본:
```sh
docker logs pg-retention 2>&1 | tail -20
# "starting (window=30d, interval=24h, oneshot=0)" 그리고 하루 1회 "sweep done at ..." 확인
# 첫 sweep의 NOTICE 로그로 삭제 건수 확인:
docker logs pg-retention 2>&1 | grep "pg-retention: deleted"
```

**6-2. (선택) 수동 1회 sweep** — ⚠️ **실 DB의 30일 초과 행을 즉시 삭제**한다(스케줄 루프가 어차피 할 일):
```sh
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm \
  -e RETENTION_ONESHOT=1 pg-retention
# => NOTICE: pg-retention: deleted N translations, M sessions (window=30 days)
```

**6-3. 라이브 세션 보호 확인** — 30일 넘은 **진행중(ended_at NULL)** 세션이 살아있는지:
```sh
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tA -c \
  "SELECT count(*) FILTER (WHERE ended_at IS NULL) AS live_alive,
          count(*) FILTER (WHERE ended_at IS NOT NULL AND ended_at < now()-interval '30 days') AS stale_ended_remaining
   FROM app.sessions;"
```
**기대**: `stale_ended_remaining = 0`(종료 30일 초과는 전부 삭제됨), 라이브 세션은 나이 무관 생존.

> 튜닝이 필요하면 `.env.prod`에 `RETENTION_DAYS` / `RETENTION_SLEEP_INTERVAL` 추가 후 `pg-retention` 재기동.

---

## 7. 풀스택 E2E 정합성 (실 번역 1건)

1. 실제 STT→번역 플로우로 세션 1개 생성·번역 발생.
2. `/monitor/`(인증 후)에서 그 세션이 목록에 뜨고, 원문↔번역 2열이 **마스킹된 상태**로 보이는지.
3. DB에 원문 PII가 저장되지 않았는지(mask-at-write):
```sh
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tA -c \
  "SELECT count(*) FROM app.translations
    WHERE source_text ~ '[0-9]{6}' OR translated_text ~ '@';"   # 기대 0 (또는 시드 무관)
```

---

## 합격 기준 (전부 충족 시 라이브 검증 PASS)

- [ ] 1. 실 htpasswd가 **bcrypt(`$2y$`)** 로 생성·마운트됨, nginx가 인식.
- [ ] 2. prod 스택 전부 Up, python alembic upgrade·db pool 성공, pg-retention 스케줄 기동.
- [ ] 3. HTTP: 무/오자격 401, 올바른 자격 200·JS·JSON (세 위치).
- [ ] 4. ⭐ **Chrome·Firefox·Safari** 전부 `/monitor` 인증 후 `wss://.../ws/monitor` **101 + Authorization 자동첨부**.
      (실패 브라우저 발생 시 → `/ws/monitor` IP 화이트 폴백 적용·재검증·기록)
- [ ] 5. 음성검증: `/health` 200·`/api/*` 비-401·클라 `/ws` 무인증·`/` 비-401, 기존 클라 플로우 무회귀.
- [ ] 6. pg-retention 스케줄 동작·종료 30일 초과 삭제·**라이브 세션 생존**.
- [ ] 7. 실 번역이 마스킹된 채 monitor에 표시·DB에 원문 PII 미저장.

> 결과는 `docs/monitoring-plan.md` §10 인계 메모에 한 줄(`(YYYY-MM-DD, Phase 7 라이브 검증)`)로 기록.
> 4번 폴백을 발동했다면 그 conf 수정은 별도 커밋으로 남길 것.
