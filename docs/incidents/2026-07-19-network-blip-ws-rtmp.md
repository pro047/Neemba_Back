# 2026-07-19 예배 중 장애: 폰 네트워크 순단 → ws 유실 + RTMP 송출 영구 중단

## 타임라인 (UTC)

1. 예배 중 정상 운영 — 번역 broadcast·keepalive ping/pong 정상.
2. **02:02:40** python `/ws` 끊김(`WebSocketDisconnect`) → 이후 약 30초간 `hub: send failed: Unexpected ASGI message ...` ×6 = **번역 6문장 유실** → keepalive 틱이 감지한 뒤에야 pending 큐잉으로 전환.
3. 같은 시기 앱의 RTMP 송출 연결도 소실. 앱은 재송출을 시도하지 않음 — 이후 node 로그에 `rtmp on_publish` **부재**(= publish 시도가 서버에 도착한 적 없음. 훅은 publish 연결 시도마다 호출되므로 부재는 미시도 확정 증거).
4. 사용자 스탑/스타트 반복 → 매 세션: ffmpeg 데이터 0 (`ffmpeg process not updated for 10s` + `Input/output error`, 10s→20s→40s→60s 백오프) + STT는 연결 성공(`configured`) 후 **오디오 10초 부재로 Google이 절단**(`ABORTED: Stream timed out after receiving no more client requests`) → 전사 없이 4연속 에러 → `Stt giving up` **세션 좀비화**.
5. **앱 완전 종료 후 재실행**으로 복구 (스탑/스타트로는 앱 RTMP 스택이 재초기화되지 않음).

## 근본 원인

1건의 네트워크 순단이 폰의 두 연결(ws 수신·RTMP 송출)을 동시에 끊었고, **양쪽 모두 복구 로직이 없거나 부족**해 장애가 지속됨. 서버 HTTPS(REST)는 내내 정상 → 폰·망 자체는 회복됐는데 앱 내 연결만 죽은 채 유지.

## 발견된 결함 (5건)

| # | 위치 | 결함 | 영향 |
|---|---|---|---|
| 1 | mvp 앱 RTMP 송출 | 끊김 감지·재송출 로직 없음 (ws의 `connectWithRetry` 같은 것이 RTMP엔 없음) | 순단 1회로 송출 영구 중단, 앱 완전 재시작 외 복구 불가. **오늘 장애의 몸통** |
| 2 | node `StreamOrchestrator` | "입력 없음"(정상 대기)을 STT 에러로 카운트, 4연속 시 `stopFlag=true` 영구 포기. ffmpeg는 무한 재시도라 서로 어긋남 | 오디오가 늦게/다시 붙어도 세션이 좀비 — 스탑/스타트 강제 |
| 3 | python `main.py`+`websocket.py` | `except WebSocketDisconnect: pass` — hub 미통지, `application_state`만 검사(클라 주도 끊김 시 CONNECTED로 남음), send 실패 문장 미재적재 | 끊김 후 최대 30초(다음 keepalive 틱까지) 번역 전량 유실. 상세: 인수인계 §4-3 |
| 4 | mvp 앱 `ws_client.dart` | 재연결 `maxRetries: 2`, 백오프 1s→2s — 약 3초 만에 영구 포기 | 서버는 5분 대기+pending 큐를 쌓는데 앱이 먼저 포기 → 큐 방류 기회 상실 |
| 5 | node `FfmpegTranscoder` | `-analyzeduration 0` = FLV 기본 90s로 확장, 실제 프로브 종료가 `-probesize 32k` — 저비트레이트 음성이면 시작 후 10~20초 무음 뒤 burst | 세션 시작 지연 + 몰아서 번역. 상세: 인수인계 §4-2 (검증 커맨드 포함) |

## 배제된 가설 (기록)

- python 서버발 ws 절단: keepalive 타임아웃 경로는 `no pong for Ns` 로그 필수 — 부재.
- nginx idle 타임아웃: `/ws`는 `proxy_read_timeout 3600s` + 30초마다 ping/pong 트래픽.
- 화면 잠금: 사용자 확인으로 배제.
- "서버가 한 번 실패하면 영원히 못 붙는 구조": 아님 — nginx-rtmp는 죽은 publisher를 ~1분 내 정리, node 좀비는 새 세션으로 리셋. 영구성은 앱 쪽에만 존재.

## 2차 발생 (02:36:57 UTC) — 진단 실증

같은 예배 중 ws가 한 번 더 끊김. 이번엔 keepalive가 빨리 감지해 유실 1문장, 이후 pending 38+까지 큐잉되는 동안 **앱 재접속 시도 0회** → `maxRetries 2`(~3초 포기) 결함 실증. 결정적 추가 증거: **끊긴 뒤에도 번역이 계속 생산됨 = 같은 폰의 RTMP 송출은 생존** → 폰·망 전체 단절이 아니라 **ws 연결 단독 사망 + 앱 미복구**가 문제의 본체임을 확정. 미전달 문장은 DB에는 저장되므로 모니터 페이지에서 사후 열람 가능.

## 운영 참고

- **RTMP 송출 성공 판정 기준 = node 로그의 `rtmp on_publish` 라인** (publish 연결 시도마다 찍힘, 유지 중엔 안 찍힘).
- `Stt giving up`까지 간 세션은 오디오가 살아나도 복구 안 됨 → on_publish 확인 후 스탑→스타트로 새 세션.
