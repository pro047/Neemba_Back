# 청취자 이벤트 실시간 알림 (listener-event-alert)

작성 2026-11-28 · 요구사항 정본 (사람이 대화에서 정한 결정 + 코드 확인 사실)

## 목적
운영자(1명)가 예배 중 **앱의 현재 상황을 실시간으로** Discord 에서 본다. 기존 monitor 사이드카 규칙(heartbeat·청취자 0명, PR #121)은
"이상이 확정됐을 때 1번" 경보라 N초 지연·묶음·억제가 있어 이 목적에 맞지 않는다 (2026-11-28 사람 판정).

## 확정 결정 (사람)
1. 알림은 **사건이 일어난 순간** 보낸다 — 끊김은 끊긴 순간, 접속은 붙은 순간.
2. 보내는 주체는 **python hub** 다. monitor 사이드카는 `MONITOR_INTERVAL` 마다 `/metrics` 숫자만 읽어(`infra/monitor/monitor.py` fetch_metrics) 누가 붙었는지 모르고, 간격 사이의 끊김→재접속(1→0→1)을 못 본다.
3. 분류 (끊김 시 `close_code`·`detected_by` 기준):

   | 조건 | 알림 |
   |---|---|
   | 새 청취자 접속 (`attach`, 열린 blip 매칭 없음) | 🟢 청취자 접속 |
   | 같은 기기 재접속 (`attach` 에서 열린 blip 매칭됨) | 🟢 복귀 — 끊긴 시간·유실 건수 (`ws_blips` 의 `duration_ms`·`lost_count`) |
   | `close_code` 1006 · 1001 · `detected_by=keepalive_timeout` | 🔴 순단 |
   | `close_code` 1005 | 🟠 앱이 닫음 (정지인지 순단인지 구분 불가) |

4. **1005 도 알린다** (사람 선택 2026-11-28). 앱(`mvp` 리포)의 두 close 호출에 코드 인자가 없어 [정지]·화면 닫기·앱 내부 재연결 정리가 전부 1005 로 온다(핸드오프 큐 13). 앱이 코드를 붙이기 전까지는 구분 불가 문구로 보낸다.
5. 웹훅은 기존 `DISCORD_WEBHOOK_URL` 을 쓴다 (prod python 은 이미 `.env.prod` 를 env_file 로 읽는다 — `docker-compose.prod.yml` python 서비스). 웹훅이 비어 있으면 로그만 찍는다.
6. 새 패키지 추가 없음 — `httpx` 는 이미 의존성이다 (`services/python/pyproject.toml`).
7. 알림 전송 실패·지연이 hub 동작(브로드캐스트·attach·drop)을 막거나 늦추면 안 된다.

## 사실 (코드 확인, 2026-11-28 develop a796431)
- 끊김 지점: `WebSocketHub._drop_conn` (`services/python/src/ws/websocket.py`) — close_code·close_reason·detected_by 를 받고 `_open_blip_locked` 로 순단을 연다.
- 접속 지점: `WebSocketHub.attach` — `_match_open_blip_locked(session_id, cid)` 가 같은 기기 재접속을 잡고 `_finish_blip` 이 blip 을 닫는다.
- close code 뜻: 1001 은 정상 종료 아님(앱 마일스톤 A 이후 ping 무응답 시 앱이 1001 로 닫고 재연결), 1005=코드 없는 close, 1006=close frame 없는 단절 (`handle_client_disconnect` 독스트링).
- 실전 빈도(핸드오프 큐 13, 실측): 1006 이 9/20 예배 7회, 10/11 15회. 11/22 python 로그 01:54~02:52 UTC 의 `listener dropped` 4건은 전부 예배 끝(02:49~02:51) `client_disconnect`.
- dev 웹훅은 prod 와 같은 채널이다(`.env.dev`). 받는 사람은 운영자 1명이라 dev 알림이 섞여도 된다(사람 확인).

## 설계에서 정할 것 (미정 — [결정] 으로 묻는다)
- `close_code` 1000(앱이 코드 1000 으로 닫음)·4410(세션 종료로 서버가 닫음)·그 밖의 코드·`detected_by` 가 `client_disconnect`/`keepalive_timeout` 외인 경우(/ws 예외 경로)를 알릴지, 어떤 문구로
- 예배 끝처럼 끊김이 몰릴 때 묶을지 (Discord 웹훅 전송 제한 수치는 미확인)
- 메시지에 넣을 식별자 (세션 id 앞 8자·client id·남은 청취자 수 등) — 개인정보(IP 등)는 넣지 않는다
- PR #121 의 monitor `no_listeners` 규칙과의 관계 (중복 알림이 되는지)

## 완료 조건 (사람 승인)
- 위 분류 표가 `close_code`/`detected_by` 별 단위 테스트로 고정된다.
- dev 에서 1006·keepalive_timeout·1005·재접속을 각각 재현해 Discord 에 알림이 1건씩 온다 (사람 실물 확인).

## 하지 않는 것
- 앱(`mvp`) 수정 (1005 구분은 큐 13 앱 작업)
- monitor 사이드카 규칙 변경
- 스키마 변경
