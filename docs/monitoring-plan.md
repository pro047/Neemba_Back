# 번역 결과 모니터링 페이지 — 구현 플랜

> 목적: 세션별 **원문(STT 텍스트) ↔ 번역문** 페어를 보는 전용 모니터링 페이지 구현.
> 이 문서는 분석 결과와 확정 결정사항, Phase별 작업 명세를 담은 **구현 핸드오프 문서**다.
> 새 구현 세션은 이 문서를 읽고 해당 Phase부터 진행하면 된다.

---

## 0. 한눈에 보기 (확정 결정사항)

| 항목 | 결정 |
|------|------|
| 보는 형태 | **자체 페이지** (원문↔번역 2열 표). Grafana/Loki 아님 |
| 실시간/이력 | **둘 다** — 진행중 세션은 실시간 스트림, 종료 세션은 DB 조회 |
| 데이터 저장 | **PostgreSQL** (마스킹된 원문+번역문 + 세션 메타) |
| 마이그레이션 | **Alembic** + PostgreSQL (런타임 asyncpg, 마이그레이션용 sync 드라이버 psycopg 별도) |
| 보존 기간 | **30일** 자동 삭제 |
| 민감정보 | **마스킹** 처리 (저장 시점 mask-at-write, 원문+번역문 양쪽) |
| 실시간 방식 | **모니터 전용 WS** (방식 1). 클라이언트 WS와 분리 |
| DB 연동 범위 | **Python 서비스에만.** Node는 DB write 없음 |
| Node DB 코드 | `db/pool.ts`/`db/repository/auth.ts`/`db/sql/churches.ts` = **죽은 로그인 잔재 → 폐기** |
| 접근 제어 | nginx Basic Auth 또는 IP 화이트리스트 (현재 인증 전무) |

**예상 공수: 약 1.5~2일** (Python-only 범위). 신규 비용 대부분은 Postgres 컨테이너 구축.

---

## 1. 레포 아키텍처 요약

듀얼 스택 모노레포:
- **Node (TS/Express, :3000)** — RTMP/mic 스트리밍 파이프라인, STT(Google), NATS publish
- **Python (FastAPI, :8000)** — NATS consume → DeepL 번역 → WebSocket 송출
- **NATS JetStream** — 메시지 브로커 (`transcript.session.*`)
- **Nginx** — 리버스 프록시 + SSL(certbot)
- **PostgreSQL** — **현재 compose에 없음 / 연동된 적 없음** (아래 참조)

데이터 흐름:
```
Node STT → NATS(transcript.session.{id}) → Python consumer
   → DeepL 번역 → pusher → WebSocketHub → 클라이언트 WS
```

### 기존 모니터링 인프라
- Node/Python 모두 `/metrics`(Prometheus), `/health` 존재
- `infra/prometheus/`, `infra/grafana/` 설정 폴더는 있으나 **compose에 Prometheus/Grafana 컨테이너 자체가 없음**
- 로그는 Docker json-file(콘솔)뿐, 집계 없음

---

## 2. 번역 결과 데이터 흐름 분석 (중요)

현재 **번역 결과는 어디에도 저장되지 않는다.**
- DB 저장 ❌ (Python `src/repository/`는 빈 폴더, asyncpg 풀은 미사용)
- NATS 재발행 ❌ (Python은 소비자 전용)
- 유일한 흔적: `src/ws/websocket.py`의 `print('hub: broadcast:', text)` → stdout으로 흘러가고 소멸
- 클라이언트 WS payload: `{"sequence", "sentence", "isFinal"}` — **원문이 빠져 있어** 페어 매칭 불가

### 핵심 객체/위치
| 항목 | 위치 |
|------|------|
| 번역 입력 DTO | `services/python/src/dto/translationDto.py` — `TranslationRequestDto`(session_id, segment_id, sequence, source_text, source_lang, target_lang, confidence) |
| 번역 결과 DTO | 동 파일 `TranslationResultDto`(session_id, translated_text만 — **보강 필요**) |
| DeepL 호출 | `services/python/src/deepL/deepL.py` (TextResult 반환) |
| 클라 송출 | `services/python/src/pushClient/pusher.py` → `src/ws/websocket.py` `broadcast_to_session()` |
| Node publish | `services/node/src/js_pub.ts` `PublishEvent`(transcriptText=원문, source/targetLanguage, createdAt 등) |

→ **캡처 지점**: `pusher.py` 경로에서 원문(`source_text`)을 함께 들고 와 마스킹 후 저장 + 모니터 WS 송출.

---

## 3. Node 세션 종료 로직 분석

### 종료 경로 2개
| | mic 경로 (메인) | RTMP 경로 (레거시) |
|---|---|---|
| 엔드포인트 | `POST /api/mic/stop` | `POST /api/sessions/stop` |
| 핸들러 | `router/mic.ts:145-173` | `router/rtmp.ts:73-109` |
| 세션 저장 | In-memory Map (`sessionRuntimeStore.ts`), 다중 세션 | 모듈 전역변수, 단일 세션 |

### mic/stop 시퀀스 (`router/mic.ts:158-166`)
1. `runtimeStore.get(sessionId)`
2. `runtime.stop()` — 파이프라인 teardown (stopFlag, 타이머 취소, NATS `drain()`/`closed()`, Google STT `stream.end()`, FFmpeg SIGTERM→2s→SIGKILL)
3. `runtimeStore.delete(sessionId)`
4. `removeSessionId()`
5. **`POST {PYTHON_HOST}/internal/sessions/stop` body `{sessionId}`** ← 크로스 서비스 "세션 종료" 신호

### 모니터링 관점 핵심
- **"세션 종료" 신호의 종점 = Python `/internal/sessions/stop`.** 세션 종료시각 기록 / 라이브→이력 전환 / 모니터 WS 종료 이벤트를 **여기서** 처리.
- **Node는 DB write 없음** → 번역 저장/세션 메타는 전부 Python(Alembic 테이블)에서 관리.

### 종료 로직 약점 (구현 시 유의)
1. **멱등성 취약**: stop 2회 호출 시 Python을 또 호출 → Python stop 핸들러를 **멱등하게** 구현 필요
2. stop 실패 시 store 정리 누락 가능(고아 런타임/프로세스)
3. Python 통지 실패해도 Node는 이미 정리 → 양쪽 상태 불일치 가능
4. 클라 WS에 명시적 close 없음
5. **비정상 종료(크래시/클라 이탈) 시 stop 미호출** → "종료시각 없는 dangling 세션" 발생 가능 → **무활동 타임아웃 기반 세션 종료 보정** 고려

---

## 4. DB 현황 진단 (greenfield)

DB는 "연동된 적 없는" 상태:
- **Postgres 컨테이너가 compose에 없음** (`docker-compose.prod.yml`에 postgres 서비스 부재)
- Python `src/database/pool.py` = **깨진 스텁** (`from services.python.src.config import postgres_host...` — 존재하지 않는 경로/심볼 import, config는 `get_postgres_config()` dict만 제공)
- `src/repository/` = 빈 폴더
- Alembic/SQL 스키마/마이그레이션 전무 (deps에 `asyncpg`만)
- `config.py`의 `get_postgres_config()`는 `require_env`라 **POSTGRES_* 없으면 기동 실패**

### Node DB 코드 = 죽은 코드 (폐기 대상)
import 체인 추적 결과 `index.ts → app.ts → 라우터들`은 DB를 import하지 않음.
- `db/pool.ts`를 import하는 곳은 `db/repository/auth.ts` 하나뿐
- 그 `auth.ts`를 import하는 곳은 **0건** → 앱 실행 경로에서 절대 로드 안 됨
- 로그인 기능 시도 잔재. **삭제해도 다른 곳 수정 불필요** (`db/pool.ts`, `db/repository/auth.ts`, `db/sql/churches.ts` 한 세트)

---

## 5. 데이터 모델 (Phase 3에서 Alembic 마이그레이션)

### `app.translations`
| 컬럼 | 설명 |
|------|------|
| id (PK) | 자동 증가 |
| session_id | 세션 식별 (인덱스) |
| segment_id, sequence | 순서 정렬 |
| source_text | 원문 (마스킹됨) |
| translated_text | 번역문 (마스킹됨) |
| source_lang, target_lang | 언어쌍 |
| confidence | STT 신뢰도 (옵션) |
| created_at | 저장 시각 (인덱스, 정렬/기간조회/보존) |

### 세션 메타 테이블 (예: `app.sessions`)
- session_id(PK), started_at, ended_at, source_lang, target_lang, 건수 등
- `ended_at`은 Python `/internal/sessions/stop`에서 기록

권장: `created_at` 기준 **일 단위 파티셔닝** → 30일 지난 파티션 DROP으로 보존 처리(대량 DELETE 회피).

---

## 6. 마스킹 정책

- **위치**: 저장 직전(INSERT) + 라이브 broadcast 직전 — 공용 마스킹 유틸 공유
- **방식**: mask-at-write (DB에 원본 민감정보 미저장, 가장 안전). 원문+번역문 **양쪽** 적용
- **대상 패턴**(정규식, 한국어 서비스): 주민등록번호, 휴대폰번호, 이메일, 카드번호. (이름/계좌는 오탐 위험 → 신중)
- 위치: `services/python/src` 공용 유틸 → `pusher.py` 경로에서 호출

---

## 7. Phase별 작업 명세

### Phase 1 — PostgreSQL 컨테이너 추가 (인프라)
> **[Phase 0 확정]** prod 기동은 `docker-compose.prod.yml` **단독**이다(base 없음, §8-1). 따라서 `postgres` 서비스·`pg-data` 볼륨·`python.depends_on`은 **반드시 `docker-compose.prod.yml`에 직접 추가**한다. base나 별도 오버레이를 새로 만들지 말 것.
> - 이 파일의 네트워크명은 `appnet`, 현재 `volumes:` 블록엔 `nats-data`만 있음 → `pg-data` 추가. 다른 서비스가 `env_file: .env.prod`를 쓰므로 `postgres`도 동일하게 `.env.prod` 사용(아래 제안 형태와 일치).
> - ⚠️ `Makefile up-prod`는 base를 참조해 레포 상태에서 깨져 있다(미수정). Phase 1 검증은 `docker compose -f docker-compose.prod.yml ...` **직접 실행**으로 하거나, 맥 실기동 방식 확정 후 그에 맞춰 진행.

**대상 파일**
- `docker-compose.prod.yml`: `postgres` 서비스 + `pg-data` named volume 추가, `python.depends_on`에 `postgres: condition: service_healthy` 추가
- `.env.prod`(**gitignore → 서버 수동**): `POSTGRES_HOST=postgres`(⚠️ 서비스명, localhost 아님), `POSTGRES_PORT=5432`, `POSTGRES_USER=neemba`, `POSTGRES_PASSWORD=<강한 비번>`, `POSTGRES_DATABASE=neemba`

**postgres 서비스 제안 형태**
```yaml
volumes:
  nats-data:
  pg-data:

services:
  postgres:
    image: postgres:16-alpine
    container_name: postgres
    env_file: [.env.prod]
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DATABASE}
    expose: ["5432"]            # 내부 전용 (외부 노출 비권장)
    volumes:
      - pg-data:/var/lib/postgresql/data
    networks: [appnet]
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DATABASE}"]
      interval: 10s
      timeout: 3s
      retries: 6
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
```

**결정 사항**: 이미지 버전(16-alpine 권장), 외부 포트 노출 여부(내부 전용 권장), 계정명 통일(`neemba`).

**완료 기준**: `postgres` healthy → `pg_isready` 통과 → python 컨테이너에서 DB 도달 → 재기동 후 `pg-data` 영속 확인.

**[Phase 1 구현 메모 — 2026-06-01]**
- 위 제안 형태 그대로 `docker-compose.prod.yml`에 반영(파일 스타일에 맞춰 `env_file`/`expose`/`logging`은 확장 표기). `volumes:`에 `pg-data` 추가, `python`에 `depends_on: postgres(condition: service_healthy)` 추가.
- ⚠️ **`POSTGRES_DB: ${POSTGRES_DATABASE}` 치환 주의**: postgres 이미지는 `POSTGRES_DB`를 읽지만 `.env.prod`/`config.py`는 `POSTGRES_DATABASE`를 쓴다(확인: `services/python/src/config.py` `get_postgres_config()`). 그래서 `environment:`에서 `POSTGRES_DB ← ${POSTGRES_DATABASE}`로 브리지함. 이 `${}` 치환은 **compose 호출 시 `--env-file .env.prod`가 있어야** 해소된다(Makefile `up-prod`는 이미 `--env-file .env.prod` 전달 → 정상). `--env-file` 없이 bare `docker compose -f docker-compose.prod.yml up`으로 띄우면 `POSTGRES_DB`가 공란이 되고, postgres가 DB명을 `POSTGRES_USER`(=neemba)로 기본 생성 → 우연히 같은 이름이라 "되는 것처럼" 보이지만 user/db명이 달라지면 깨짐. **검증·기동 시 반드시 `--env-file .env.prod` 사용**(Phase 0 미해결 TODO와 연결: prod 실기동 방식 확정 필요).
- `healthcheck`는 `pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DATABASE}`로 `$$`(런타임 컨테이너 셸 확장)를 써서 compose config-time 치환에 의존하지 않게 함(컨테이너 env엔 env_file로 주입됨).
- 검증 한계: 이 컨테이너엔 실 시크릿이 없어 실제 `up`/`pg_isready`/asyncpg/영속은 미수행. 더미 `.env.prod`(가짜값, gitignore)로 `config` 파싱만 검증 후 즉시 삭제함. **실 검증은 맥/실서버에서 `make up-prod`(또는 `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d`)로 수행.**

**[Phase 1 런타임 검증 — 2026-06-01, iOS 클라우드 컨테이너]**
- 컨테이너에 docker daemon이 미기동 상태(`/var/run/docker.sock` 부재)였으나 root 권한으로 `dockerd` 직접 기동 가능(overlayfs, cgroup v1). 이미지 pull은 Docker Hub 매니페스트는 도달하나 **블롭(`production.cloudfront.docker.com`)이 403 Forbidden으로 차단** → `mirror.gcr.io/library/postgres:16-alpine`로 우회 pull 성공, `postgres:16-alpine`로 태깅하여 compose 무수정 사용.
- 더미 `.env.prod`(`POSTGRES_USER=neemba`, `POSTGRES_DATABASE=neemba_monitor`로 **USER≠DB명**) + `--env-file`로 postgres 단독 기동 → **healthy / `pg_isready` accepting / `neemba_monitor` DB 생성(USER명 아님) / down(−v 없이)→up 후 마커 영속** 모두 통과.
- ⚠️ **`--env-file` 함정의 실제 거동은 위 메모 예측과 다름**(런타임으로 확정): bare `up`(--env-file 없이) 시 `environment:`의 `${POSTGRES_USER/PASSWORD/DB}`가 전부 빈 문자열 `""`로 치환되고, compose에서 `environment:`는 `env_file:`을 **덮어쓰므로** `.env.prod`의 값(neemba/dummy/neemba_monitor)이 무시되어 컨테이너 env가 공란이 된다. 결과는 "DB가 USER명으로 잘못 생성"이 아니라 **`POSTGRES_PASSWORD` 공란 → postgres `initdb` 거부("Database is uninitialized and superuser password is not specified") → 컨테이너 crash loop(unhealthy)**. 즉 함정은 실재하되 실패는 조용한 오생성이 아니라 **요란한 기동 실패**다. 결론은 동일: **기동·검증 시 반드시 `--env-file .env.prod`**.

### Phase 2 — Python DB 배선
- `src/database/pool.py` 정상화(깨진 import 수정), FastAPI **lifespan에서 풀 생성/종료**, DI 주입
- `get_postgres_config()` 연결, POSTGRES_* 로딩 확인

### Phase 3 — Alembic 도입 + 스키마
- deps에 `alembic` + 마이그레이션용 sync 드라이버 `psycopg` 추가
- `alembic init`, `env.py`에서 DATABASE_URL(env) 읽기
- 첫 마이그레이션: `app.translations` + `app.sessions`(인덱스, 일 파티션 선택)
- 컨테이너 기동 엔트리포인트에 `alembic upgrade head`

### Phase 4 — 저장 + 마스킹 + 모니터 WS
- 공용 마스킹 유틸
- `pusher.py` 경로에서 원문+번역문 마스킹 후 **비동기 INSERT**(번역 지연 영향 최소화, fire-and-forget/배치)
- `TranslationResultDto` 보강(source_text/source_lang/target_lang/sequence/segment_id/confidence)
- **모니터 전용 WS**(`/ws/monitor?sessionId=`) — 풀 페이로드 broadcast
- Python `/internal/sessions/stop`을 **멱등화** + `ended_at` 기록 + 모니터 WS 종료 이벤트

### Phase 5 — 이력 조회 API
- `GET /api/monitor/sessions` — 세션 목록
- `GET /api/monitor/sessions/{id}/translations?cursor=&limit=` — 페어, sequence 정렬, 커서 페이지네이션
- `GET /api/monitor/translations?lang=&from=&to=&q=` — 기간/언어/키워드 검색

### Phase 6 — `/monitor` 프론트 페이지
- 경량 정적 페이지(별도 SPA 불필요), nginx `/monitor` 경로 서빙
- 세션 목록 → 상세(원문↔번역 2열 표) → 라이브(WS)/이력(API) 토글 + 검색/기간 필터

### Phase 7 — 접근 제어 + 보존
- nginx Basic Auth 또는 IP 화이트리스트 (민감정보 노출 방지, 필수)
- **30일 보존 자동화**: 파티션 DROP 또는 스케줄 DELETE
- (선택) dangling 세션 무활동 타임아웃 보정, 물리 백업(pg_dump)

**[Phase 7 구현 확정]** 인증=**Basic Auth**(IP 화이트리스트는 모바일/클라우드 IP 가변→취약), 보존=**스케줄 DELETE 사이드카**(스키마 무변경·저위험, 파티셔닝보다 가벼움).

**htpasswd 생성·배치(레포에 커밋 금지)** — 서버/맥에서 수동으로 1회:
```sh
# bcrypt 해시(-B), monitor 사용자 1명. 파일은 node/python과 동일한 시크릿 디렉터리에 둔다.
sudo mkdir -p /var/lib/neemba/secrets
htpasswd -B -c /var/lib/neemba/secrets/monitor.htpasswd monitor   # 비번 입력
sudo chmod 640 /var/lib/neemba/secrets/monitor.htpasswd
# htpasswd가 없으면(예: alpine): docker run --rm httpd:alpine htpasswd -nbB monitor '<pw>' > ... 로 한 줄 생성
```
- nginx는 `auth_basic_user_file /var/lib/neemba/secrets/monitor.htpasswd`로 참조하고, compose의 `nginx` 서비스에 `/var/lib/neemba/secrets:/var/lib/neemba/secrets:ro` 마운트가 추가돼 있다(node/python과 동일 패턴). 보호 위치: `/monitor`·`/api/monitor/`·`/ws/monitor` 3곳(prod+dev conf 양쪽). 비-monitor 경로(`/api/`→node·`/ws`·`/health`·acme·`/`)는 무인증 유지.
- **WS+Basic Auth**: 브라우저 `new WebSocket()`은 Authorization 헤더를 직접 못 싣지만, `/monitor` 페이지 로드 시 Basic 챌린지를 통과하면서 **동일 출처 캐시 자격증명이 후속 same-origin WS 업그레이드 핸드셰이크에 자동 첨부**된다(Chrome/Firefox/Safari 문서화 동작) → 프론트(`app.js`) **무수정**. nginx가 WS 업그레이드에 Basic 인증을 적용·통과시킴은 런타임 입증(헤더 있으면 101, 없으면 401). **폴백**: 자격증명을 전달하지 않는 클라이언트를 만나면 `/ws/monitor`만 `allow/deny` IP 화이트리스트로 전환(나머지 둘은 Basic 유지).

**보존 사이드카** — `infra/postgres/retention.sh`(alpine 기반 `postgres:16-alpine` + psql while-loop, certbot/reloader 사이드카 idiom):
- `DELETE FROM app.translations WHERE created_at < now() - interval '30 days'`
- `DELETE FROM app.sessions WHERE ended_at IS NOT NULL AND ended_at < now() - interval '30 days'` — **라이브(ended_at IS NULL) 세션은 나이 무관 보호**, 종료된 세션만 30일 경과 시 삭제. 외래키 없어 순서 무관, 재실행 멱등.
- `to_regclass()` 가드로 스키마 생성(python alembic) 전이라도 에러 아닌 no-op. 튜너블: `RETENTION_DAYS`(기본 30)·`RETENTION_SLEEP_INTERVAL`(기본 24h)·`RETENTION_ONESHOT`(테스트용 1회 실행). compose `pg-retention` 서비스가 `postgres` healthy 후 기동.

**라이브(실환경) 검증 런북** → `docs/phase7-live-verification-runbook.md`. 클라우드 컨테이너 독립 검증(더미·스텁)이 입증 못한 부분 — ⭐**브라우저 WS 자격증명 자동첨부**(결정 #2 핵심 전제)·실 htpasswd(bcrypt)·실 인증서/시크릿 prod 전체 기동·pg-retention 24h 스케줄 — 을 맥/실서버 + 실제 브라우저(Chrome/FF/Safari)에서 사람이 따라 실행하는 체크리스트. 4번(브라우저 WS) 실패 시 `/ws/monitor` IP 화이트 폴백 절차 포함.

---

## 8. 미해결 선결 이슈 (구현 전 확인)

1. **base `docker-compose.yml` 부재** — `Makefile`은 `up-prod`에서 `-f docker-compose.yml -f docker-compose.prod.yml`(base+오버레이)를 기대하나 레포엔 base 파일이 없고 `docker-compose.prod.yml`이 단독 완결형.
   - **(해소: Phase 0 조사, 2026-06-01)** 레포(이 컨테이너) 커밋 기준으로 확정:
     - 추적되는 compose 파일은 **`docker-compose.prod.yml` 단 하나**. base `docker-compose.yml`은 추적·워킹트리·`.gitignore` 어디에도 없음(= gitignore된 게 아니라 진짜로 레포에 존재하지 않음).
     - `docker-compose.prod.yml`은 **self-contained**: `name: neemba` + `networks.appnet` + `volumes.nats-data` + 8개 서비스(nginx/rtmp/reloader/certbot/node/python/nats/nats-box) 전부 자체 정의. `extends`나 base 참조 **없음**. → `docker compose -f docker-compose.prod.yml config --services` **exit 0**(8개 서비스 정상 파싱, YAML 구조 유효).
     - `Makefile`의 `PROD?=-f docker-compose.yml -f docker-compose.prod.yml` 그대로 실행 시 **exit 1, `open docker-compose.yml: no such file or directory`** → **레포 커밋 상태에서는 `make up-prod`가 동작 불가**(Makefile↔실제 파일 불일치 = 사실로 확정).
   - **(부분 해소: Phase 1 런타임 검증, 2026-06-01)** `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres` 경로가 iOS 클라우드 컨테이너에서 **실기동·검증 통과**(healthy/pg_isready/DB브리지/영속) → prod 기동을 이 직접 compose 호출로 하는 것이 동작함을 실증. **단 "맥에서 prod가 실제로 어떤 방식으로 떠 있는지" 확정은 iOS 컨테이너에서 알 수 없어 TODO 유지.**
   - **잔여 TODO(맥/실서버에서 사람이 확인):** 실제 prod가 어떻게 떠 있는지 — (a) `make up-prod` 대신 `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d` 직접 실행, (b) 맥 로컬에 untracked `docker-compose.yml`(base)가 별도로 존재, (c) Makefile을 로컬에서 수정해 사용 — 중 무엇인지 1개로 확정. base `docker-compose.yml`은 `.gitignore` 대상이 **아니므로**, 맥에 존재한다면 단지 commit 안 된 상태일 뿐(존재 여부 직접 확인 필요).
   - **Makefile 정합성 수정은 Phase 0 범위 밖(별도 Phase 권고).** Phase 1 명세는 `docker-compose.prod.yml` 단독 기준으로 작성되어 있고 그대로 유효함.
2. `.env.prod`, `docker-compose.dev/stage.yml`은 `.gitignore` 처리 → 로컬에만 존재. POSTGRES_* 변수는 서버에서 수동 추가 필요.
   - **(구조 해소: Phase 1 런타임 검증, 2026-06-01)** 필요한 5개 변수(`POSTGRES_HOST=postgres`/`POSTGRES_PORT`/`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DATABASE`)만으로 postgres가 정상 기동·DB 생성됨을 더미값으로 실증. **실 비번/값 주입은 여전히 서버·맥에서 수동(커밋·채팅 금지) — 운영 비밀 자체는 미해결.**
3. Node/Python 계정 정합성: 죽은 Node 코드는 `node_auth`, config 기본은 `neemba` → **`neemba`로 통일** (Node DB 폐기로 충돌 없음).
   - **(해소: Phase 1 런타임 검증, 2026-06-01)** `POSTGRES_USER=neemba`로 postgres 기동·`pg_isready -U neemba`·`psql -U neemba` 정상 동작 확인. `neemba` 계정명으로 문제 없음.

---

## 9. 구현 진행 방식 (권장)

iOS/모바일 + 멀티데이 작업 특성상:
- 이 문서를 핸드오프로 삼아 **Phase별 새 세션 + 별도 브랜치/PR**로 진행
- 각 세션 시작: *"`docs/monitoring-plan.md` 읽고 Phase N 구현해줘"*
- 현재 작업 브랜치: `claude/neemba-server-monitoring-plan-1GcsS`

---

## 10. 진행 상황 트래커 (세션 간 핸드오프) ⭐

> **세션끼리는 메모리를 공유하지 않는다. git에 커밋된 이 문서만이 유일한 공유 채널이다.**
> **규칙: 모든 구현 세션은 작업/검증을 마치면 이 표를 갱신하고 반드시 commit & push 한다.**
> 상태값: `미착수` / `진행중` / `구현완료(미검증)` / `검증완료` / `보류`

| Phase | 작업 | 상태 | 검증 방법/결과 | 담당 브랜치·PR | 비고 |
|-------|------|------|----------------|----------------|------|
| 0 | 선결 이슈 확정(base compose) | 검증완료(조사) | 레포 커밋 기준 확정: base `docker-compose.yml` 없음 / `docker-compose.prod.yml` self-contained(`config --services` exit 0) / Makefile `up-prod` 경로는 exit 1(`no such file`)로 동작 불가. §8-1 참조 | `claude/monitor-phase0-compose-baseline-C6XEo` | 코드/설정 무수정(문서만). 맥 실기동 방식 확인 + Makefile 정합성 수정은 별도 Phase 권고 |
| 1 | Postgres 컨테이너 | 검증완료(런타임) | iOS 클라우드 컨테이너에서 `dockerd` 직접 기동 후 더미 `.env.prod`로 **postgres 단독 실기동 검증(4/4 통과)**: (a) `ps`→**healthy**, (b) `pg_isready -U neemba -d neemba_monitor`→**accepting connections (exit 0)**, (c) DB명 브리지→`psql -lqt`에 **`neemba_monitor` 생성**(USER명 `neemba`로 잘못 생기지 **않음**)=`POSTGRES_DB←${POSTGRES_DATABASE}` 치환 성공, (e) **영속**: 마커 테이블 insert→`down`(−v 없이)→`up`→마커·DB 유지 확인. (d) **`--env-file` 함정 실재 재현**: bare `up`(--env-file 없이)→`environment:`의 `${}`가 전부 `""`로 치환되고 이 빈값이 `env_file`을 덮어써 **POSTGRES_PASSWORD 공란→initdb 거부→crash loop(unhealthy)**. ⚠️asyncpg 접속·python 컨테이너 도달은 실시크릿/풀 코드 없어 Phase 2로 이월 | `claude/monitor-phase0-compose-baseline-C6XEo` | 이미지 pull: Docker Hub 블롭(`production.cloudfront.docker.com`) **403 차단** → `mirror.gcr.io/library/postgres:16-alpine`로 우회 pull 후 `postgres:16-alpine` 태깅. `POSTGRES_DB`는 `${POSTGRES_DATABASE}` 치환→**`--env-file .env.prod` 필수** |
| 2 | Python DB 배선(pool/lifespan) | 검증완료(런타임) | **A. 정적**: `python -m py_compile main.py src/database/pool.py`→**OK**(둘 다). pool.py 깨진 import(`from services.python.src.config import postgres_host…` 존재X 심볼) 제거→`from src.config import get_postgres_config` 로 교체. **B. 런타임 스모크(핵심)**: iOS 클라우드 컨테이너에서 `dockerd` 기동→`mirror.gcr.io/library/postgres:16-alpine` 우회 pull→`postgres:16-alpine` 태깅→`docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres`(더미값)→**healthy**. 스모크 스크립트가 `get_postgres_config()`→`Db.create_pool()`(asyncpg, port int 캐스팅)→`SELECT 1`→`Db.close()` 실행: **`SELECT 1 => 1`, `SELECT 1 == 1 OK`, `pool closed OK`** 전부 통과. **접속 방식**: 컨테이너 내부 pip가 PyPI 도달 불가(`No matching distribution`)라 in-network 컨테이너 스모크 실패→**호스트 python(asyncpg 설치됨)에서 compose postgres 컨테이너의 브리지 IP(172.18.0.2)로 직접 접속**(`POSTGRES_HOST`만 IP로 오버라이드; 서비스명 `postgres` DNS는 in-network 전용). 정리: 스모크 스크립트 rm, `compose down -v`(더미 볼륨 제거), 더미 `.env.prod` rm | `claude/monitor-phase2-python-db-wiring-nj6YB` | ⚠️full-stack 부팅은 미검증(의도): lifespan이 NATS/DeepL/WS + 신규 POSTGRES_* 전부 require_env→실시크릿 없으면 부팅 실패. lifespan 풀 배선은 startup `app.state.db_pool` 생성 / finally `db.close()` 추가 + `get_db_pool(request)` DI 헬퍼. **Phase 2 브랜치는 phase0(C6XEo)에서 분기**(compose+plan 포함 필요) |
| 3 | Alembic + 스키마 | 검증완료(런타임) | **A. 정적**: `pyproject.toml`에 `alembic`+`psycopg[binary]` 추가→`uv lock` 재생성→`uv lock --check` **exit 0**(`alembic 1.18.4 / psycopg 3.3.4 / sqlalchemy 2.0.50` 추가됨). `python -m py_compile main.py src/config.py src/database/pool.py migrations/env.py migrations/versions/0001_initial_monitor_schema.py`→**exit 0**. **B. 런타임 마이그레이션 스모크(핵심)**: dockerd 기동→`mirror.gcr.io/library/postgres:16-alpine` 우회 pull·태깅→더미 `.env.prod`→`docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres`→**healthy(t=8s)**. 호스트에 `alembic psycopg[binary] python-dotenv` 설치 후 컨테이너 브리지 IP(`172.18.0.2`)로 `POSTGRES_HOST`만 오버라이드→**`alembic upgrade head` → `Running upgrade -> 0001_initial` exit 0**. 테이블 검증: `\dt app.*`→`app.sessions`+`app.translations`, `SELECT to_regclass('app.translations'),to_regclass('app.sessions')`→**둘 다 non-null**, 인덱스 `ix_translations_session_id`+`ix_translations_created_at` 생성 확인. **down/up 왕복**: `alembic downgrade base`(테이블·`app`스키마 제거, `app`스키마 count=0)→`alembic upgrade head`(테이블 재생성 non-null) 모두 exit 0. **오프라인**(`upgrade head --sql`)도 `CREATE SCHEMA/TABLE/INDEX app.*` SQL 정상 생성. **alembic_version은 `public`에 위치**(downgrade의 `DROP SCHEMA app CASCADE`와 충돌 회피). 정리: 스모크 후 `compose down -v`+더미 `.env.prod` rm | `claude/monitor-phase3-alembic-schema-0fRe8` | sync 드라이버 `psycopg[binary]`(런타임은 asyncpg 유지). DATABASE_URL=POSTGRES_*에서 조립(`get_postgres_sync_url()`, 신규 시크릿 無). 파티셔닝은 Phase 7로 이월(단순 인덱스 테이블). 엔트리포인트=`entrypoint.sh`(`alembic upgrade head`→`exec "$@"`). ⚠️호스트 alembic은 PyPI 도달 OK(컨테이너 내부 pip는 불가)→호스트 직결로 검증 |
| 4 | 저장+마스킹+모니터 WS | 검증완료(런타임) | **A. 정적**: 변경/신규 파일 `py_compile` exit 0(main/pusher/monitor/kss_separator/translation_repository/dto/masking). 신규 마스킹·WS 파일 `ruff check` **All checks passed**(`src/masking/*`, `src/ws/monitor.py`, `pusher.py`, `translation_repository.py`, `dto`). main/kss_separator는 base 대비 **신규 ruff 위반 0**(기존 F841/SIM110 등만 잔존). `grep -rn "services.python.src"`→앱코드 0건(src/test.py만, Phase2부터 알려진 잔재). deps 무추가→`uv lock --check` **exit 0**. **B. 단위(pytest 27 passed)**: 마스킹 패턴별 양/음성(RRN/휴대폰/이메일/카드 + 날짜·유선·짧은숫자 등 음성), stop 멱등성(fake asyncpg pool로 `end_session` 1회/2회/5회 호출→ended_at 1회만, 카운트 1회만 기록). **C. 런타임 스모크(핵심)**: dockerd→`mirror.gcr.io/library/postgres:16-alpine` 우회 pull·태깅→더미 `.env.prod`→`compose --env-file .env.prod up -d postgres`→**healthy(t=8s)**→호스트에서 컨테이너 브리지 IP(`172.18.0.2`)로 `alembic upgrade head`. 실제 `Pusher.push_to_client`(capture 경로)+실제 `MonitorHub`+`/ws/monitor` 라우트를 Starlette TestClient로 구동: ① 모니터 WS가 **마스킹된 풀 페이로드 수신**(RRN/PHONE/EMAIL 전부 치환, 원문+번역문 양쪽), ② `psql SELECT app.translations`→**마스킹된 1행**(원문 PII 미저장; `~'[0-9]{6}' OR ~'@'` 카운트=0), ③ `app.sessions` started+ended, `translation_count=1`, ④ stop 2회 호출→1회차 `ended=True/count=1`, 2회차 `ended=False/count=1`(UPDATE…WHERE ended_at IS NULL가 0행 매칭=멱등). **D. 회귀**: `alembic downgrade base`→`upgrade head` 왕복 exit 0, Phase2/3 자산 py_compile OK. 정리: 스모크 rm, `compose down -v`, 더미 `.env.prod` rm | `claude/monitor-phase4-impl-wUu3v` | base=phase3(`0fRe8`)에서 분기. **sessions 행 생성 결정=(b)+(a) 하이브리드**: 주경로는 `/internal/sessions/start`에서 `ON CONFLICT DO NOTHING` upsert(started_at/lang 확보), 첫 INSERT에서도 세션당 1회 안전망 upsert(start 신호 유실 대비 ended_at 타깃 보장). INSERT는 **fire-and-forget asyncio task + 전역 try/except**(번역/broadcast 비차단). 번역 target은 'en-US' 유지(기존 동작 불변)→target_lang도 'en-US'로 정직 기록. ⚠️Node 무수정, full app boot 미시도(NATS/DeepL/WS require_env) |
| 5 | 이력 조회 API | 검증완료(런타임) | **A.정적**: 신규/변경 파일 `py_compile` exit 0; 신규 파일(`monitor_query_repository.py`+테스트) `ruff check` **All checks passed**; `main.py`는 phase4 base 대비 **신규 ruff 위반 0**(B008은 FastAPI `Depends`/`Query` 관례라 `pyproject` `flake8-bugbear.extend-immutable-calls`로 화이트리스트→base와 위반 히스토그램 동일 `2 F841 / 1 I001`); `uv lock --check` **exit 0**(deps 무추가, pyproject 변경은 ruff 설정뿐); `grep services.python.src`→`src/test.py` 1건만(알려진 잔재). **B.단위(pytest 29 passed, 전체 56 passed)**: clamp_limit 경계(None→default/0·음수→1/초과→cap), clamp_offset, 정수커서·검색커서 인코딩/디코딩 라운드트립+오류시 ValueError, list_sessions/session_translations/search SQL·바인딩파라미터 검증(커서 경계 `id > $n`, lang `source OR target`, from/to, q는 **바인딩 ILIKE**(SQL injection 프로브 포함), 콤보 파라미터 순서, 빈 결과·다음커서 None). **C.런타임 스모크(핵심)**: dockerd→`mirror.gcr.io/library/postgres:16-alpine` 우회 pull·태깅→더미 `.env.prod`→`compose --env-file .env.prod up -d postgres`→healthy(t=7s)→호스트에서 브리지 IP `172.18.0.2`로 `alembic upgrade head`. 3세션(라이브 1 ended_at NULL 포함)+10translation(sequence 일부 NULL·언어 ko/ja/en-US 혼합·created_at 4일 분산) 시드 후 **실제 라우트 3개를 Starlette TestClient(앱 단독 lifespan, pool 동일루프)로 호출**: ① sessions 정렬(started_at DESC=B,C,A)·live 플래그·offset 페이지네이션(limit=2→nextOffset=2→끝 None)·limit 클램프(9999→200, 0→1); ② sessions/{id}/translations **id ASC 안정 커서**로 limit=2씩 끝까지 순회 시 **중복·누락 0**(5행 모두), sequence NULL 보존, 잘못된 커서→422, 빈 세션→200+[]; ③ search lang(=source OR target, ja→5행 sess-B/C)·from/to 범위(06-02→2행)·q는 **마스킹된 텍스트** ILIKE(`hello`→4, `[PHONE]` 플레이스홀더→1)·콤보(ko+hello→3)·커서 순회 10행 무중복·잘못된 커서/from→422·무매치→200+[]; 직렬화 createdAt/startedAt **ISO8601 문자열**. **D.회귀**: `alembic downgrade base`→`upgrade head` 왕복 exit 0, Phase2/3/4 자산 `py_compile` exit 0. 정리: 스모크 rm·`compose down -v`·더미 `.env.prod` rm. **[독립 검증 2026-06-04 재확인 PASS]** A~D 전부 재현(py_compile/ruff신규0·B008 화이트리스트 load-bearing 확인/uv.lock 무변경/grep 1건/pytest 56/실DB 라우트 25체크 ALL PASS·PII교차0·동일 created_at 키셋 tie-break 무중복/alembic 왕복) — 결함 0 | `claude/monitor-phase5-history-api-AB5cx` | base=phase4(`wUu3v`)에서 분기. read-only 순수 async 함수(`src/repository/implementation/monitor_query_repository.py`)+`main.py` 3 라우트(`@app.get`, `Depends(get_db_pool)`). **결정**: sessions=offset 페이지네이션, session_translations=PK `id` 키셋(sequence nullable·비유니크), search=`(created_at,id)` 복합 키셋(opaque base64 토큰). **lang은 source_lang OR target_lang 양쪽 매칭**. limit 초과/음수는 안전 클램프, 잘못된 cursor/from·to는 422. deps 무추가 |
| 6 | /monitor 페이지 | 검증완료(런타임) | **A.정적**: prod `infra/nginx/nginx.conf`+dev `infra/nginx/dev/nginx.conf` 각각 `nginx -t`(임시 nginx:1.27-alpine 컨테이너에 conf만 마운트; prod는 self-signed cert stub을 `/etc/letsencrypt/live/neemba.app/`에 마운트, 둘 다 `--add-host node/python`로 upstream DNS 해소)→**syntax ok·test successful**. diff는 기존 라우팅(`/api/`→node, `/ws`→python, `/health`, acme-challenge, `/`) **무삭제·추가만**(prod+dev 각 3블록: `/api/monitor/`→python, `/monitor` static, http에 `include mime.types`). HTML/JS **빌드 없음**(순수 정적)·**외부 CDN 0**(grep https?://·cdn·unpkg 등 0건, 오프라인 동작)·**innerHTML/eval/document.write 0**(textContent 전용 XSS 방지). `node --check app.js`→OK, JS가 참조하는 24개 DOM id 전부 HTML에 존재. **B.런타임 스모크(핵심)**: dockerd→`mirror.gcr.io/library/postgres:16-alpine` 우회 pull·태깅→더미 `.env.prod`→`compose --env-file .env.prod up -d postgres`→healthy(t=7s)→호스트 브리지 IP(`172.18.0.2`)로 `alembic upgrade head`. 3세션(B 라이브 ended_at NULL)+10translation(sequence 일부 NULL·ko/ja/en-US·마스킹/플레이스홀더 텍스트, raw PII 0) 시드. 실제 Phase5 라우트 3개+Phase4 `/ws/monitor`를 **main.py에서 그대로 가져온** 얇은 러너(lifespan만 pool+MonitorHub로 축소, 백엔드 로직 무수정)로 호스트 uvicorn 기동→**dev conf nginx 컨테이너**(`--add-host python:host-gateway`)로 교차검증: ① `/monitor/`→**200 text/html**·`app.js`→**application/javascript**·`style.css`→**text/css**(mime.types 추가로 교정)·deep-link `/monitor/x`→index.html 폴백; ② `/api/monitor/sessions`→**python JSON 200**(3세션·started_at DESC=[C,B,A]·live 플래그·nextOffset)·search(lang=ja→3행)·session translations(limit=2→nextCursor=2) 전부 nginx 경유 정상; ③ **음성검증** `/api/something-node`→**502(죽은 node upstream)** =비-monitor `/api/`는 python 아닌 node로 라우팅됨 입증(python이면 404였을 것); ④ **WS** `/ws/monitor?sessionId=sess-B`→nginx 경유 **101 Switching Protocols**(연결 유지), sessionId 없으면 **거부(403; 핸들러가 accept 전 close→uvicorn이 403으로 표면화)**. **DOM-unit**: jsdom 부재→최소 DOM shim(node)으로 실제 시드 JSON 주입해 app.js 실행→세션목록 **3행·DESC순·LIVE 배지(sess-B만)·lang/건수/시각 textContent·dataset.sessionId·more 버튼 hidden(nextOffset null)** 전부 정확 렌더. **C.회귀**: git diff 범위=`infra/nginx`(conf 2개)+`docs`+신규 `infra/nginx/html/`뿐, **services/python·node 무변경**; Phase 5 `pytest` **56 passed**; 기존 nginx 라우팅 무회귀(nginx -t·502 음성검증). 정리: nginx-smoke·uvicorn 종료, `compose down -v`, 더미 `.env.prod`·러너·스모크 스크립트·cert stub·pyc/.pytest_cache 전부 rm. **[독립 검증 2026-06-04 재확인 PASS]** A~C 전부 재현(prod/dev `nginx -t` 양쪽 syntax ok·diff 추가만(기존 5/5블록 무삭제)·CDN0/위험sink0/`node --check`/24 DOM id 전부 존재; 실DB B스모크: `/monitor/`→200 html·`.css`→text/css·`.js`→application/javascript·deep-link 폴백 / sessions started_at DESC·live 플래그·camelCase+ISO8601Z·nextOffset / session_translations nextCursor=정수id / search lang=source OR target / **음성검증 `/api/whatever`→502 죽은node**(python 404 아님→`/api/monitor/` longer-prefix 우선 입증) / WS `?sessionId=`→**101**·미지정→**403** / **in-process `hub.broadcast`→클라가 마스킹 페이로드 수신·raw PII 0** / DOM-shim 렌더 3행·DESC·LIVE배지(live만)·textContent·more hidden 전부 정확; pytest **56 passed**) — 결함 0(시드 의존 카운트만 본 세션 시드 기준으로 상이, 계약 동작은 동일) | `claude/monitor-phase6-frontend-kUv85` | base=phase5(`AB5cx`). 경량 바닐라 HTML/CSS/JS(빌드툴·SPA·CDN 0). **함정**: ① `/api/`가 node로 프록시됨→모니터 API는 python:8000이라 **`/api/monitor/` 블록을 prod+dev 양쪽에** 추가(`/api/`보다 구체적 prefix라 우선). ② 두 conf 모두 `include mime.types` 부재→정적 .css가 text/plain으로 나가 브라우저가 스타일시트 거부→**http 블록에 `include /etc/nginx/mime.types` 추가**(필수 교정). ③ html 디렉터리(`infra/nginx/html/`) 신규(prod compose가 이미 `/var/www/html`로 마운트). **Phase 7 주의**: 이 페이지·API 무인증→nginx Basic Auth/IP 화이트리스트 필수 |
| 7 | 접근제어 + 30일 보존 | 검증완료(런타임) | **A.정적**: prod `infra/nginx/nginx.conf`+dev `infra/nginx/dev/nginx.conf` 각각 `nginx -t`(임시 `nginx:1.27-alpine`에 conf만 마운트; prod는 self-signed cert stub `/etc/letsencrypt/live/neemba.app/`+**htpasswd stub**(`openssl passwd -apr1` 더미)+`--add-host node/python`)→**syntax ok·test successful**(양쪽). diff는 **추가만**(비-monitor 블록·기존 라우팅 prod `/api/`→node·`/ws`·`/health`·acme·`/`, dev `/api/mic`·`/api/`→node·`/ws`·`/health`·`/` **무삭제**; 유일 `-`는 `# WebSocket → python` 주석 1줄 교체). 추가분=세 monitor 위치(`/monitor`·`/api/monitor/`·신규 `/ws/monitor`)의 `auth_basic`+`auth_basic_user_file`(prod+dev 양쪽)+compose `nginx`에 `/var/lib/neemba/secrets` 마운트+`pg-retention` 사이드카+`infra/postgres/retention.sh`. **시크릿**: `git ls-files`에 htpasswd/.pem/.env 0건, `.env.prod` gitignored, retention.sh에 하드코딩 비밀 0(`BEGIN`은 plpgsql). **B.런타임 스모크(핵심)**: dockerd→`mirror.gcr.io/library/postgres:16-alpine`+`nginx:1.27-alpine`+`alpine:3.20` 우회 pull·태깅→더미 `.env.prod`→`compose up -d postgres`→healthy(t=11s)→호스트 브리지 IP(`172.18.0.2`)로 `alembic upgrade head`. ① **인증**(dev conf nginx 컨테이너+main.py 라우트 그대로 재사용 얇은 uvicorn 러너+htpasswd stub): 무자격증명 `/monitor/`·`/monitor/app.js`·`/api/monitor/sessions`→**전부 401**, 오자격증명→**401**, 올바른 Basic→`/monitor/` **200**·`app.js` **200 application/javascript**·`/api/monitor/sessions` **JSON 200**. **WS**: `/ws/monitor?sessionId=`→무자격증명 **401**·오자격증명 **401**·올바른 Basic 헤더 **101 CONNECTED**(nginx가 WS 업그레이드에 Basic 적용·통과 입증; 브라우저는 same-origin 캐시 자격증명 자동첨부→app.js 무수정). ② **음성검증(회귀)**: 무인증 `/health`→**200**, `/api/whatever`·`/api/mic`→**502(죽은 node)**(NOT 401=인증 미누출·node 라우팅 유지), 클라 `/ws`→**500(NOT 401)**=인증 `/ws`로 미누출, `/`→403(dev static, NOT 401). ③ **보존**: 시드(sessions: 40일전 종료/10일전 종료/40일전 **라이브(ended_at NULL)**/29일전 종료 4행, translations: 40·35·31일전+29·10·1일전+라이브세션 40일전 7행)→**실제 `pg-retention` 사이드카 컨테이너**(`compose run --rm -e RETENTION_ONESHOT=1`)→`NOTICE: deleted 4 translations, 1 sessions`→translations 7→**3**(29/10/1일=전부<30d)·sessions 4→**3**(40일 종료만 삭제, **라이브 세션 생존**·29일 종료 생존). **재실행 멱등**: 2회차 `deleted 0/0`·카운트 불변(3/3). **C.회귀**: `git diff`=`infra/nginx`(conf 2)+`docker-compose.prod.yml`+신규 `infra/postgres/`+`docs`뿐 **services/node·python 무변경**; Phase 5 `pytest` **56 passed**; nginx 라우팅 무회귀(nginx -t·502 음성검증). **[독립 검증 2026-06-05 재확인 PASS·결함 0]** A~C 전부 독립 재현: A.정적=prod/dev `nginx -t` 양쪽 **syntax ok·test successful**(prod는 self-signed cert stub+`openssl passwd -apr1` htpasswd stub+`--add-host node/python` 마운트); `git diff 594d89b..HEAD`=`docker-compose.prod.yml`+`docs`+`infra/nginx`(conf 2)+신규 `infra/postgres/retention.sh`뿐 **services/node·python 무변경**(diff 추가만, 비-monitor 블록·기존 라우팅 무삭제, `/ws/monitor`가 `/ws`보다 longer-prefix); 시크릿 0(`git ls-files`에 htpasswd/.pem/.env 0건·`.env.prod` gitignored·retention.sh 하드코딩 비밀 0). B.런타임(dev conf nginx 컨테이너+main.py 라우트 재사용 얇은 uvicorn 러너+htpasswd stub `monitor:testpass` 더미): **인증**=무자격증명 `/monitor/`·`/monitor/app.js`·`/api/monitor/sessions`→**전부 401**·오자격증명→**401**·올바른 Basic→`/monitor/` **200 text/html**·`app.js` **200 application/javascript**·`/api/monitor/sessions` **JSON 200**; **WS**(websockets 클라 3케이스)=무헤더 **401**·오헤더 **401**·올바른 Authorization 헤더 **101 CONNECTED**(nginx가 WS 업그레이드에 Basic 적용·통과 입증); **음성검증**=무인증 `/health`→**200**·`/api/whatever`·`/api/mic`·`/api/sessions/stop`→**502(죽은 node, NOT 401)**·`/`→**403(NOT 401)**·`/index.html`→**404(NOT 401)**·클라 `/ws`→**101(NOT 401, 인증 미누출)**; **보존**=시드(translations 7행/sessions 4행, 40일전 라이브 세션·31일전(최근세션 하위)·29일 경계 포함)→**실제 `pg-retention` 사이드카**(`compose run --rm -e RETENTION_ONESHOT=1`)→`NOTICE: deleted 4 translations, 1 sessions`→translations 7→**3**·sessions 4→**3**(40일 종료 세션만 삭제, **41일 라이브 세션 생존**·29일 종료 생존), **멱등 2회차 `deleted 0/0`·3/3 불변**. C.회귀=services 무변경·Phase 5 `pytest` **56 passed**·`nginx -t` 양쪽 ok. **환경차 1건(결함 아님)**: 원세션은 클라 `/ws`→500 기록, 본 검증은 살아있는 얇은 러너라 `/ws`→101 — 핵심 계약(**NOT 401, 인증 미누출**)은 동일. | `claude/monitor-phase7-access-retention-qx7jB` | base=phase6(`kUv85`/594d89b). 인증=**Basic Auth**(IP화이트는 모바일/클라우드 가변IP 취약), htpasswd는 **시크릿 마운트**(레포 미커밋, 생성법 §7 문서화). 보존=**스케줄 DELETE 사이드카**(스키마 무변경·저위험). **WS 함정 처리**: same-origin 캐시 Basic 자격증명이 WS 업그레이드에 자동첨부(문서화 브라우저 동작)→**프론트 무수정**; nginx WS+Basic 101 런타임 입증; 폴백=`/ws/monitor`만 IP화이트. Node 무수정 |

### 세션 간 인계 메모 (자유 기록)
> 각 세션이 다음 세션에게 남기는 메모. 발견한 함정, 바꾼 결정, 미해결 TODO 등.

- (2026-06-01, 플랜 세션) 문서 최초 작성. 구현 시작 전. 실제 prod 키/시크릿은 채팅·커밋에 절대 포함 금지(더미로 구조검증, 실키 E2E는 맥/실서버).
- (2026-06-01, Phase 0 조사 세션, 브랜치 `claude/monitor-phase0-compose-baseline-C6XEo`) **확정 사실**: 레포에 base `docker-compose.yml` 없음(gitignore도 아님 = 진짜 부재), `docker-compose.prod.yml`은 self-contained(8서비스, `config --services` exit 0), Makefile `up-prod`는 base 참조로 레포 상태에서 exit 1(`no such file`)로 깨짐. **Phase 1 담당 주의**: postgres/pg-data/depends_on는 `docker-compose.prod.yml`에 직접 추가(네트워크 `appnet`, `env_file: .env.prod`), 검증은 `make`가 아니라 `docker compose -f docker-compose.prod.yml` 직접 실행으로. **맥에서 사람이 확인할 TODO**: (1) prod 실기동이 `make up-prod`인지 `docker compose -f docker-compose.prod.yml` 직접인지, (2) 맥 로컬에 untracked base `docker-compose.yml`이 실제로 있는지, (3) 있다면 Makefile 정합성 수정을 별도 Phase로 진행할지 결정. **코드/설정/Makefile/.env 일체 무수정**(문서만 갱신).
- (2026-06-01, **Phase 1 런타임 검증 세션**, 동일 브랜치 `claude/monitor-phase0-compose-baseline-C6XEo`) iOS 클라우드 컨테이너에서 `dockerd` 직접 기동 후 더미 `.env.prod`로 **postgres 단독 실기동 4/4 통과**(healthy / `pg_isready` accepting / `neemba_monitor` DB 브리지 성공(USER명 아님) / `down`(−v 없이)→`up` 마커 영속). 이미지 pull은 Docker Hub 블롭이 403 차단 → **`mirror.gcr.io/library/postgres:16-alpine` 우회 pull 후 `postgres:16-alpine` 태깅**. **`--env-file` 함정 실재 재현**: 빼고 띄우면 `environment:` `${}`→`""`가 `env_file`을 덮어 **PASSWORD 공란→initdb 거부→crash loop**(메모의 "USER명으로 오생성" 예측과 달리 조용한 오류가 아닌 기동 실패). 코드/compose 무수정(문서만 갱신), 더미 `.env.prod` 삭제, 컨테이너 down(볼륨 `neemba_pg-data`는 더미데이터로 잔존—사람이 `docker volume rm`). **Phase 2 주의**: ① 기동·검증 시 `--env-file .env.prod` 필수, ② asyncpg/python 컨테이너 DB 도달은 실시크릿+`pool.py` 정상화(Phase 2) 후에야 검증 가능—이번 세션 범위 밖, ③ 실서버 이미지 pull도 Docker Hub 블롭 차단 가능성 있으니 mirror/사내 레지스트리 확인 권장.
- (2026-06-02, **Phase 2 세션**, 브랜치 `claude/monitor-phase2-python-db-wiring-nj6YB`, **phase0 `C6XEo`에서 분기**) **pool.py 정상화 + lifespan 풀 배선 완료, 런타임 스모크 통과**. ⓐ `src/database/pool.py`: 깨진 `from services.python.src.config import postgres_host…`(존재X 심볼) 제거→`from src.config import get_postgres_config`. `Db` 클래스 유지(`src/test.py`가 import) + `create_pool(min_size=1,max_size=10)`/`close()` 캡슐화, **port `int()` 캐스팅**(config는 문자열 dict), 멱등 생성. ⓑ `main.py` lifespan: startup `db=Db(); app.state.db=db; app.state.db_pool=await db.create_pool()`, finally `await app.state.db.close()`(기존 task 정리와 공존), 라우트용 DI 헬퍼 `get_db_pool(request)` 추가. ⓒ **스모크 통과**: dockerd→mirror postgres→compose `--env-file .env.prod` up→`get_postgres_config→create_pool→SELECT 1(=1)→close` 전부 OK. **발견한 함정**: ① lifespan은 NATS/DeepL/WS + **신규 POSTGRES_5종**까지 전부 `require_env`→실시크릿 없으면 부팅 실패(=의도된 정책, full-stack 부팅은 이 세션 미검증). ② **컨테이너 내부 pip가 PyPI 도달 불가**(블롭 403과 별개 egress 제한)→in-network 컨테이너 스모크 불가→**호스트에서 컨테이너 브리지 IP 직결**로 우회(서비스명 DNS는 in-network 전용이라 `POSTGRES_HOST`만 IP로 오버라이드). ③ asyncpg는 host `pip install` 가능(PyPI 도달은 host에선 OK). **다음(Phase 3: Alembic+psycopg) 주의**: deps에 `alembic`+sync 드라이버 `psycopg` 추가, `env.py`에서 DATABASE_URL(env) 읽기, 첫 마이그레이션 `app.translations`+`app.sessions`, 엔트리포인트 `alembic upgrade head`. 마이그레이션 검증도 동일 포트노출 함정 있음→호스트 직결/임시 -p로 진행 권장. (pool/lifespan/config만 변경, compose 무수정, 더미 `.env.prod` rm·`down -v`로 정리)
- (2026-06-02, **Phase 2 독립 검증 세션**, 동일 브랜치 `claude/monitor-phase2-python-db-wiring-nj6YB`) **판정: 합격(검증완료·런타임 재확인)**. 코드/compose/문서 무수정(메모 1줄만). ⓐ 정적: `grep -rn "services.python.src.config" services/python`→**0건**(깨진 import 잔재 없음), `pool.py`는 `from src.config import get_postgres_config`만 사용·`asyncpg.create_pool(host/port=int()/user/password/database)`·`create_pool()/close()` 캡슐화·`pool` property 가드 확인. `main.py` lifespan: startup `app.state.db=Db(); app.state.db_pool=await db.create_pool()`, finally `await app.state.db.close()`(기존 consumer/separator task 정리와 공존), `get_db_pool(request)` DI 헬퍼 확인. `cd services/python && python -m py_compile main.py src/database/pool.py`→**exit 0**. ⓑ 런타임 스모크(핵심): `dockerd` 기동→`mirror.gcr.io/library/postgres:16-alpine` 우회 pull→`postgres:16-alpine` 태깅→더미 `.env.prod`(가짜값)→`docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres`→**healthy(t=4s)**. **실제 `pool.py` 코드**를 호출하는 스모크(`get_postgres_config()`→`Db().create_pool()`→`SELECT 1`→`close()`) 실행: config가 `postgres_port='str'`로 반환됨을 출력(=`int()` 캐스팅이 실제로 필요·동작함 입증), **`create_pool OK / SELECT 1 => 1 / SELECT 1 == 1 OK / pool closed OK / 종료 후 pool property RuntimeError`** 전부 통과. **접속 방식**: 컨테이너 내부 pip가 PyPI 도달 불가→호스트 python에 `pip install asyncpg python-dotenv` 후 postgres 컨테이너 브리지 IP(`172.18.0.2`)로 직결(`POSTGRES_HOST`만 IP 오버라이드; 서비스명 DNS는 in-network 전용). 정리: 스모크 rm, `compose down -v`, 더미 `.env.prod` rm 완료. ⚠️full-stack 부팅은 미검증(의도된 정책: lifespan이 NATS/DeepL/WS+POSTGRES_5종 전부 require_env). **결론: 트래커 Phase 2 "검증완료(런타임)"는 독립 검증으로 재확인됨(신뢰 가능).**
- (2026-06-02, **Phase 3 세션**, 브랜치 `claude/monitor-phase3-alembic-schema-0fRe8`, **phase2 `nj6YB`에서 분기**) **Alembic 도입 + 첫 스키마 마이그레이션 완료, 런타임 검증 통과**. ⓐ **deps**: `pyproject.toml`에 `alembic`+`psycopg[binary]`(psycopg3, sync; 런타임 asyncpg는 그대로) 추가→**`uv lock` 재생성·커밋**(`uv lock --check` exit 0). Dockerfile이 `uv export --frozen`을 쓰므로 lock 재생성 필수(안 하면 frozen 불일치로 빌드 깨짐). ⓑ **DATABASE_URL 결정=POSTGRES_*에서 조립**(신규 시크릿 표면 최소화): `config.py`에 `get_postgres_sync_url()` 추가→`postgresql+psycopg://user:pwd@host:port/db`(자격증명 `quote_plus` 인코딩). ⓒ **스캐폴딩**: `services/python`에서 `alembic init migrations`(ruff가 이미 `migrations` exclude). `migrations/env.py`가 `get_postgres_sync_url()`로 URL을 **코드 주입**(오프라인/온라인 양쪽), `alembic.ini`의 `sqlalchemy.url`은 **비워둠(비밀 하드코딩 없음)**. ⓓ **첫 마이그레이션 `0001_initial`**: `CREATE SCHEMA IF NOT EXISTS app` + `app.sessions`(session_id PK, started_at, ended_at nullable, source/target_lang, translation_count) + `app.translations`(id BIGINT PK 자동증가, session_id, segment_id, sequence, source_text, translated_text, source/target_lang, confidence nullable, created_at) + 인덱스 `ix_translations_session_id`/`ix_translations_created_at`. downgrade는 인덱스·테이블 drop 후 `DROP SCHEMA app CASCADE`. ⓔ **엔트리포인트 결정=`entrypoint.sh`**(`alembic -c .../alembic.ini upgrade head` → `exec "$@"`): Dockerfile runtime 스테이지에 `ENTRYPOINT`로 배선(dev uvicorn/prod gunicorn CMD는 인자로 보존). **결정·함정**: ① **파티셔닝은 Phase 3에선 안 함**→인덱스 있는 단순 테이블, 일 파티셔닝+30일 보존은 **Phase 7로 이월**(§5/§7). ② **`alembic_version`은 `public` 스키마에 둠**(version_table_schema 미설정): downgrade의 `DROP SCHEMA app CASCADE`가 version 테이블을 같이 지우면 alembic의 down 기록(DELETE)이 깨져 왕복 실패→public 유지가 robust. ③ **호스트 alembic 검증**: 컨테이너 내부 pip는 PyPI 도달 불가(Phase 2 함정)→호스트에 `alembic psycopg[binary] python-dotenv` 설치 후 postgres 컨테이너 브리지 IP로 `POSTGRES_HOST`만 오버라이드해 `upgrade head`/`downgrade base` 검증(서비스명 DNS는 in-network 전용). config.py가 top-level에서 `dotenv` import하므로 호스트에도 `python-dotenv` 필요. **검증 결과**: `upgrade head` exit 0→테이블 to_regclass 둘 다 non-null·인덱스 2개 확인, `downgrade base`→`upgrade head` 왕복 exit 0, 오프라인 `--sql`도 정상. **다음(Phase 4) 주의**: 공용 마스킹 유틸 + `pusher.py` 경로에서 원문+번역문 마스킹 후 비동기 INSERT(`app.translations`), `TranslationResultDto` 보강(source_text/lang/sequence/segment_id/confidence), 모니터 전용 WS(`/ws/monitor?sessionId=`), `/internal/sessions/stop` **멱등화**+`ended_at` 기록. (정리: 스모크 후 `compose down -v`·더미 `.env.prod` rm)
- (2026-06-02, **Phase 4 세션**, 브랜치 `claude/monitor-phase4-impl-wUu3v`, **phase3 `0fRe8`에서 분기**) **저장+마스킹+모니터 WS+stop 멱등화 구현·런타임 검증 통과(검증완료-런타임)**. ⓐ **공용 마스킹 유틸**(`src/masking/masker.py`, 순수함수 `mask_text`): 정규식 4종(주민번호 `\d{6}[-\s]?[1-8]\d{6}` / 휴대폰 `01[016789]…`(유선 02 등 제외) / 이메일 / 카드 16자리)을 **카드→RRN→폰 순**(긴 패턴 우선)으로 적용, 각 숫자패턴은 `(?<!\d)…(?!\d)` 룩어라운드로 더 긴 숫자열 슬라이스 오탐 방지. 이름/계좌는 §6대로 비대상. 원문+번역문 양쪽 동일 적용(mask-at-write). pytest 양/음성 다수. ⓑ **`pusher.py` capture 경로**: `push_to_client`가 클라broadcast(핫패스) 먼저 → 그 다음 `source_text/session_id/...` 있으면 **fire-and-forget `asyncio.create_task(_capture)`**(전역 try/except로 격리, 번역 비차단). `_capture`=마스킹→모니터 WS fan-out→`app.translations` INSERT. 메타는 separator를 통해 전달: `SegmentState`+신규 `PendingSentence`에 session/segment/sequence/lang/confidence 실어 `_flush`→`_push_loop`→pusher로 흐름. `TranslationResultDto` 보강(source_text/lang/sequence/segment_id/confidence). ⓒ **모니터 전용 WS**(`src/ws/monitor.py` `MonitorHub` + `/ws/monitor?sessionId=`): 클라 hub와 분리된 sessionId→{소켓}set fan-out(백로그 없음, 라이브 구독자만), 종료 시 `session_closed` 이벤트 후 close. ⓓ **stop 멱등화**: `repository.end_session`=`UPDATE app.sessions SET ended_at=now() WHERE session_id=$1 AND ended_at IS NULL RETURNING …`로 **ended_at 1회만**, ended일 때만 translation_count 재집계+모니터 close 이벤트 1회. 2회 호출 무부작용(런타임+단위 입증). **결정/함정**: ① **sessions 행 생성 시점=(b)주경로 start + (a)첫 INSERT 안전망** 하이브리드(둘 다 `ON CONFLICT DO NOTHING`)→ended_at UPDATE 타깃 항상 존재. ② 번역 target은 'en-US' 유지(기존 동작 불변), 그 값을 target_lang으로 기록(정직). ③ **검증은 호스트 직결**: 컨테이너 내부 pip PyPI 불가→호스트에 `asyncpg alembic psycopg[binary] python-dotenv fastapi deepl httpx pytest` 설치 후 컨테이너 브리지 IP(`172.18.0.2`)로 `POSTGRES_HOST`만 오버라이드. 실제 Pusher+MonitorHub+`/ws/monitor`를 TestClient로 구동해 마스킹 페이로드 수신·`psql`로 마스킹 행 확인(원문 PII 0건)·stop 2회 멱등 확인. deps **무추가**(pytest는 이미 dev에 존재)→`uv lock` 변동 없음. **다음(Phase 5: 이력 조회 API) 주의**: `GET /api/monitor/sessions`(목록) + `/sessions/{id}/translations?cursor=&limit=`(sequence 정렬·커서 페이지네이션) + `/translations?lang=&from=&to=&q=`(기간/언어/키워드). 인덱스는 `ix_translations_session_id`/`ix_translations_created_at` 활용, 키워드 q는 **마스킹된** source/translated 대상(원문 PII는 애초에 DB에 없음). (정리: 스모크 rm·`compose down -v`·더미 `.env.prod` rm 완료)
- (2026-06-03, **Phase 4 독립 검증 세션**, 동일 브랜치 `claude/monitor-phase4-impl-wUu3v`) **판정: 합격(검증완료-런타임 재확인, 신뢰 가능)**. 코드/compose 무수정, 이 메모 1줄만 추가. **A.정적 PASS**: 7개 변경/신규파일 `py_compile` exit 0; `uv lock --check` exit 0(uv.lock 무변경=deps 무추가, pyproject 변경은 pytest config뿐); `ruff check` 신규 마스킹/WS/repo/dto 파일 **All checks passed**; main.py+kss_separator.py는 phase3 base 대비 ruff 위반 히스토그램 **완전 동일**(F841×2/I001×2/SIM110/UP006/UP035/W291/W293×4)=신규 위반 0; `grep -rn "services.python.src"`→`src/test.py` 1건만(알려진 잔재). **B.단위 PASS(27 passed)**: 마스킹 양성(RRN/폰/이메일/카드)·음성(날짜/유선02·031/짧은숫자/12자리ID/IP/@no-domain) 전부 정확, 추가 적대적 프로브도 통과; stop 멱등성 fake pool이 `UPDATE…WHERE ended_at IS NULL` 0행 매칭을 정확히 모사(ended_at_writes=1, set_count_writes=1). **C.런타임 스모크 PASS**: dockerd→`mirror.gcr.io/library/postgres:16-alpine` 우회 pull·태깅→더미 `.env.prod`→`compose --env-file .env.prod up -d postgres`→healthy(t=15s)→호스트에서 브리지 IP `172.18.0.2`로 `alembic upgrade head`. 실제 `Pusher.push_to_client`+`MonitorHub`+`/ws/monitor`+`start/stop` 라우트를 Starlette TestClient(app 단독 lifespan으로 pool 동일루프 생성)로 구동: ① 모니터 WS가 **마스킹 풀 페이로드 수신**(source=`[RRN]/[PHONE]/[EMAIL]`, translated=`[RRN]/[PHONE]/[CARD]/[EMAIL]`, meta 전부), ② `app.translations` 1행 마스킹 저장·`WHERE source_text ~ '[0-9]{6}' OR translated_text ~ '@'` **카운트=0**(원문 PII 미저장), ③ `app.sessions` started+ended+`translation_count=1`, ④ **stop 2회**: 1회차 `ended=True/count=1`+`session_closed` 1회 emit, 2회차 `ended=False/count=1`(ended_at 재기록 없음), ⑤ **(a)안전망 검증**(start 없이 push만): 첫 INSERT의 `ensure_session`이 세션행 생성(세션당 1회, `_ensured_sessions` 가드)·2행 저장·PII 0. fire-and-forget 격리는 코드 확인(클라broadcast await 먼저→`create_task(_capture)` 전체 try/except). **D.회귀 PASS**: pool/main/config/migrations `py_compile` exit 0, `alembic downgrade base`(app스키마 제거)→`upgrade head`(테이블 재생성) 왕복 exit 0. **발견 결함: 없음**(설계상 한계 1건—RRN 정규식 성별자리 `[1-8]`이라 1800년대생 코드 9/0 미매칭, §6 의도·무시 가능). 정리: 스모크 스크립트 rm·`compose down -v`(볼륨 제거)·더미 `.env.prod` rm 완료. **결론: 트래커 Phase 4 "검증완료(런타임)"는 독립 검증으로 재확인됨.**
- (2026-06-04, **Phase 5 세션**, 브랜치 `claude/monitor-phase5-history-api-AB5cx`, **phase4 `wUu3v`에서 분기**) **이력 조회 API 3개 구현·런타임 검증 통과(검증완료-런타임)**. **한 일**: ⓐ read-side 순수 async 쿼리 모듈 신규 `src/repository/implementation/monitor_query_repository.py`(asyncpg.Pool 받음, 전부 `$n` 바인딩, q는 바인딩 ILIKE→SQL injection 차단, `clamp_limit`/`clamp_offset`/정수커서/검색커서 인코딩·디코딩 헬퍼는 라우트가 얇게 유지되도록 `(rows, next_cursor)` 반환). ⓑ `main.py`에 `@app.get` 3개 + pydantic 응답모델(snake_case 필드+camelCase alias로 기존 API 스타일 통일, datetime ISO8601 자동직렬화) + `Depends(get_db_pool)`. **결정/함정**: ① **페이지네이션 전략 3종**: sessions=OFFSET(행 적고 started_at DESC), sessions/{id}/translations=**PK `id` 키셋**(`ORDER BY id ASC, WHERE id > $cursor` — sequence가 nullable·비유니크라 유일한 안정 전순서), search=**`(created_at, id)` 복합 키셋**(`ORDER BY created_at DESC, id DESC`, opaque base64(JSON) 토큰으로 인코딩). ② **lang 필터는 source_lang OR target_lang 양쪽 매칭**(문서·코드 주석 명시). ③ **q는 마스킹된 source/translated 텍스트 대상 ILIKE '%q%'**(원문 PII는 Phase 4 mask-at-write로 애초에 DB에 없음→키워드 검색이 민감정보 노출 불가). ④ limit 초과/음수는 **안전 클램프**(sessions 50/200, session_translations·search 100/500), 잘못된 cursor/from/to는 **422**, 빈 결과는 **200+빈배열**. ⑤ **B008 함정**: ruff가 함수 기본값의 `Depends()` 호출을 B008로 잡음(`Query()`는 ruff 기본 면제)→FastAPI 관례라 `pyproject` `[tool.ruff.lint.flake8-bugbear] extend-immutable-calls`에 `fastapi.Depends/Query/...` 추가(거짓양성 제거, main.py 신규위반 0 달성). deps 무추가→`uv lock` 무변경. ⑥ **검증은 호스트 직결**(Phase 2~4와 동일): 컨테이너 내부 pip PyPI 불가→호스트에 `asyncpg alembic ruff fastapi starlette ... pytest` 설치 후 postgres 컨테이너 브리지 IP(`172.18.0.2`)로 `POSTGRES_HOST`만 오버라이드. ⑦ **TestClient 스모크 시 kss 더미 스텁 필수**(main.py가 모듈 로드 시 `from kss import Kss`→무겁고 mecab 의존)+lifespan은 dummy NATS로 consumer 백그라운드 task가 실패하나 `done_callback`이 로깅만(비차단)→TestClient startup 정상, pool은 동일 루프 생성. **다음(Phase 6: /monitor 프론트) 주의**: 경량 정적 페이지를 nginx `/monitor`로 서빙(별도 SPA 불필요), 이 API 3개(camelCase 응답)와 Phase 4 `/ws/monitor?sessionId=` WS를 소비. 응답 페이지네이션 키: sessions=`nextOffset`(정수), session_translations=`nextCursor`(정수 id), search=`nextCursor`(opaque 문자열 토큰). 라이브/이력 토글은 `live` 플래그(ended_at IS NULL)로. **Phase 7**: 이 API들은 인증 전무→nginx Basic Auth/IP 화이트리스트 필수(민감정보는 마스킹돼 있으나 노출 최소화). (정리: 스모크 스크립트 rm·`compose down -v`·더미 `.env.prod` rm·pyc/.pytest_cache 정리 완료)
- (2026-06-04, **Phase 5 독립 검증 세션**, 동일 브랜치 `claude/monitor-phase5-history-api-AB5cx`) **판정: 합격(검증완료-런타임 재확인, 신뢰 가능)**. 구현 무수정, 이 메모 + 트래커 재확인 표기만 추가(코드/compose/deps 일체 무수정, 결함 0). **A.정적 PASS**: 변경/신규 3파일 `py_compile` exit 0; 신규(`monitor_query_repository.py`+테스트) `ruff check` **All checks passed**; `main.py`는 phase4 base 대비 위반 히스토그램 **동일 `2 F841 / 1 I001`(신규 0)** — B008 화이트리스트가 **load-bearing임을 직접 입증**(`--isolated`로 whitelist 끄면 `Depends()` 기본값 3건이 B008로 발화 → `pyproject` `flake8-bugbear.extend-immutable-calls`로 정확히 억제); `uv lock --check` exit 0 + `uv.lock`은 base와 **바이트 동일(deps 무추가, pyproject 변경은 ruff 설정뿐)**; `grep -rn services.python.src`→`src/test.py` 1건만(알려진 잔재). **B.단위 PASS(전체 56=신규 29+기존 27)**: clamp 경계·정수/검색 커서 라운드트립+오류 ValueError·list_sessions/session_translations(`AND id > $2` 키셋)/search SQL·바인딩 검증(lang `source OR target`, from/to, q **바인딩 ILIKE** + injection 프로브 `'; DROP TABLE …`가 SQL텍스트 미진입·`%evil%`로 바인드, 콤보 파라미터 순서, 빈결과·next None) 전부 의미 있는 어서션. **C.런타임 스모크 PASS(실DB 25체크 ALL PASS)**: dockerd→`mirror.gcr.io/library/postgres:16-alpine` 우회 pull·태깅→더미 `.env.prod`→`compose --env-file .env.prod up -d postgres`→healthy(t=12s)→호스트에서 브리지 IP `172.18.0.2`로 `alembic upgrade head`. 3세션(B 라이브 ended_at NULL)+10translation(sequence 일부 NULL·ko/ja/en-US 혼합·created_at 06-01~06-04 분산·마스킹 텍스트/`[PHONE]`·`[RRN]` 플레이스홀더) 시드 후 **실제 라우트 3개를 Starlette TestClient(앱 단독 lifespan, pool 동일루프; kss 더미 스텁·dummy NATS 비차단)로 호출**: ① sessions started_at DESC=[B,C,A]·live 플래그·offset 페이지(limit=2→nextOffset=2→partial None)·clamp(9999→200, 0→1); ② sessions/sess-A/translations **id ASC 안정 커서**로 limit=2씩 순회 5행 **무중복·무누락**·sequence `[1,2,None,4,5]` 보존·잘못된 커서→422·빈/없는 세션→200+[]; ③ search lang=ja(source OR target→5행 B+C)·lang=ko(8행)·from/to 범위(06-04→2, 06-02~03→3)·q=`hello` 마스킹텍스트 ILIKE→4·q=`[PHONE]` 플레이스홀더→2·콤보(ko+hello→3)·커서 순회 10행 무중복·opaque 문자열 커서·잘못된 cursor/from→422·무매치→200+[]; 직렬화 startedAt/createdAt **ISO8601 문자열(`…Z`)**. **PII 교차검증**: 시드는 마스킹 텍스트만 → `source_text/translated_text ~ '[0-9]{6}' OR ~ '@'` **카운트=0**(q가 원문 PII에 매칭 불가 입증). **추가(독립 강화)**: 동일 `created_at` 7행으로 `(created_at,id)` 복합 키셋 tie-break 스트레스 → id DESC로 7행 **무중복·무누락**(시드가 모두 distinct timestamp라 미검증되던 동률 경로를 직접 커버). **D.회귀 PASS**: `alembic downgrade base`(app스키마 count=0)→`upgrade head`(테이블·인덱스 2개 재생성) 왕복 exit 0, Phase2/3/4 자산 `py_compile` exit 0. 정리: 스모크/pyc/.pytest_cache rm·`compose down -v`(볼륨 제거)·더미 `.env.prod` rm 완료. **발견 결함: 없음.** **Phase 6 주의**: 응답은 camelCase 별칭(sessionId/startedAt/createdAt ISO8601), 페이지네이션 키 sessions=`nextOffset`(정수)/session_translations=`nextCursor`(정수 id)/search=`nextCursor`(opaque base64 토큰), 라이브/이력 토글은 `live`(ended_at IS NULL). 이 API들은 **인증 전무**→Phase 7 nginx Basic Auth/IP 화이트리스트 필수.
- (2026-06-04, **Phase 6 세션**, 브랜치 `claude/monitor-phase6-frontend-kUv85`, **phase5 `AB5cx`에서 분기**) **경량 정적 `/monitor` 프론트 + nginx 라우팅 구현·런타임 검증 통과(검증완료-런타임)**. **한 일**: ⓐ 신규 `infra/nginx/html/monitor/{index.html,style.css,app.js}` — 바닐라 HTML/CSS/JS(빌드툴·SPA·외부 CDN 0, 오프라인). 세션 탭(목록 started_at DESC·LIVE/종료 배지·nextOffset 더보기→선택 시 원문↔번역 2열 표, 이력은 nextCursor 정수 id 키셋·sequence NULL 허용, **live(ended_at IS NULL) 세션만 라이브 토글 노출**→`/ws/monitor?sessionId=` 구독해 마스킹 풀 페이로드 append·`session_closed` 수신 시 종료 표기, 종료 세션은 이력만) + 검색 탭(lang=source OR target·from/to·q는 마스킹 텍스트·nextCursor opaque 문자열). **모든 외부 텍스트 textContent 렌더(innerHTML/eval 0=XSS 방지)**, 시각 ISO8601 파싱. ⓑ nginx: prod `infra/nginx/nginx.conf`+dev `infra/nginx/dev/nginx.conf` **양쪽에** `location /api/monitor/`→python(8000) + `location /monitor`(static, index.html 폴백) + http 블록 `include mime.types` 추가. **결정/함정**: ① **`/api/`는 node로 프록시**됨→모니터 API는 python:8000이라 `/api/monitor/`를 별도 추가(더 구체적 prefix라 `/api/`보다 우선; 음성검증 `/api/something-node`→502 죽은 node upstream으로 입증). ② **두 conf 모두 `include mime.types` 부재**→정적 `.css`가 `text/plain`으로 나가 표준모드 브라우저가 스타일시트 거부→http 블록에 `include /etc/nginx/mime.types`+`default_type` 추가(런타임 재현·교정: 추가 후 .css→`text/css`, .js→`application/javascript`). ③ `infra/nginx/html/` 신규 디렉터리(prod compose가 이미 `/var/www/html`로 마운트→파일만 채우면 됨). ④ Node·Python 백엔드 **무수정**(git diff=nginx conf 2개+docs+신규 html뿐). **검증**: A.정적=prod/dev `nginx -t` 둘 다 syntax ok(임시 nginx 컨테이너, prod는 self-signed cert stub·`--add-host node/python`)·기존 라우팅 무회귀(diff 추가만)·CDN 0·textContent 전용·`node --check`·24 DOM id 전부 존재. B.런타임=postgres(mirror) 시드(3세션 B라이브+10translation 마스킹) 후 **main.py 라우트 그대로 재사용한 얇은 uvicorn 러너**(lifespan만 pool+MonitorHub로 축소)+**dev conf nginx 컨테이너**로 `/monitor/`→200 html·정적 MIME 교정확인·`/api/monitor/*`→python JSON 200(node 아님 502로 음성입증)·`/ws/monitor`→**101**(sessionId 없으면 403 거부)·DOM shim으로 실제 JSON 렌더(3행 DESC·LIVE 배지·textContent) 정확. C.회귀=services 무변경·**pytest 56 passed**·nginx 라우팅 무회귀. **발견 결함: 없음**(다만 mime.types 누락은 이번에 교정한 선재 문제). 정리: nginx-smoke·uvicorn·`compose down -v`·더미 `.env.prod`·러너·스크립트·cert stub·pyc 전부 rm. **Phase 7 주의(중요)**: 이 페이지·API 3개·`/ws/monitor` **전부 무인증**(마스킹은 돼 있으나 세션·번역 전량 열람 가능)→**nginx Basic Auth 또는 IP 화이트리스트 필수**(`/monitor`+`/api/monitor/`+`/ws/monitor` 세 위치 모두 보호) + 30일 보존 자동화.
- (2026-06-04, **Phase 6 독립 검증 세션**, 브랜치 `claude/monitor-phase6-frontend-kUv85` 기준 재현) **판정: 합격(검증완료-런타임 신뢰 가능). 발견 결함: 없음.** 구현 무수정(nginx conf 2개·html 3파일·Node/Python 백엔드 일체 무수정), 이 메모 + 트래커 재확인 표기만 추가. **A.정적 PASS**: prod/dev `nginx -t` 둘 다 **syntax ok·test successful**(임시 `nginx:1.27-alpine`, prod는 self-signed cert stub `/etc/letsencrypt/live/neemba.app/`+`--add-host node/python`); 두 conf diff는 **추가만**(기존 라우팅 prod `/api/`→node·`/ws`→python·`/health`·acme·`/`, dev `/api/mic`·`/api/`→node·`/ws`→python·`/health`·`/` 전부 무삭제); 추가분=`include /etc/nginx/mime.types`+`default_type`·`/api/monitor/`→python·`/monitor` static(둘 다); html/js **외부 http(s)URL 0**(CDN/unpkg/jsdelivr 0)·**innerHTML/eval/document.write/insertAdjacentHTML 0**(textContent 전용)·`node --check app.js` OK·`$("id")` 24개 전부 index.html에 존재(누락 0). **B.런타임 스모크 PASS**: dockerd→`mirror.gcr.io/library/postgres:16-alpine` 우회 pull·태깅→더미 `.env.prod`→`compose up -d postgres`→healthy→호스트 브리지 IP(`172.18.0.2`)로 `alembic upgrade head`(테이블 2·인덱스 2). 3세션(sess-B 라이브 ended_at NULL)+10translation(sequence 일부 NULL·ko/ja/en-US·`[PHONE]`/`[RRN]`/`[CARD]`/`[EMAIL]` 플레이스홀더, **DB내 raw PII 카운트=0**) 시드. **main.py 라우트 객체 그대로 재사용한** 얇은 uvicorn 러너(lifespan을 pool+MonitorHub로만 축소, kss 더미 스텁)+**dev conf nginx 컨테이너**(`--add-host python:host-gateway --add-host node:127.0.0.1`, html 마운트)로 교차검증: ① `/monitor/`→**200 text/html**·`style.css`→**text/css**·`app.js`→**application/javascript**(mime.types 동작)·deep-link→index.html 폴백; ② `/api/monitor/sessions`→**python JSON 200**(3세션·started_at DESC=[B,C,A]·live 플래그 정확·camelCase 키·`startedAt`=`…Z` ISO8601·`nextOffset`)·session translations(limit=2→`nextCursor`=정수 id 2·sequence 보존)·search(lang=ja→**5행** sess-B+sess-C, source OR target 매칭 입증) 전부 nginx 경유 정상; ③ **음성검증** `/api/whatever`·`/api/mic`→**502(죽은 node upstream)** =비-monitor `/api/`는 node로 라우팅됨 입증(python이면 404)→`/api/monitor/`가 longer-prefix로 우선함을 200/502 대비로 교차확인; ④ **WS** `/ws/monitor?sessionId=sess-B`→nginx 경유 **101 Switching Protocols**, sessionId 없으면 **HTTP 403 거부**(핸들러 accept 전 close); **in-process** 실제 `MonitorHub.broadcast`→연결된 클라가 마스킹 translation 페이로드 수신·**수신 payload raw PII 0**(라이브 append 경로 end-to-end). **DOM-unit**: jsdom 부재→최소 DOM shim(node)에 **실제 `/api/monitor/sessions` 응답 JSON 주입**해 app.js 실행→세션목록 **3행·서버 DESC 순서 유지·LIVE 배지는 live 세션(sess-B)만·`종료` 배지는 종료 세션·textContent(lang→/건수/시각)·`sessions-more` hidden(nextOffset null)·status 카운트** 전부 정확. **C.회귀 PASS**: `git diff`(phase5..phase6) 범위=`infra/nginx`(conf 2개)+`docs`+신규 `infra/nginx/html/`뿐 **services/python·node 무변경**; Phase 5 `pytest` **56 passed**; 기존 nginx 라우팅 무회귀(nginx -t·502 음성검증·`/health` 200). **참고**: 본 세션 시드가 원세션과 달라 일부 카운트(DESC 순서 표기·lang=ja 행수)는 수치상 상이하나 **계약 동작(DESC 정렬·live 플래그·source OR target·camelCase/ISO8601·키셋 페이지네이션·MIME 교정·라우팅 우선순위)은 전부 동일 재현**. mime.types 누락은 원세션이 교정한 선재 문제로 현 conf엔 이미 반영됨. 정리: nginx-smoke·uvicorn·`compose down -v`·더미 `.env.prod`·러너/스모크 스크립트/cert stub/pyc/.pytest_cache 전부 rm. **Phase 6 트래커 "검증완료(런타임)" 신뢰 가능.**
- (2026-06-05, **Phase 7 세션**, 브랜치 `claude/monitor-phase7-access-retention-qx7jB`, **phase6 `kUv85`(594d89b)에서 분기**) **접근제어(Basic Auth)+30일 보존 자동화 구현·런타임 검증 통과(검증완료-런타임). 발견 결함: 없음.** **한 일**: ⓐ nginx prod+dev conf 양쪽에 세 monitor 위치(`/monitor`·`/api/monitor/`·**신규 `/ws/monitor`** 블록)에 `auth_basic`+`auth_basic_user_file /var/lib/neemba/secrets/monitor.htpasswd` 추가(`/ws/monitor`는 `/ws`보다 longer-prefix라 먼저 매칭→클라 `/ws`는 무인증 유지). ⓑ compose `nginx` 서비스에 `/var/lib/neemba/secrets:ro` 마운트 추가(node/python과 동일 패턴, htpasswd는 레포 미커밋). ⓒ 보존=신규 `infra/postgres/retention.sh`(psql while-loop, `to_regclass` 가드+plpgsql DO/GET DIAGNOSTICS 카운트 로그)+compose `pg-retention` 사이드카(postgres healthy 후 기동, `RETENTION_ONESHOT`/`RETENTION_DAYS`/`RETENTION_SLEEP_INTERVAL` 튜너블). **확정한 결정**: ① **인증 방식=Basic Auth**(IP 화이트리스트는 모바일/클라우드 가변 IP에서 취약→기각). ② **WS 함정 처리=프론트 무수정**: 브라우저 `new WebSocket()`은 Authorization 헤더를 임의로 못 싣지만, `/monitor` HTML 로드 시 Basic 챌린지를 통과하면 **same-origin 캐시 자격증명이 후속 WS 업그레이드 핸드셰이크에 자동 첨부**됨(문서화 브라우저 동작). 런타임으로 nginx가 WS 업그레이드에 Basic을 적용·통과시킴을 입증(헤더 있으면 **101**, 없으면 **401**)→`app.js` 무수정. **폴백 명시**: 자격증명 미전달 클라이언트를 만나면 `/ws/monitor`만 `allow/deny` IP 화이트로 전환(나머지 둘은 Basic 유지). ③ **보존 방식=스케줄 DELETE 사이드카**(파티셔닝(b)보다 가볍고 스키마 무변경·저위험; translations는 `created_at`, sessions는 `ended_at IS NOT NULL AND ended_at<30d`로 **라이브 세션 나이무관 보호**). **발견 함정**: ⓐ 컨테이너 내부 pip PyPI 불가(Phase 2~6 동일)→호스트에 deps 설치+postgres 브리지 IP 직결(`POSTGRES_HOST`만 오버라이드)로 alembic/러너 검증. ⓑ 얇은 러너가 main.py를 import하므로 `prometheus_client`/`deepl`/`httpx`/`nats-py`까지 호스트 설치 필요(kss는 더미 스텁)+lifespan을 pool+MonitorHub로 축소(백엔드 로직 무수정). ⓒ nginx 러너 도달 위해 uvicorn은 0.0.0.0 바인드+`--add-host python:host-gateway`(node는 `:127.0.0.1`로 죽은 upstream→502 음성검증). ⓓ htpasswd stub은 호스트에 htpasswd 바이너리 없어 `openssl passwd -apr1`로 생성(nginx가 apr1 지원). **검증 요약**: A.정적=prod/dev `nginx -t` 양쪽 syntax ok·diff 추가만·시크릿 미커밋. B.런타임=무자격증명 세 위치 401/올바른 Basic 200·JSON·WS 101·음성검증(`/health` 200·`/api/*`→502 node·`/ws` 500 NOT 401)·**실제 사이드카로 보존 4 translations/1 session 삭제·라이브 세션 생존·멱등 0/0**. C.회귀=services 무변경·pytest **56 passed**·라우팅 무회귀. 정리: nginx-smoke·uvicorn·`compose down -v`·더미 `.env.prod`·htpasswd/cert stub·러너/스크립트·pyc/.pytest_cache 전부 rm. **운영 시 사람이 할 일**(커밋·채팅 금지): `htpasswd -B -c /var/lib/neemba/secrets/monitor.htpasswd monitor`로 실자격증명 1줄 생성(§7 문서화). **모니터링 플랜 Phase 0~7 전부 검증완료(런타임)**.
- (2026-06-05, **Phase 7 독립 검증 세션**, 동일 브랜치 `claude/monitor-phase7-access-retention-qx7jB`) **판정: 합격(검증완료-런타임 재확인, 신뢰 가능). 발견 결함: 없음.** 구현(nginx conf 2개·`docker-compose.prod.yml`·`infra/postgres/retention.sh`·services 일체) **무수정**, 이 메모 + 트래커 재확인 표기만 추가. **A.정적 PASS**: prod/dev `nginx -t` 둘 다 **syntax ok·test successful**(임시 `nginx:1.27-alpine`; prod는 self-signed cert stub+`openssl passwd -apr1` htpasswd stub+`--add-host node/python` 마운트); `git diff 594d89b..HEAD --name-status`=`docker-compose.prod.yml`·`docs`·`infra/nginx/{nginx.conf,dev/nginx.conf}`·신규 `infra/postgres/retention.sh` 5개뿐 **services/node·python 0줄 변경**; nginx diff는 **추가만**(세 monitor 위치 `auth_basic`+`auth_basic_user_file`, `/ws/monitor`는 `/ws`보다 longer-prefix; 비-monitor 라우팅 무삭제, 유일 `-`는 주석 1줄), compose는 `nginx`에 `/var/lib/neemba/secrets:ro` 마운트+`pg-retention` 사이드카 추가; **시크릿 0**(`git ls-files`에 htpasswd/.pem/.env 0건·`.gitignore`가 `.env`/`.env.*`/`*.pem` 커버·retention.sh 하드코딩 비밀 0, `PGPASSWORD`는 env 참조뿐). **B.런타임 스모크 PASS**: dockerd→`mirror.gcr.io/library/{postgres:16-alpine,nginx:1.27-alpine,alpine:3.20}` 우회 pull·태깅→더미 `.env.prod`→`compose up -d postgres`→healthy(t=7s)→호스트(`asyncpg alembic psycopg fastapi uvicorn websockets prometheus_client deepl httpx nats-py …` 설치)에서 브리지 IP `172.18.0.2`로 `alembic upgrade head`. **main.py 라우트 객체 그대로 재사용한** 얇은 uvicorn 러너(lifespan만 pool+MonitorHub+WebSocketHub로 축소, kss 더미 스텁, 0.0.0.0 바인드)+**dev conf nginx 컨테이너**(`--add-host python:host-gateway --add-host node:127.0.0.1`, htpasswd stub 마운트)로 교차검증: ① **인증**=무자격증명 `/monitor/`·`/monitor/app.js`·`/api/monitor/sessions`→**전부 401**·오자격증명→**전부 401**·올바른 Basic→`/monitor/` **200 text/html**·`app.js` **200 application/javascript**·`/api/monitor/sessions` **200 application/json**(시드 세션 1행 반환); ② **WS+Basic**(websockets 클라 Authorization 헤더 3케이스)=무헤더 **HTTP 401**·오헤더 **HTTP 401**·올바른 헤더 **101 CONNECTED**=nginx가 WS 업그레이드 핸드셰이크에 Basic을 적용·통과시킴 직접 입증(헤더↔101/무헤더↔401)→프론트 무수정 전제(브라우저 same-origin 캐시 자격증명 자동첨부)와 정합; ③ **음성검증(회귀)**=무인증 `/health`→**200**·`/api/whatever`·`/api/mic`·`/api/sessions/stop`→**전부 502(죽은 node upstream, NOT 401)**=비-monitor `/api/`는 node로 라우팅·인증 미누출·`/api/monitor/`가 longer-prefix로 우선함을 200/502 대비로 교차확인·`/`→**403(NOT 401)**·`/index.html`→**404(NOT 401)**·클라 `/ws`→**101(NOT 401)**=인증이 monitor 밖으로 새지 않음. **C.보존 PASS(실제 사이드카)**: 시드 translations 7행(40·35·31일전 + 29·10·1일전 + 라이브세션 하위 40일전)·sessions 4행(40일전 종료/10일전 종료/**41일전 라이브 ended_at NULL**/29일전 종료)→**실제 `pg-retention` 컨테이너**(`docker compose … run --rm -e RETENTION_ONESHOT=1 pg-retention`)→`NOTICE: pg-retention: deleted 4 translations, 1 sessions (window=30 days)`→translations 7→**3**(29/10/1일 생존)·sessions 4→**3**(40일 종료만 삭제, **41일 라이브 세션 생존**=ended_at NULL 나이무관 보호·29일 종료 세션 생존). **재실행 멱등**: 2회차 `deleted 0 translations, 0 sessions`·카운트 불변(3/3). **D.회귀 PASS**: services 무변경(diff 0줄)·Phase 5 `pytest` **56 passed**(kss sitecustomize 스텁)·nginx 라우팅 무회귀(`nginx -t`·502 음성검증). **환경차 1건(결함 아님)**: 원세션 메모는 클라 `/ws`→500 기록했으나 본 검증은 살아있는 얇은 러너라 `/ws`→101 — 둘 다 **NOT 401**로 핵심 계약(인증 미누출) 동일. 정리: nginx-smoke·uvicorn·`compose down -v`·더미 `.env.prod`·htpasswd/cert stub·러너/스크립트·pyc/.pytest_cache·worktree 전부 rm. **결론: 트래커 Phase 7 "검증완료(런타임)"는 독립 검증으로 재확인됨(신뢰 가능). 접근제어 3위치 Basic Auth·30일 보존(라이브 보호·멱등) 전부 명세대로 동작. 모니터링 플랜 Phase 0~7 전부 검증완료(런타임).**
- (2026-06-01, Phase 1 구현 세션, 동일 브랜치 `claude/monitor-phase0-compose-baseline-C6XEo`) **`docker-compose.prod.yml`에 postgres 추가 완료**(image `postgres:16-alpine`, expose 5432 내부전용, `pg-data` 볼륨, `pg_isready` healthcheck, `python.depends_on: postgres(service_healthy)`). 더미 `.env.prod`(가짜값)로 `config --quiet` **exit 0** 확인 후 삭제. **다음 세션(Phase 2) 주의**: ① 기동/검증 시 **`--env-file .env.prod` 필수**(POSTGRES_DB ← ${POSTGRES_DATABASE} 치환 때문, §7 Phase 1 메모 참조). ② Python은 `POSTGRES_DATABASE`/`POSTGRES_USER` 등을 `config.py get_postgres_config()`로 읽음 — `.env.prod`에 `POSTGRES_HOST=postgres`(서비스명) 포함 5개 변수 수동 추가 필요(서버/맥, 실비번은 커밋·채팅 금지). ③ **맥 실검증 TODO**: `make up-prod`(또는 직접 compose+`--env-file`)로 실제 `up` → `pg_isready` healthy → python 컨테이너에서 DB 도달 → 재기동 후 `pg-data` 영속 확인(이 4개가 Phase 1 "검증완료" 승격 조건). ④ Phase 0 Makefile 불일치 미해결 — 맥 실기동 방식 따라 별도 Phase로 수정 결정.

### 각 구현 세션 종료 시 체크리스트
1. 위 표의 해당 Phase 상태·검증결과·브랜치 갱신
2. 인계 메모에 함정/결정/TODO 한 줄 추가
3. `docs/monitoring-plan.md` 포함해서 commit & push
