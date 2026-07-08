# 런타임 검증 하네스

모바일 앱·OBS 없이 이 맥에서 서버 파이프라인을 검증한다. 합성 음성(PCM)을
`/api/mic` WebSocket으로 실시간 속도로 송출하면서 `/ws` 번역 결과를 출력한다.
검증 항목 전체 목록은 `docs/handover-2026-07-07.md` §5-3 참조.

## 준비 (1회)

```bash
# 1. 스택 기동 (.env.dev에 실키 필요 — GOOGLE_APPLICATION_CREDENTIALS, DEEPL_API_KEY)
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d --build

# 2. 테스트 음성 생성 (say + ffmpeg 필요: brew install ffmpeg)
./scripts/verify/gen-audio.sh          # audio/short.pcm(~30s), audio/long.pcm(340s+ 필요)
# long이 340초 미만으로 나오면: REPEATS=120 ./scripts/verify/gen-audio.sh
```

## 시나리오 실행 (Node ≥ 22)

```bash
cd scripts/verify

node run-scenario.mjs basic       # 첫 발화 + 타임아웃 flush + JetStream 로그
node run-scenario.mjs long        # 285s rotation 경계 (약 6분 소요)
node run-scenario.mjs ghost       # WS만 끊기 → 10초 후 유령 세션 teardown
node run-scenario.mjs stop-tail   # 발화 도중 stop → 잔여 문장 DB 기록
```

각 시나리오가 끝나면 **무엇을 확인해야 하는지** 요약에 출력된다.

## 시나리오 ↔ 수정 PR 매핑

| 시나리오 | 검증 대상 | 통과 기준 |
|---|---|---|
| basic | PR #4 첫 발화, PR #8 타임아웃 flush, PR #7 JetStream 설정 | 첫 문장 [RECV] 도착 / 미완성 꼬리가 종료 ~2초 후 도착 / python 로그에 stream·durable exists\|created |
| long | PR #6 rotation 경계 | ~285초 전후 문장 뒤섞임·중복 없음 (단어 1~2개 절단은 알려진 한계) |
| ghost | PR #10 유령 세션 | `docker logs node --since 2m \| grep 'tearing down ghost session'` + 세션 ended |
| stop-tail | PR #8 close_session | `curl -s localhost:8080/api/monitor/sessions`에서 해당 세션 잔여 문장 확인 |
| (수동) 장애 생존 | PR #5·#9 | DEEPL_API_KEY를 틀린 값으로 재기동 → 실패 로그 반복되되 프로세스 생존 / NATS 컨테이너 stop → node 크래시 없이 `publish ... failed` 로그 |
| (수동) 중복 차단 | PR #7 | basic 도중 `docker restart nats` → python 로그 `duplicate dropped`, 결과 문장 중복 없음 |

## RTMP 경로 (OBS 대체)

```bash
# 아무 mp4나 rtmp로 push (검증 때만, 상시 불필요)
curl -s -X POST localhost:3000/api/sessions/start -H 'Content-Type: application/json' -d '{}'
ffmpeg -re -i sample.mp4 -c:a aac -f flv rtmp://localhost:1935/live/translation
```

## 자주 쓰는 로그

```bash
docker logs -f node          # ghost teardown, publish 실패, rotation
docker logs -f python        # consumer/dedup/separator, JetStream 선언
docker logs nats --since 5m  # 인증 실패(자격증명 회전 후 확인)
```

## 주의

- `.env.dev`의 NATS 비밀번호는 2026-07-08 회전됨 — nats.conf가 `$NATS_*` env를
  읽으므로 dev compose의 nats 서비스에 `env_file: [.env.dev]`가 있어야 한다
  (docker-compose.dev.yml은 gitignore된 로컬 파일 — 다른 머신에서는 직접 추가).
- Google STT는 스트리밍 과금이 발생한다. long 시나리오(6분)는 필요할 때만.
