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

---

## 8. 미해결 선결 이슈 (구현 전 확인)

1. **base `docker-compose.yml` 부재** — `Makefile`은 `up-prod`에서 `-f docker-compose.yml -f docker-compose.prod.yml`(base+오버레이)를 기대하나 레포엔 base 파일이 없고 `docker-compose.prod.yml`이 단독 완결형.
   - **(해소: Phase 0 조사, 2026-06-01)** 레포(이 컨테이너) 커밋 기준으로 확정:
     - 추적되는 compose 파일은 **`docker-compose.prod.yml` 단 하나**. base `docker-compose.yml`은 추적·워킹트리·`.gitignore` 어디에도 없음(= gitignore된 게 아니라 진짜로 레포에 존재하지 않음).
     - `docker-compose.prod.yml`은 **self-contained**: `name: neemba` + `networks.appnet` + `volumes.nats-data` + 8개 서비스(nginx/rtmp/reloader/certbot/node/python/nats/nats-box) 전부 자체 정의. `extends`나 base 참조 **없음**. → `docker compose -f docker-compose.prod.yml config --services` **exit 0**(8개 서비스 정상 파싱, YAML 구조 유효).
     - `Makefile`의 `PROD?=-f docker-compose.yml -f docker-compose.prod.yml` 그대로 실행 시 **exit 1, `open docker-compose.yml: no such file or directory`** → **레포 커밋 상태에서는 `make up-prod`가 동작 불가**(Makefile↔실제 파일 불일치 = 사실로 확정).
   - **잔여 TODO(맥/실서버에서 사람이 확인):** 실제 prod가 어떻게 떠 있는지 — (a) `make up-prod` 대신 `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d` 직접 실행, (b) 맥 로컬에 untracked `docker-compose.yml`(base)가 별도로 존재, (c) Makefile을 로컬에서 수정해 사용 — 중 무엇인지 1개로 확정. base `docker-compose.yml`은 `.gitignore` 대상이 **아니므로**, 맥에 존재한다면 단지 commit 안 된 상태일 뿐(존재 여부 직접 확인 필요).
   - **Makefile 정합성 수정은 Phase 0 범위 밖(별도 Phase 권고).** Phase 1 명세는 `docker-compose.prod.yml` 단독 기준으로 작성되어 있고 그대로 유효함.
2. `.env.prod`, `docker-compose.dev/stage.yml`은 `.gitignore` 처리 → 로컬에만 존재. POSTGRES_* 변수는 서버에서 수동 추가 필요.
3. Node/Python 계정 정합성: 죽은 Node 코드는 `node_auth`, config 기본은 `neemba` → **`neemba`로 통일** (Node DB 폐기로 충돌 없음).

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
| 1 | Postgres 컨테이너 | 구현완료(미검증) | `docker-compose.prod.yml`에 postgres/pg-data/`python.depends_on` 추가. 더미 `.env.prod`로 `config --quiet` **exit 0**, `--env-file` 시 `POSTGRES_DB=neemba` 정상 치환 확인. ⚠️실제 `up`/`pg_isready`/asyncpg 접속/재기동 영속은 **맥·실서버 미검증**(시크릿 없음) | `claude/monitor-phase0-compose-baseline-C6XEo` | `POSTGRES_DB`는 `${POSTGRES_DATABASE}` 치환→**`--env-file .env.prod` 필수**(아래 메모) |
| 2 | Python DB 배선(pool/lifespan) | 미착수 | 앱 기동 + 풀 생성 확인 | — | |
| 3 | Alembic + 스키마 | 미착수 | `alembic upgrade head` + 테이블 확인 | — | sync 드라이버 psycopg |
| 4 | 저장+마스킹+모니터 WS | 미착수 | pytest + INSERT/WS 동작 | — | stop 멱등화 포함 |
| 5 | 이력 조회 API | 미착수 | 엔드포인트 호출 검증 | — | |
| 6 | /monitor 페이지 | 미착수 | 로컬 렌더 확인 | — | |
| 7 | 접근제어 + 30일 보존 | 미착수 | Basic Auth + 보존 동작 | — | |

### 세션 간 인계 메모 (자유 기록)
> 각 세션이 다음 세션에게 남기는 메모. 발견한 함정, 바꾼 결정, 미해결 TODO 등.

- (2026-06-01, 플랜 세션) 문서 최초 작성. 구현 시작 전. 실제 prod 키/시크릿은 채팅·커밋에 절대 포함 금지(더미로 구조검증, 실키 E2E는 맥/실서버).
- (2026-06-01, Phase 0 조사 세션, 브랜치 `claude/monitor-phase0-compose-baseline-C6XEo`) **확정 사실**: 레포에 base `docker-compose.yml` 없음(gitignore도 아님 = 진짜 부재), `docker-compose.prod.yml`은 self-contained(8서비스, `config --services` exit 0), Makefile `up-prod`는 base 참조로 레포 상태에서 exit 1(`no such file`)로 깨짐. **Phase 1 담당 주의**: postgres/pg-data/depends_on는 `docker-compose.prod.yml`에 직접 추가(네트워크 `appnet`, `env_file: .env.prod`), 검증은 `make`가 아니라 `docker compose -f docker-compose.prod.yml` 직접 실행으로. **맥에서 사람이 확인할 TODO**: (1) prod 실기동이 `make up-prod`인지 `docker compose -f docker-compose.prod.yml` 직접인지, (2) 맥 로컬에 untracked base `docker-compose.yml`이 실제로 있는지, (3) 있다면 Makefile 정합성 수정을 별도 Phase로 진행할지 결정. **코드/설정/Makefile/.env 일체 무수정**(문서만 갱신).
- (2026-06-01, Phase 1 구현 세션, 동일 브랜치 `claude/monitor-phase0-compose-baseline-C6XEo`) **`docker-compose.prod.yml`에 postgres 추가 완료**(image `postgres:16-alpine`, expose 5432 내부전용, `pg-data` 볼륨, `pg_isready` healthcheck, `python.depends_on: postgres(service_healthy)`). 더미 `.env.prod`(가짜값)로 `config --quiet` **exit 0** 확인 후 삭제. **다음 세션(Phase 2) 주의**: ① 기동/검증 시 **`--env-file .env.prod` 필수**(POSTGRES_DB ← ${POSTGRES_DATABASE} 치환 때문, §7 Phase 1 메모 참조). ② Python은 `POSTGRES_DATABASE`/`POSTGRES_USER` 등을 `config.py get_postgres_config()`로 읽음 — `.env.prod`에 `POSTGRES_HOST=postgres`(서비스명) 포함 5개 변수 수동 추가 필요(서버/맥, 실비번은 커밋·채팅 금지). ③ **맥 실검증 TODO**: `make up-prod`(또는 직접 compose+`--env-file`)로 실제 `up` → `pg_isready` healthy → python 컨테이너에서 DB 도달 → 재기동 후 `pg-data` 영속 확인(이 4개가 Phase 1 "검증완료" 승격 조건). ④ Phase 0 Makefile 불일치 미해결 — 맥 실기동 방식 따라 별도 Phase로 수정 결정.

### 각 구현 세션 종료 시 체크리스트
1. 위 표의 해당 Phase 상태·검증결과·브랜치 갱신
2. 인계 메모에 함정/결정/TODO 한 줄 추가
3. `docs/monitoring-plan.md` 포함해서 commit & push
