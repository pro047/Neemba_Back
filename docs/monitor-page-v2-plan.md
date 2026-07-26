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
  라이브 `ws_blip` 이벤트의 전역 채널(WU1) 발행 여부는 WU5 세션에서 결정.
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
| WU1 | 완료 | 2026-07-25 | pytest 111 통과 + dev compose 이벤트 수신 검증. 커밋 미실행 |
| WU2 | 대기 | — | |
| WU3 | 대기 | — | |
| WU4 | 대기 | — | |
| WU5 | 대기 | — | |

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
