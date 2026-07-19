# 2026-07-19 장애 수정 로컬 재현·검증 가이드 (§4-3·§4-4)

수정 3건(python /ws 끊김 복구, node STT 일시정지·재개, flutter ws 재시도 예산)을
로컬에서 before/after 로 확인하는 절차. 인수인계 §4-3·§4-4, 전말은
`docs/incidents/2026-07-19-network-blip-ws-rtmp.md` 참조.

## 0. 사전 준비

```bash
# 호스트 터미널에서 (devcontainer 안에서 띄우면 bind mount 경로가 어긋남)
cd ~/Documents/translation/neemba
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d
docker ps   # python/node/nginx/nats/postgres 전부 Up 확인
```

- §4-3 스크립트는 DeepL·NATS 자격증명(.env.dev), §4-4-2 스크립트는 Google STT
  자격증명이 유효해야 한다.
- dev 는 uvicorn `--reload`/`tsx watch` 라 코드 저장 즉시 반영 — before/after 를
  같은 스택에서 `git stash`/`git stash pop` 으로 비교할 수 있다.

## 1. §4-3 — /ws 끊김 시 번역 유실 (자동 판정)

```bash
cd services/python && uv run python ../../scripts/repro_ws_disconnect.py
```

시나리오: 정상 수신(마커 1~3) → TCP abort(close frame 없는 망 순단 모사) →
끊긴 사이 번역 3건(마커 4~6) 생산 → 같은 sessionId 재접속.

| 시점 | 기대 결과 |
|---|---|
| 수정 전 | `[FAIL]` — 마커 4~6 유실, python 로그에 `hub: send failed: Unexpected ASGI message ...` |
| 수정 후 | `[PASS]` — 재접속 직후 마커 4~6 전부 도착 (유실 0) |

수정 후 서버 로그에서 볼 것:

```bash
docker logs python --since 5m 2>&1 | grep -E "client disconnected|queued send|re-queued|flushed"
# hub: client disconnected, waiting for reconnect ...   ← 끊김 '즉시' (30s 지연 없음)
# hub: queued send, ws not connected ...                ← 유실 대신 큐잉
# hub: flushed pending count=N                          ← 재접속 방류
```

단위 수준 재현(도커 불필요, 즉시):

```bash
cd services/python && uv run --extra dev pytest tests/test_ws_disconnect_recovery.py -q
```

## 2. §4-4-2 — STT 좀비화 → 일시정지·재개 (자동 판정, Google 자격증명 필요)

```bash
cd services/python && uv run python ../../scripts/repro_stt_pause_resume.py
```

시나리오: mic 세션 시작 → 무음 PCM 8초 → 소켓 유지한 채 전송만 중단(Google
타임아웃 4연속 유도, 약 1~2분) → 무음 재전송. RTMP 경로와 같은
`StreamOrchestrator` 를 지나므로 검증 대상 코드는 동일하다.

| 시점 | 기대 node 로그 |
|---|---|
| 수정 전 | `Stt giving up: ... stopping rotation` — 이후 오디오가 와도 영구 좀비 |
| 수정 후 | `Stt paused: ... pausing rotation until audio returns` → 재전송 시 `Stt resuming: audio returned` |

단위 수준 재현:

```bash
cd services/node && npx vitest run test/sttErrorBound.test.ts
```

## 3. §4-4-3 — 앱 ws 재시도 예산 (수동, 앱 실행 필요)

1. 앱에서 세션 시작 → 번역 수신 중 폰을 비행기 모드 3~10초 → 해제.
2. 기대: "연결 끊김. 재연결 시도 중... (n/12)" 토스트 후 자동 복구 +
   끊긴 사이 문장들이 한꺼번에 도착(pending flush). 구 코드는 약 3초(2회) 만에
   "연결 복구 실패"로 포기했다.
3. 재시도 예산: 1s 지수 백오프, 30s 상한, 12회 ≈ 271s — 서버(WebSocketHub)의
   5분 재연결 대기 안에서 끝까지 버티도록 맞춤 (`lib/ws_client.dart`).

## 4. 남은 검증 (로컬 범위 밖)

- §4-4-1 앱 RTMP 송출 재연결: mvp 앱에는 RTMP 송출 스택이 없음(외부 송출 앱
  사용 구조) — 송출 주체 확인 후 별도 처리 필요.
- prod 반영은 push → deploy 후 실제 예배 로그로 최종 확인
  (`rtmp on_publish`, keepalive, pending flush).
