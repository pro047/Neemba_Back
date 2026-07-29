# /monitor 페이지 v2 — 실시간 모니터링 개선 계획 (세션 핸드오프 문서)

> 이 문서는 여러 Claude 세션에 걸친 작업의 단일 연결 고리다.
> **모든 세션은 시작 시 이 문서 전체를 읽고, 자기 작업 단위가 끝나면
> §6 상태 테이블과 §7 세션 로그를 갱신한 뒤 종료한다.**
> 작업 단위 하나 = 세션 하나. 다음 단위는 반드시 새 세션에서 시작한다.

작성: 2026-07-25 · 발주자: jinseong

---

## 1. 배경 / 문제 정의

`/monitor` 페이지(`infra/nginx/html/monitor/` + python `/api/monitor/*`, `/ws/monitor`)는
세션 내부 번역 스트림은 실시간(WS fan-out)이지만, **라이브 세션 감지 자체는 수동**이다.

현재 한계 (2026-07-25 분석 결과):
- 세션 목록은 페이지 로드 1회 + 수동 새로고침뿐. `session_started` 전역 이벤트 부재.
- `MonitorHub`(`services/python/src/ws/monitor.py`)는 sessionId 단위 구독만 지원.
- 라이브 모드는 세션 클릭 → "라이브 시작" 버튼의 2단계 수동 opt-in.
- WS 끊기면 자동 재연결 없음, 끊긴 동안 번역은 화면 유실 (백로그/리플레이 의도적 부재).
- broadcast가 DB insert **전**에 나가서(`src/pushClient/pusher.py:118~120`)
  payload에 `id`/`createdAt`이 없음 → 이력↔라이브 dedup·gap fill 불가.
- stop 유실 시 `ended_at` NULL 영구 잔존 → 유령 LIVE 배지.
- start 시 `ensure_session` 실패가 무시됨(`main.py` start 핸들러) →
  번역 0건 세션은 목록에 안 뜰 수 있음.
- 시스템 상태(NATS 연결, STT paused 등) 표시 없음 — Discord 사이드카 경보 후
  1차 상황 파악을 이 페이지에서 못 함.

## 2. 확정된 결정 사항 (사용자 승인 완료)

| # | 결정 | 내용 |
|---|------|------|
| D1 | 세션 감지 | **전역 WS 이벤트 채널** `/ws/monitor/events` 신설 (폴링 아님) |
| D2 | 범위 | 상태 개요(WU5)까지 전부 포함 |
| D3 | broadcast 순서 | **DB insert 후 broadcast** — payload에 `id`, `createdAt` 포함. 단 insert 실패 시 id 없이 broadcast하는 폴백 유지(라이브가 DB 장애에 볼모 잡히지 않게) |
| D4 | 유령 LIVE | **(c) 둘 다**: python 기동 시 `ended_at IS NULL` 일괄 종료 스탬프 + 프런트 STALE 배지(무활동 180s, 사이드카 `MONITOR_GAP_THRESHOLD`와 정렬) — 확정(2026-07-25) |
| D5 | 라이브 구독 | live 세션 선택 시 **자동 구독** (중지 버튼은 유지) |
| D6 | 프런트 스택 | 바닐라 JS → **TypeScript 마이그레이션** |
| D7 | TS 빌드 | `typescript`만 devDependency로 두고 `tsc`로 단일 `app.js` 출력, **산출물 커밋**. 번들러 없음. prod는 html 디렉터리 그대로 마운트하므로 배포 무변경. 소스·tsconfig·package.json은 `infra/nginx/html/monitor/` 하위에 배치 |

불변 제약:
- 렌더링은 `textContent` 전용 유지 (innerHTML 금지, XSS 방지).
- 모니터 노출 텍스트는 기존대로 masked (mask-at-write 유지).
- `/monitor`·`/api/monitor/`·`/ws/monitor*` nginx Basic Auth 유지. 신규 WS 경로도 동일 보호.
- 경보 **판정**은 사이드카(`infra/monitor/monitor.py`) 책임으로 남긴다.
  페이지는 현재값 **표시**만 (중복 판정 로직 금지).
- 캡처 실패가 클라이언트 번역 브로드캐스트 경로를 막으면 안 된다(기존 원칙).

## 3. 작업 단위 (WU) — 각각 별도 세션

### WU1 — [python] 전역 이벤트 채널
- `MonitorHub`에 전역 구독자 셋 추가 (sessionId 무관 fan-out).
- `GET /ws/monitor/events` WS 엔드포인트 신설 (read-only, 인바운드 무시).
- 이벤트 발행:
  - `/internal/sessions/start` → `{type:"session_started", sessionId, sourceLang, targetLang, startedAt}`
  - stop 핸들러(최초 1회 ended 전이 시에만) → `{type:"session_ended", sessionId, translationCount, endedAt}`
- nginx: `/ws/monitor/events` 라우팅 + Basic Auth (prod·dev conf 모두).
- 테스트: MonitorHub 전역 fan-out 단위 테스트 (기존 `tests/` 패턴 따름).
- 완료 기준: pytest 통과, dev compose에서 wscat 등으로 start/stop 이벤트 수신 확인.

### WU2 — [python] 캡처 경로 개편 + 세션 정합성
- `pusher._capture`: insert → 반환된 `id`(+`created_at`)를 payload에 넣어 broadcast.
  insert 실패 시 id 없이 broadcast 폴백 (D3).
  `insert_translation`이 id를 반환하도록 쿼리 수정 (`RETURNING id, created_at`).
- `ensure_session` 실패 시 1~2회 재시도 + 실패 카운터 메트릭 추가.
- python lifespan 기동 시: `ended_at IS NULL` 세션 일괄 종료 스탬프 (D4-a).
- 세션 목록 쿼리에 `last_translation_at`(MAX created_at) 추가 →
  `/api/monitor/sessions` 응답에 `lastTranslationAt` 필드 (STALE 배지·미니 통계용).
- 테스트: 캡처 순서·폴백·reconciliation 단위 테스트.
- 완료 기준: pytest 통과, `/ws/monitor` payload에 id/createdAt 확인.

### WU3 — [프런트] TypeScript 마이그레이션 (동작 불변)
- `app.js` → `app.ts` 포팅. **기능 추가 금지** — 순수 마이그레이션.
- 빌드 셋업은 §5 Q1 답변에 따름 (문서 갱신 후 진행).
- 백엔드 계약 타입 정의 (SessionItem, TranslationPair, WS 메시지 유니온 등).
- 완료 기준: 빌드 산출물이 기존과 동일 동작 (세션 목록/이력/라이브/검색 수동 확인),
  `tsc --noEmit` 무오류.

### WU4 — [프런트] 실시간 기능 (WU1·2·3 완료 후)
- 이벤트 채널 상시 연결: `session_started` → 목록 상단 삽입+LIVE 배지,
  `session_ended` → 배지 전환·건수 갱신. 이벤트 채널 자체도 자동 재연결.
- live 세션 선택 시 자동 `startLive()` (D5).
- 세션 WS 자동 재연결(지수 백오프, 예: 1s→2s→4s… 최대 30s) +
  재연결/라이브 시작 시 마지막 수신 `id` 이후 이력 조회로 gap fill, id 기반 dedup.
- STALE 배지: `lastTranslationAt`이 180s 초과 && live → "STALE" 표시 (D4-b).
- 완료 기준: dev compose에서 세션 start→목록 자동 등장, stop→배지 전환,
  WS 강제 절단 후 재연결·gap fill 수동 확인.

### WU5 — [python+프런트] 시스템 상태 개요
- `GET /api/monitor/status`: python 자체 게이지(active_session, nats_connected,
  last_broadcast 경과) 직접 + node `/metrics` 내부 HTTP로 수집(stt_paused,
  rtmp_auth_enabled, buffer_size) → 통합 JSON. node 수집 실패는 `nodeUp:false`로 표시.
- 프런트 상단 상태 칩 바 (10s 폴링). 세션 상세에 분당 번역 건수·마지막 수신 경과 표시.
- **순단 이력 섹션 (2026-07-26 추가 확정)**: `ws_blips` 테이블 조회로 /ws 순단 이력
  (끊긴 시각·지속 초·flush/유실 건수·close code) 표시. 데이터 기록+조회 API는
  handover §4-7 소PR(이 계획 밖, monitor-v2보다 먼저)에서 선행 — WU5는 화면 노출만.
  라이브 `ws_blip` 이벤트의 전역 채널(WU1) 발행 여부는 WU5 세션에서 결정
  → **발행 안 함 확정(2026-07-27)**, REST 조회만. **구현 완료(2026-07-27)**.
- 완료 기준: dev compose에서 상태 칩 정상 표시, node 컨테이너 중지 시 nodeUp:false 확인,
  ws_probe 강제 절단으로 순단 이력 행 등장 확인.

의존 관계: WU1 → WU2 → WU3 → WU4 → WU5 (WU3은 WU1·2와 병행 가능하나
세션 분리 원칙상 순차 진행).

## 4. 세션 운영 규칙

1. 새 세션 시작 프롬프트 예시:
   `neemba/docs/monitor-page-v2-plan.md 읽고 WU<n> 진행해`
2. 세션은 자기 WU 범위를 벗어나는 수정 금지. 범위 밖 발견 사항은 §7에 메모만.
3. WU 완료 시: §6 상태 갱신, §7에 한 줄 로그(변경 파일·검증 결과·다음 세션 주의사항).
4. 커밋 여부·시점은 각 세션에서 사용자에게 확인 (되돌리기 어려운 결정 게이트).
5. 검증(테스트·수동 확인)은 해당 WU 세션 내에서 수행하되,
   전체 통합 리뷰는 WU5 이후 별도 세션에서.

## 5. 열린 질문

- 없음. 모든 결정(D1~D7) 확정 — 2026-07-25.

## 6. 진행 상태

| WU | 상태 | 세션 일자 | 비고 |
|----|------|----------|------|
| WU1 | 완료 | 2026-07-25 | pytest 111 통과 + dev compose 이벤트 수신 검증. PR #55 머지, 2026-07-26 release #57로 prod 배포 완료 |
| WU2 | 완료 | 2026-07-26 | pytest 135 통과(테스트 DB 도입) + dev compose에서 payload id/createdAt·lastTranslationAt·D4-a 일괄 종료 검증 |
| WU3 | 완료 | 2026-07-26 | `tsc --noEmit` 무오류 + 헤드리스 Chrome으로 목록/이력/라이브/검색 4시나리오 검증 |
| WU4 | 완료 | 2026-07-26 | `tsc --noEmit` 무오류 + 헤드리스 Chrome 15개 시나리오 검증 (start 자동 등장·자동 라이브·재연결 gap fill·STALE·이벤트 채널 복구) |
| WU5 | 완료 | 2026-07-27 | pytest 145 통과 + dev compose 검증: 칩 표시·node 중지→nodeUp:false·/ws 절단→순단 이력 행·라이브 분당 건수 (헤드리스 Chrome 23개 체크). 2026-07-29 코드 리뷰 지적 2건 반영 후 커밋 |

## 7. 세션 로그

- 2026-07-25 (계획 세션): 현황 분석·결정 D1~D7 확정·본 문서 작성. 코드 변경 없음.
  다음 세션: `neemba/docs/monitor-page-v2-plan.md 읽고 WU1 진행해`
- 2026-07-25 (WU1 세션): TDD로 구현. 변경: `services/python/src/ws/monitor.py`
  (attach_global/detach_global/broadcast_global), `services/python/main.py`
  (`/ws/monitor/events` WS + start/stop 이벤트 발행, 타임스탬프는 서버 UTC now),
  `tests/test_monitor_hub_global.py` 신규(7개), nginx prod·dev conf 주석만
  (기존 `location /ws/monitor` prefix가 events 경로 커버 — 라우팅 변경 불필요).
  검증: pytest 111 통과, dev compose에서 start→`session_started`,
  stop→`session_ended`, 중복 stop→무발행 확인. 다음 세션 주의: dev python
  컨테이너의 `docker logs`가 2026-07-18에서 멈춰 있음(프로세스는 정상,
  uvicorn reload도 동작) — 로그 드라이버 이상 의심, WU 범위 밖이라 미조치.
  다음 세션: `neemba/docs/monitor-page-v2-plan.md 읽고 WU2 진행해`
- 2026-07-26 (WU2 세션): 캡처 경로 개편 + 세션 정합성. 변경:
  `translation_repository.py`(insert `RETURNING id, created_at` 반환,
  `ensure_session_with_retry`(2회 재시도·백오프 0.1s→0.2s),
  `close_stale_sessions`(D4-a, translation_count 재계산 포함)),
  `pusher.py`(`_capture` 순서 역전: insert→broadcast, payload에 id/createdAt
  — insert 실패·pool 부재 시 null로 항상 포함(안정 shape)),
  `metrics.py`(`neemba_ensure_session_failed_total`, 실패 시도당 1 증가),
  `main.py`(lifespan 기동 시 stale 세션 일괄 종료, start 핸들러 재시도 적용,
  `MonitorSession.lastTranslationAt`),
  `monitor_query_repository.py`(세션 목록에 `last_translation_at` 상관 서브쿼리).
  테스트: **테스트 DB 도입**(사용자 결정) — `tests/conftest.py`가 일회용
  `postgres:16-alpine` 컨테이너(호스트 54329) 기동 + alembic upgrade head,
  테스트마다 TRUNCATE. 장애 주입만 실 pool 래퍼, 성공 경로는 실 DB.
  `tests/test_capture_db.py` 신규(11개). 검증: pytest 135 통과, ruff 클린
  (main.py I001·F841 2건, pusher.py I001은 기존 위반으로 미조치), dev
  compose에서 `/ws/monitor` payload에 id=222/createdAt 수신,
  `lastTranslationAt` 일치, reload 기동 시 유령 세션 4건 일괄 종료 확인.
  범위 밖 발견: 동일 sessionId 재사용(start) 시 `ensure_session`이
  DO NOTHING이라 이미 종료된 세션의 `ended_at`이 NULL로 리셋되지 않음 →
  재사용 세션은 live로 안 보이고 stop도 no-op(`ended:false`). WU4 LIVE
  배지·WU5에서 영향 검토 필요. 다음 세션:
  `neemba/docs/monitor-page-v2-plan.md 읽고 WU3 진행해`
- 2026-07-26 (WU3 세션): TS 마이그레이션 (관용적 TS 스타일, 동작 불변).
  변경: `infra/nginx/html/monitor/src/app.ts` 신규(계약 타입 포함 — WU2의
  id/createdAt/lastTranslationAt도 타입에 선언, 사용은 WU4),
  `tsconfig.json`(ES2022·strict·`types:[]`·outDir `.`), `package.json`+lock
  (typescript ^5.9.2만), `app.js`는 이제 빌드 산출물(직접 수정 금지,
  `npm run build`). 세션 시작 시 브랜치 정리: WU2 브랜치를 develop에
  ff-merge·push 후 피처 브랜치 삭제(사용자 지시), WU3 브랜치는 develop에서
  분기. 검증: `tsc --noEmit` 무오류, 헤드리스 Chrome(puppeteer-core)으로
  ① 세션 목록 27개 렌더 ② 이력 8행 ③ 라이브 WS 연결·`session_closed` 전이
  ④ 검색 6행 + 콘솔 오류 없음(favicon 404는 기존과 동일) 확인.
  다음 세션 주의: dev nginx 컨테이너에는 html 마운트·htpasswd가 없어
  `/monitor` 페이지를 nginx 경유로 못 봄 → 검증은 socat 브리지
  (`docker run --rm -d --name wu3-py-bridge --network neemba_appnet -p
  18000:8000 alpine/socat tcp-listen:8000,fork,reuseaddr tcp:python:8000`)
  + 로컬 정적/프록시 서버로 수행했음(WU4도 동일 방법 권장). 검증용 세션
  `wu3-verify-live-01`(종료됨)이 dev DB에 남아 있음. 다음 세션:
  `neemba/docs/monitor-page-v2-plan.md 읽고 WU4 진행해`
- 2026-07-26 (WU4 세션): 프런트 실시간 기능 (백엔드 무변경). 변경:
  `infra/nginx/html/monitor/src/app.ts`(세션 목록 `Map<sessionId, 행>` 리팩터,
  공용 재연결 WS 헬퍼(백오프 1s→2s→…→30s, open 시 리셋), `/ws/monitor/events`
  상시 구독 — started→상단 삽입+LIVE(기존 행이면 LIVE 전환+상단 이동),
  ended→배지·건수·상세 메타 갱신, 재연결 성공 시 목록 REST 1회 재조회,
  D5 자동 라이브(선택 시 `startLive`, 이력도 syncLive가 처음부터 로드),
  세션 WS 재연결+gap fill(`cursor=마지막 수신 id`로 nextCursor 소진까지,
  `seenIds` dedup, fill 중 라이브 수신은 버퍼링 후 flush), STALE 배지
  (목록 행+상세 헤더, 10s 타이머 재평가, 선택 세션은 라이브 수신 즉시 갱신)),
  `style.css`(`--stale`·`.badge.stale`), `app.js` 재빌드. 검증: `tsc --noEmit`
  무오류, socat 브리지+로컬 WS 프록시(강제 절단/차단 스위치 포함)+헤드리스
  Chrome으로 15개 시나리오 전부 통과 — start→목록 자동 등장, 선택→자동
  라이브·수신, WS 차단 중 2건 주입→재연결 후 gap fill 정확 행수(중복 없음),
  stop→배지·건수 전환, 이벤트 채널 차단 중 start→복구 시 재조회로 등장·선택
  하이라이트 유지, STALE(목록·상세). NATS 직접 주입(`transcript.session.*`)으로
  실 번역 파이프라인 사용. 범위 밖 발견: 없음(WU2의 sessionId 재사용 이슈는
  결정 2대로 이벤트 신뢰로 처리, 백엔드 미조치 그대로). 검증용 세션
  `wu4-live-*`(모두 종료)가 dev DB에 잔존, STALE 검증용 세션은 삭제함.
  커밋 전 코드 리뷰(워크플로)에서 실버그 6건 발견·수정: ① 전역 session_ended가
  라이브 재연결 루프 미중단(끊긴 사이 종료 시 무한 재연결) → 전역 이벤트·재조회
  경로에서 stopLive ② 재조회가 loading 중이면 복구 재조회 소실 → reloadQueued
  ③ 재조회 병합으로 종료 판명 시 상세 헤더 "진행중" 고착 → 헤더 재렌더
  ④ sessionId 재시작 시 이전 런 lastTranslationAt로 STALE 오탐 → null 리셋
  ⑤ gap fill 행이 lastTranslationAt 미갱신 → fill에서 갱신 ⑥ 같은 세션 빠른
  재선택 시 이전 fill 루프 늦은 fetch 개입 → epoch 가드. 수정 후 회귀 시나리오
  (WS 절단 중 stop→전역 이벤트로 라이브 중단) 추가해 15/15 재통과. 다음 세션:
  `neemba/docs/monitor-page-v2-plan.md 읽고 WU5 진행해`
- 2026-07-27 (WU5 계획 세션): 코드 변경 없음. 탐색 결과 §4-7 소PR(ws_blips
  기록+`GET /api/monitor/ws-blips`)은 PR #56으로 선행 완료 확인. 사용자 결정
  4건: ① `ws_blip` 라이브 이벤트 전역 채널 발행 안 함(REST 조회만) ② 활성 칩은
  live 세션 수 + 자막기기 `/ws` 연결 여부 둘 다 ③ 순단 이력은 별도 탭(진입 시
  로드, 상시 폴링 없음) ④ 분당 번역 건수는 프런트 파생(최근 60s 수신 행
  카운트). 승인된 상세 계획은 `.claude/handoff.md`. 다음 세션:
  `neemba/docs/monitor-page-v2-plan.md 읽고 WU5 진행해` (구현은 /implement)
- 2026-07-27 (WU5 구현 세션): 시스템 상태 개요. 변경 — python:
  `src/monitoring/metrics.py`(setter가 Prometheus 게이지와 함께 모듈 스냅샷
  dict에도 기록 + `get_snapshot()`, 시그니처 불변),
  `src/monitor/node_metrics.py` 신규(httpx 2s 타임아웃, 사이드카 방식 라인
  파싱으로 stt_paused·rtmp_auth_enabled·publish_buffer_size 추출, 모든 실패
  None, env `NODE_METRICS_URL`), `monitor_query_repository.py`
  (`count_active_sessions`), `src/ws/websocket.py`(`is_client_connected()`
  읽기 전용 접근자 — 계획 변경 목록 밖 3줄 추가, wsClientConnected용),
  `main.py`(`GET /api/monitor/status` + NodeStatus/MonitorStatusResponse,
  node 실패 시 nodeUp:false·node:null·나머지 정상), `tests/test_monitor_status.py`
  신규(8개 — 스냅샷·파서·count는 실 DB). 프런트: `index.html`(칩 바 +
  순단 이력 탭), `src/app.ts`(StatusResponse/WsBlip 타입, 10s 폴링 칩 렌더,
  상세 미니 통계 — 분당 건수는 detail.recentTimes(수신 행 createdAt) 60s 필터,
  종료 세션 "분당 —", 순단 이력 탭 offset 페이지네이션), `style.css`(.chip),
  `app.js` 재빌드. 검증: pytest 143 통과, ruff·mypy 신규 유입 0,
  `tsc --noEmit` 무오류. dev compose(socat 브리지+헤드리스 Chrome):
  칩 7종 표시, node 중지→"node 응답 없음" 칩(나머지 칩 유지), /ws 1001 절단→
  재접속으로 순단 행(1.0s·flush 0·유실 0·1001·client_disconnect) 탭 표시,
  NATS 주입 라이브에서 자막기기 연결 칩·live 세션 1개·분당 2건·마지막 수신
  경과 표시. 참고: dev DB에 마이그레이션 0002 미적용 상태였음(컨테이너가
  머지 전 기동) → `docker exec python alembic upgrade head`로 적용. 검증용
  세션 wu5-verify-blip-01·wu5-live-01·wu5-live-02(모두 종료)와 ws_blips 1행
  dev DB 잔존. 커밋·release PR은 사용자 확인 대기.
- 2026-07-29 (WU5 리뷰·커밋 세션): 커밋 전 코드 리뷰(에이전트 27개, 후보 33건
  → 반증 5건 → 확정 10건). 표시 계층 결함뿐이라 배포를 막을 건 없다고 판단,
  "상태가 거짓말하는" 2건만 반영하고 나머지 8건은 후속 과제로 넘김.
  ① `natsConnected` 거짓 초록 — `consumer.py`가 `nats.connect()` 직후 플래그를
  세워, JetStream 준비가 실패해도 True로 남았다(연결 자체는 살아 있어 nats-py
  콜백이 안 뜬다). 스트림·구독 준비 성공 후로 옮기고 실패 시 False. **§4-2
  범위 규칙의 의도적 예외** — 근인이 WU5 밖 파일이라 거기서만 고칠 수 있었다.
  ② `main.py`의 맨 `int(buffer_raw)`·로컬 `_gauge_bool` 제거하고 이미 있던
  `node_metrics.gauge_int`/`gauge_bool`(math.isfinite 가드) 사용 — node가
  NaN/+Inf를 내면 라우트가 500이 되어 nodeUp:false 열화 설계가 무너졌다.
  테스트 2개 추가(145 통과), ruff 53 = develop 기준선 동일. 리뷰 잔여 8건:
  순단 탭 새로고침 실패 시 nextOffset 미초기화·OFFSET 페이지네이션 중복 행,
  activeSessions가 버려진 세션 포함, 10s 폴링 in-flight 가드·teardown 부재,
  캐시 스큐 시 init 예외로 페이지 백지화, nodeUp이 임의 2xx를 healthy로 판정,
  폴링 1회 실패에 칩 전체 소거, 스크레이프 실패 무로깅.
