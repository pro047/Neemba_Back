# 청취자 복귀 매칭을 기기 id 일치로만 (listener-alert-exact-match)

작성 2026-11-28 · 요구사항 정본 (사람 결정 + 실측·코드 확인). 선행: `docs/prd/listener-event-alert.md` (같은 브랜치, 커밋 7dff350)

## 배경
- listener-event-alert 는 끊김·접속 순간 Discord 알림을 보낸다. 재접속이 열린 끊김(blip)과 짝지어지면 "🟢 복귀" 를 보낸다.
- 짝짓기 `WebSocketHub._match_open_blip_locked` (`services/python/src/ws/websocket.py`) 는 client_id 가 같은 blip 을 먼저 찾고, 없으면 **그 세션의 가장 오래된 열린 blip 을 꺼낸다**(2026-08-08 사용자 결정 — 앱이 id 를 안 보내던 P1 용).
- 그래서 새 사람이 들어와도 남의 끊김을 가져가 "복귀" 로 보인다. dev 실측(2026-11-28): 처음 접속한 클라이언트 L3·L5 가 `🟢 복귀 … (끊긴 소켓 0248cb3c)` 로 찍혔다.
- 앱(`mvp`)이 이제 `/ws?sessionId=…&clientId=<installId>` 를 보낸다 — prod 실측 2026-11-28T06:22:03Z `"WebSocket /ws?sessionId=c4616f79-…&clientId=5fb2fcee-1e9f-4815-b8d1-8d1a593cb95e" [accepted]`, `ws_blips` 에 `5fb2fcee-…|1005` 행. installId 는 기기 단위 영속 값이고 재연결 사이에 유지된다(mvp 세션 코드 확인). 진단 디렉터리 실패 시 앱은 clientId 를 빼고 보낸다.
- 서버는 query clientId 를 이미 쓴다: `main.py` `client_id=ws.query_params.get("clientId")` → `websocket.py` `cid = client_id or uuid.uuid4().hex`.

## 확정 결정 (사람, 2026-11-28)
1. **접속이 clientId 를 보냈으면** 같은 client_id 의 열린 blip 만 짝짓는다. 없으면 대체 매칭 없이 "🟢 청취자 접속" 으로 처리하고, 남의 blip 은 건드리지 않는다.
2. **대체 매칭(가장 오래된 blip 꺼내기)을 완전히 없앤다** (사람 결정 2026-11-28 — "우연히 맞는 것"). clientId 가 없는 접속(구버전 앱·진단 꺼진 기기)은 서버 발급 id 라 어떤 blip 과도 짝지어지지 않고 "🟢 청취자 접속" 으로 처리된다. 그 기기의 blip 은 세션 종료 시 미복귀로 마감된다 — 업데이트 전 기기가 남아 있는 동안 `ws_blips.reconnected_at`·`duration_ms` 가 비는 것을 받아들인다. 2026-08-08 결정을 뒤집는다.
3. 같은 client_id 의 새 접속이 올 때, 그 client_id 의 **옛 소켓이 아직 명부에 남아 있으면**(서버가 끊김을 아직 모름 — 1006 은 keepalive 로 최대 60초 뒤 감지) 옛 소켓을 **교체된 것으로 정리**한다. 목표: 이미 돌아온 기기에 대해 나중에 "🔴 순단" 이 오거나 미복귀 blip 이 남는 일이 없어야 한다.
5. 교체 시 ws_blips 행을 남긴다 — 옛 소켓 blip 을 열자마자 새 소켓으로 닫아 기존 "🟢 복귀" 문구 + ws_blips 행(끊긴 시간 ≈0초) 1건 (이전 설계 게이트 D:141 답 아니오).
6. 같은 clientId 의 열린 blip 이 여럿이면 가장 오래된 1건만 닫고 나머지는 열어 둔다 (이전 설계 게이트 D:143 답 예).
4. 업데이트 전(서버 발급 id)과 후(installId)가 섞이는 첫 재접속은 같은 기기라도 "접속" 으로 보인다 — 서버에 이을 재료가 없어 받아들인다 (prod 예: `2a8fd8f7`(06:14:29 1005) 와 `5fb2fcee`(06:22:03) 는 같은 폰이지만 구분 불가).

## 설계에서 정할 것 ([결정] 으로 묻는다)
- "clientId 를 보냈는가" 를 hub 가 어떻게 아는지 (attach 인자 None 여부 등) — 대체 매칭이 없으므로 짝짓기에는 영향이 없고 교체(결정 3) 판정에만 쓰인다

## 완료 조건
- 결정 1~3 이 단위 테스트로 고정된다 (clientId 있음·없음·교체 각각, 대체 매칭이 어떤 경우에도 일어나지 않음).
- dev 실물: clientId=X 접속 → 끊기 → X 재접속 = "🟢 복귀 X" · 다른 clientId=Y 접속 = "🟢 청취자 접속" (X 의 blip 은 열린 채) · clientId 없는 접속은 끊긴 뒤 재접속해도 "🟢 청취자 접속" (대체 매칭 없음) · X 가 끊김 감지 전 재접속(교체) 시 뒤늦은 "🔴 순단" 없음.

## 하지 않는 것
- 앱 수정 · 스키마 변경 · monitor 사이드카 변경
- 업데이트 전 서버 발급 id 와 installId 를 잇는 것
