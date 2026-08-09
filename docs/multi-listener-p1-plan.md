# 멀티 청취자 P1 계획 (세션 핸드오프 문서)

작성: 2026-08-08 (긴급 조사 세션)
선행 문서: `docs/session-lifecycle-plan.md` (WU-A auto-stop — 이 계획의 D4 가 그 위에 선다)

---

## 1. 배경 — 무엇이 발견됐나

"여러 사람이 동시에 번역을 듣게 해달라" 는 요청으로 조사에 들어갔다. 현재는
**두 번째 청취자가 붙으면 첫 번째가 끊긴다.** 원인이 두 겹이고, 위쪽이
훨씬 심각하다.

### 근인 1 — 청취자 앱이 곧 세션 개시자다 (더 심각)

`mvp/lib/rtmp_translation_tab.dart:166` 의 [시작] 버튼이 유일한 진입점이고,
그게 `mvp/lib/rest_client.dart:38` 에서 `POST /api/sessions/start` 를 친다.
node 는 이걸 "새 방송 시작" 으로 해석한다 (`SessionLifecycle.ts:111-113`):

```ts
if (currentSessionId) { await teardown("superseded"); }
```

연쇄:

1. 첫 세션 `teardown("superseded")`
2. `handle?.stop()` → **ffmpeg·STT 파이프라인 정지** ← 방송 자체가 끊긴다
3. `stopPythonSession` → `main.py:571` `hub.detach()` → 첫 청취자 소켓 close
4. 새 세션·새 ffmpeg·새 STT 스트림 기동

즉 두 번째 사람은 첫 번째의 소켓을 뺏는 정도가 아니라 **방송 전체를
재시작시킨다.**

### 근인 2 — 허브가 소켓을 1개만 든다

`services/python/src/ws/websocket.py:15`:

```python
self.client: Optional[WebSocket] = None          # 소켓 슬롯 1개
# 동시 1세션 전제: 풀 dict 맵 대신 '지금 슬롯의 주인' sessionId 1개만 추적
self._session_id: Optional[str] = None           # :16-17 세션 슬롯 1개
```

`attach()` `:64-65` 가 같은 sessionId 여도 기존 소켓을 무조건 닫는다:

```python
if self.client and self.client is not ws:
    await self._safe_close(self.client)
```

부속물도 전부 소켓 1개 전제다 — `_send_gate`(`:18`, 전역 세마포어),
`_pending`(`:25`, 세션당 1큐), `_keepalive_task`(`:107`, 허브당 1개),
blip 계측(`:29-36`, "그 세션의 유일한 소켓이 끊김 = 순단").

### 같은 레포에 이미 정답 모양이 있다

`src/ws/monitor.py:24` 의 `MonitorHub` 는 처음부터 세션당 N소켓 팬아웃이다:

```python
self._subscribers: dict[str, set[WebSocket]] = {}
```

**다만 그대로 베끼면 안 된다** — MonitorHub 에는 keepalive·blip·백로그가 없다.

---

## 2. 확정 사실 (2026-08-08 코드 직독)

- **중간 경로는 이미 멀티세션 준비가 돼 있다.** `consumer.py:59`
  `_last_sequence_by_session`, `kss_separator.py:167` `(session_id, segment_id)`
  키별 상태, `pusher.py:47` `_ensured_sessions`. **손댈 게 없다.**
- **`translations` 테이블은 스키마 변경 없이 언어별 행 N개를 받는다.**
  `0001_initial_monitor_schema.py:67` 에 `target_lang` 컬럼이 있고 유니크 제약이
  없다. (P2 대비 확인)
- **`ws_blips` 에는 소켓 식별 컬럼이 없다.** `0002_ws_blips.py:37-53` 의 컬럼은
  `id, session_id, disconnected_at, reconnected_at, duration_ms, flushed_count,
  lost_count, close_code, close_reason, detected_by`. 소켓별 행을 기록하면
  같은 세션의 행 N개가 구분되지 않는다 → D5.
- **Flutter 앱은 응답의 모르는 필드를 무시한다.** `mvp/lib/type.dart:7-12`
  `StartSessionResponse.fromJson` 이 `sessionId`/`webSocketUrl` 만 읽는다.
  **응답에 필드를 추가해도 앱 배포가 필요 없다** (D1 이 성립하는 근거).
- **청취자에게 가는 자막은 평문 문자열이다.** `websocket.py:132-137` 이
  payload 의 `sentence` 만 꺼내 `send_text` 하고, `pusher.py:67` 이 넘긴
  `sequence` 를 버린다. 앱(`mvp/lib/ws_client.dart`)의 `onText` 도 평문 전제다.
  → 백로그 커서를 넣으려면 포맷을 JSON 으로 바꿔야 하고, 그 순간 앱 배포가
  필수가 된다 (D3 이 P1 에서 백로그를 빼는 이유).

### `sequence` 는 커서로 쓸 수 없다 (P3 설계에 중요)

`?since=<seq>` 재개를 검토했으나 **전제가 셋 다 깨진다.**

1. **중복** — `kss_separator.py:266-276` 이 한 flush 의 모든 문장에 같은
   `state.sequence` 를 붙인다. `seq > since` 필터가 나머지 문장을 삼킨다.
2. **건너뜀** — `state.sequence`(`:191`)는 들어온 chunk 의 seq 를 덮어쓰기만
   한다. 여러 chunk 가 쌓였다 한 번에 flush 되면 중간 번호가 클라에 안 간다.
3. **역행** — 상태가 `(session_id, segment_id)` 키별이라, 세그먼트 A 의 늦은
   timeout flush 가 이미 더 큰 seq 를 낸 B 보다 **작은 seq 를 나중에** 보낸다.

→ P3 은 upstream 의 `sequence` 를 쓰지 말고 **전달 계층에서 자체 커서를
발급**해야 한다. 발급과 링버퍼는 한 몸이어야 하므로 별도 객체
(`SessionBacklog`)로 두고 허브가 주입받아 쓴다.

### DeepL 호출이 지금 이벤트 루프를 막고 있다 (P2 선행 조건)

`deepL.py:7-11` 은 매 호출마다 `Translator(api_key)` 를 새로 만들고 동기
블로킹 HTTP 를 친다. `kss_separator.py:207` 이 이걸 **`await` 없이** 부르고,
`_push_loop` 는 태스크 1개라 문장이 직렬 처리된다. **지금도 번역 왕복
시간만큼 파이썬 전체가 멈춘다.**

P2 에서 언어 N개로 fan-out 하면 이 블로킹이 N배가 된다. **언어 fan-out
이전에 `asyncio.to_thread` + `Translator` 인스턴스 재사용으로 풀어야 한다.**
순서를 바꾸면 예배 중에 지연이 터진다.

---

## 3. 확정된 결정

| # | 결정 | 근거 |
|---|---|---|
| D1 | `start` 를 **멱등 join** 으로. 라이브 세션이 있으면 teardown 없이 캐시된 `{sessionId, webSocketUrl}` 반환 | Flutter 앱을 배포하지 않고 당일 나가야 한다 (사용자 결정 2026-08-08) |
| D2 | 허브 키를 처음부터 `(sessionId, targetLang)` 으로. P1 에서는 언어가 1개일 뿐 | P2 가 자료구조를 갈아엎지 않게. 지금 비용은 거의 0 |
| D3 | `_pending` 백로그 **제거**. 순단 구간 자막은 P3 까지 유실 감수 | 커서 없이 멀티 소켓에 백로그를 태우면 붙어 있던 청취자에게 중복 전송이 된다. 커서를 넣으면 포맷이 바뀌어 앱 배포가 필요해진다 (§2) |
| D4 | `POST /api/sessions/stop` **강등** — 200 만 돌려주고 세션을 닫지 않는다 | join 한 청취자가 [정지](`mvp/lib/rtmp_translation_tab.dart:286`)를 누르면 방송이 죽는다. 종료는 `on_publish_done` + 120초 grace 단일 경로로 (release #72 에서 prod 검증됨) |
| D5 | blip 은 **소켓별 행**. `ws_blips` 에 `client_id TEXT NULL` 추가 | 세션당 행이 N개가 되는데 지금 스키마엔 소켓 식별 컬럼이 없다 (§2). nullable 이라 기존 행과 호환되고 롤백은 `drop_column` 한 줄 |
| D6 | 언어별 청취(P2)·백로그 커서(P3)는 이번 범위 밖 | 둘 다 앱 배포가 선행돼야 한다 |

### D4 가 감수하는 것

종료 경로가 `on_publish_done` 하나로 줄어든다. **그 훅이 안 오면 세션을 닫을
수단이 없다** — 지금은 수동 stop 이 그 백업이었다. `close_stale_sessions`
(lifespan startup)와 120초 grace 가 남은 안전망이다. §8 의 검증 항목 2번이
이 판단의 반증 조건이다.

---

## 4. 변경 대상 — 이 목록 밖은 읽기 전용

### node

- `services/node/src/usecases/SessionLifecycle.ts` — D1 join (`webSocketUrl`·
  언어 캐시 추가, teardown·실패 경로에서 **함께** 비울 것), D4
  `stopBySessionId` 가 teardown 을 호출하지 않게
- `services/node/src/router/rtmp.ts` — start 응답에 `joined`·실제
  `sourceLang`/`targetLang` 추가, `/sessions/stop` 은 200 유지하고 로그만
- `services/node/src/monitoring/metrics.ts` — `SessionStopReason` 의 `manual`
  발생처가 사라진다. 라벨은 남기고(기존 시계열 보존) 주석으로 고정
- `services/node/test/sessionAutoStop.test.ts`, `test/rtmp.router.test.ts`

### python

- `services/python/src/ws/websocket.py` — **본체.** `client` →
  `_clients: dict[(session, lang), set[WebSocket]]`, 소켓별 `_Conn`
  (send gate·keepalive task·pong 상태·blip 상태·client_id).
  `_pending`/`_flush_pending`/`_requeue`/`_drop_oldest_pending_locked` 제거
- `services/python/main.py` — `/ws`(`:595`)에서 client_id 발급·lang 해석,
  `/internal/sessions/start` 에서 세션→lang 등록, `/internal/sessions/stop`
  에서 전 소켓 close, `/api/monitor/status`(`:474`)에 청취자 수
- `services/python/src/pushClient/pusher.py` — `broadcast_to_session(...,
  target_lang=)` 전달 (`:59` 에 이미 target_lang 을 들고 있다)
- `services/python/src/ws/blip_recorder.py`,
  `services/python/src/repository/implementation/ws_blip_repository.py` —
  client_id 관통
- `services/python/migrations/versions/0003_ws_blips_client_id.py` — **신규**
- `services/python/src/monitoring/metrics.py` — `neemba_hub_listeners` 게이지
  신설. `set_active_session` 은 **세션이 여전히 1개이므로 유지**
- `services/python/tests/` — `test_ws_disconnect_recovery.py`,
  `test_ws_blips.py`, `test_monitor_status.py` 갱신 + **신규**
  `test_ws_multi_listener.py`

### 읽기 전용 (건드리지 말 것)

node 의 ffmpeg·STT·NATS 경로 전부, `consumer.py`, `separator/kss_separator.py`,
`deepL/deepL.py`, `ws/monitor.py`, `mvp/` 전체.

---

## 5. 구현 순서

1. 마이그레이션 `0003` + repository + recorder 에 `client_id` 관통
   (가장 독립적 — 먼저 끝내고 잊는다)
2. **대조군 테스트를 먼저 작성** — `test_ws_multi_listener.py`: 같은 세션에
   소켓 2개 attach → 브로드캐스트 1회 → 양쪽 다 수신 + 1번 소켓이 여전히
   CONNECTED. **지금 코드로 돌려 실패를 먼저 확인하고 그 출력을 보고할 것.**
   실패하지 않으면 §1 분석이 틀린 것이니 거기서 멈춘다
3. `websocket.py` 재작성
4. `main.py` 배선
5. `metrics.py` + `pusher.py`
6. node D1·D4
7. 나머지 테스트 갱신

---

## 6. 검증 (전부 종료코드 0)

```
cd services/python && uv run pytest
cd services/node && npx tsc -p tsconfig.json --noEmit && npm test
```

허브 경쟁 조건 수동 검증 (docker·NATS·DeepL 불필요, 실 이벤트 루프에서 sleep):

```
cd services/python && uv run python verify_hub_step_a.py
```

### 마이그레이션 — 호스트에서 직접 못 돌린다

**`make up-dev` 로 띄우지 말 것.** Makefile 의 `up-dev`/`up-stage`/`up-prod` 는
전부 base `docker-compose.yml` 을 참조하는데 리포에 그 파일이 없다(`590e5d5`·
`e7344f3` 이후 사라짐, gitignore 도 아님) → `exit 1, no such file`.
기존 미해결 TODO 다 (`monitoring-plan.md` §0, 2026-06-01 Phase 0 조사).

**호스트에서 `uv run alembic` 도 안 된다.** dev postgres 는
`docker-compose.dev.yml` 에서 `expose: 5432` 뿐이라 호스트로 포트가 안 열려
있다 — 컨테이너 네트워크 밖에서는 접속 자체가 안 된다.

dev 스택을 띄울 때는 compose 를 직접 부르고, 마이그레이션은 python 컨테이너
안에서 돌린다 (`services/python` 이 `/app/services/python` 으로 바인드 마운트돼
있어 워킹트리의 `0003` 이 그대로 보인다):

```
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d
docker exec -w /app/services/python python sh -c \
  'alembic upgrade head && alembic downgrade -1 && alembic upgrade head'
```

---

## 7. 반드시 지킬 불변식 — 어기면 예배 중에 터진다

- **락 순서는 `_lock → send_gate` 만 허용.** 역방향은 ABBA 데드락이다
  (`websocket.py:214-216` 주석). `_requeue` 를 게이트 밖에서 부르던 이유가 이것
- **send gate 를 전역으로 두지 말 것.** 소켓 N개에 세마포어 1개면 느린 청취자
  1명이 전원의 자막을 막는다 (head-of-line blocking). 반드시 소켓별
- **연결 검사는 `client_state` 와 `application_state` 를 함께 본다**
  (`websocket.py:38-47`). 하나만 보면 죽은 소켓에 send 를 반복한다
- **`_safe_close` 는 send 와 같은 게이트로 직렬화한다** — 안 그러면
  `send after websocket.close` (에러A)
- **`broadcast_to_session` 의 stale drop 규약을 완화하지 말 것**
  (`websocket.py:141-144`) — 다른 세션의 번역이 새 청취자에게 새는 교차
  전송(에러B)을 막는 장치다
- **`stop_session` 의 멱등성**(`main.py:568-592`)을 깨지 말 것 — 중복 stop 이
  이벤트를 두 번 쏘면 안 된다
- **기존 순단 테스트를 삭제하지 말 것.** `test_ws_disconnect_recovery.py` 의
  백로그 관련 4건은 2026-07-19 장애의 회귀 방지책이다. D3 로 무의미해지면
  지우지 말고 **P3 복원용 skip 마커 + 사유 주석**으로 남긴다

---

## 8. 리스크

1. **순단 자막 유실 회귀 (D3)** — 2026-07-19 장애 대응으로 만든 유실 방지를
   P3 까지 되돌린다. 알고 감수하는 것이며, 회귀 방지 테스트는 skip 으로 보존한다
2. **허브 테스트 726줄이 단일 소켓 전제** —
   `test_ws_blips.py`(540) + `test_ws_disconnect_recovery.py`(186). 통과시키려고
   기대치를 낮추는 게 아니라, 각 테스트가 지키던 불변식(교차 전송 금지,
   stale drop, blip 주인 판정)이 소켓 집합에서도 성립하는지 하나씩 옮겨야 한다.
   **여기가 작업량의 절반이다**
3. **D4 로 수동 종료 수단이 사라진다** — §3 참조. §8 검증 2번이 반증 조건
4. **배포 타이밍** — 릴리스가 node·python·nginx **전 컨테이너를 재생성**하므로
   예배 송출 중에는 머지 금지

---

## 9. 이월 — P2 / P3

### P2 — 청취자별 언어 (앱 배포 필수)

사용자의 최종 요구는 "방송 세션 하나를 열어두고 청취자들이 각자의 언어로
듣는 것" 이다. P1 은 그 절반(같은 언어 여러 명)만 한다.

**갈라져야 할 지점은 딱 한 곳이다.** STT 는 언어와 무관(ko-KR 인식)하므로
`kss_separator.py:207` 의 DeepL 호출만 언어별로 fan-out 하면 된다. 그 위
(node·NATS·consumer·separator 버퍼)는 전부 그대로 쓴다.

```
방송 세션 (sessionId)              ← RTMP → ffmpeg → STT → 문장분리. 언어 무관
   └─ 언어 채널 (sessionId, lang)   ← 여기서만 갈라짐
        ├─ 청취자 소켓 (ja)
        └─ 청취자 소켓 (en)
```

필요한 것:

- `/ws?sessionId=X&lang=ja` — **앱 배포 필수** (P1 을 P2 와 분리한 이유)
- 구독 언어 레지스트리 — 허브가 attach/detach 로 갱신, separator 가 읽는다.
  **아무도 안 듣는 언어는 번역하지 않는다** (DeepL 비용 통제)
- DeepL 논블로킹화 — §2 참조. **fan-out 보다 먼저 해야 한다**
- `InterimChunkOrchestrator` 의 `targetLanguage`(`createStreamOrchestrator.ts:66-72`)
  는 세션 속성에서 빠진다. 하위 호환을 위해 기본값으로 남기고 무시하는 편이 안전

**비용**: DeepL 은 번역 문자 수 과금이라 언어 3개면 문자 수 3배다.

### P3 — 백로그 커서 (앱 배포 필수)

- `SessionBacklog` 객체 — 커서 발급 + 링버퍼(`maxlen=100`). 소켓을 모르는 순수
  자료구조라 단위 테스트가 쉽다. 허브가 주입받아 쓴다
- 전송 포맷 평문 → `{"sequence": n, "sentence": "..."}` JSON
- `/ws?...&since=<마지막 seq>` — 없으면 백로그 없이 현재부터
- **upstream 의 `sequence` 를 쓰지 말 것** (§2 — 중복·건너뜀·역행)
- 링버퍼와 커서는 **언어 채널별**이다 (P2 이후)
- D3 으로 skip 처리한 순단 테스트 4건을 여기서 되살린다

---

## 10. 배포·검증

배포 후 첫 주일예배에서 확인할 것:

1. **기기 2대가 동시에 자막을 받는가** (P1 의 목표)
2. **OBS 종료 후 2분 안에 `neemba_hub_active_session` 이 0으로 떨어지는가**
   — D4 로 수동 stop 백업이 사라졌으므로 이게 유일한 종료 경로다.
   안 떨어지면 즉시 `POST /internal/sessions/stop` 으로 수동 회수하고 D4 를
   재검토한다

`docs/handover-2026-08-02.md` §2 의 기존 8/9 검증 3종(WU-F F-1·F-2, V3-A)과
같은 예배에서 함께 본다.
