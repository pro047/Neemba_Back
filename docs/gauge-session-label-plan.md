# 전역 게이지 sessionId 라벨화 계획 (세션 핸드오프 문서)

2026-08-14 설계. **구현은 새 세션에서** — 이 문서가 지시서다.

**한 줄 결론**: `neemba_stt_paused`·`neemba_publish_buffer_size` 는 프로세스
전역 last-writer-wins 게이지인데, 마이크 다중 세션(PR #91) 배포로 오케스트레이터
인스턴스가 동시에 N개가 됐다 — **한 세션의 장애를 다른 세션이 0으로 덮어
정상으로 위장**한다. 게이지에 `sessionId` 라벨을 붙이고 세션 종료 시 라벨
시리즈를 지운다.

**시급성**: 다음 주일 예배가 RTMP + 마이크 2명 동시 가동의 첫 실전(실기기 STT
검증)이다. 그날 모니터가 세션별로 정직하게 보이려면 이 수정이 먼저 배포돼야 한다.

## 1. 결함 재현 (리뷰 CONFIRMED, 2026-08-11)

세션 A(마이크)·B(RTMP)가 동시 가동 중:

1. A 의 STT 가 4연속 무오디오로 wedge → `StreamOrchestrator.ts:172`
   `setSttPaused(true)` → 게이지 1
2. B 가 정상 회전 성공 → `:111` 또는 `:128` `setSttPaused(false)` → 게이지 0
3. **모니터는 "STT 동작"으로 표시, A 청취자는 자막 없음** — 같은 은폐가
   `publish_buffer_size` 에도 성립

역방향 오탐도 있다(watch-service 스킬 문서화됨): 종료된 세션이 남긴
`stt_paused=1` 이 새 세션에서 리셋되지 않아 stale 1 로 고착 — 스킬 오탐
목록의 "stt_paused 는 순단 판정에 쓰지 마라"가 이것이다. 라벨화 + 종료 시
시리즈 제거가 두 방향을 모두 없앤다.

## 2. 확정 사실 (2026-08-14 코드 직독, 줄번호 검증됨)

### 쓰는 곳 (node)

| 위치 | 동작 |
|---|---|
| `monitoring/metrics.ts:7-8` | `sttPaused` Gauge 정의 (`neemba_stt_paused`) |
| `metrics.ts:35-36` | `publishBufferSize` Gauge 정의. **:31 주석 "one live session at a time" 이 이번에 깨진 전제** |
| `metrics.ts:80-81` | `setSttPaused(paused)` — 라벨 없는 `.set()` |
| `metrics.ts:92-93` | `setPublishBufferSize(size)` — 동일 |
| `StreamOrchestrator.ts:111·128` | `setSttPaused(false)` (오디오 복귀·세션 stop) |
| `StreamOrchestrator.ts:172` | `setSttPaused(true)` (에러 임계 초과 pause) |
| `createStreamOrchestrator.ts:60` | `onQueueSize: setPublishBufferSize` → RetryingTranscriptPublisher 큐 깊이 |

오케스트레이터는 `start(pcm, {sessionId})` 로 sessionId 를 이미 안다
(세션 슬롯 분리로 context 필수). `createStreamOrchestrator` 는 sessionId 를
모르는 채 publisher 를 조립하므로, publish_buffer_size 라벨링은 sessionId 를
`createStreamOrchestrator` 로 넘기거나(runPipeLines·runMicPipeline 호출부가
이미 앎) 콜백 바인딩 시점을 옮겨야 한다 — 구현 시 결정.

### 읽는 곳 (라벨 붙이면 전부 영향 — 하나라도 빼먹으면 그쪽이 조용히 깨진다)

| 소비자 | 위치 | 현재 파싱 방식 |
|---|---|---|
| python 상태 API | `services/python/src/monitor/node_metrics.py:29` | `neemba_stt_paused` 이름으로 게이지 수집 → `main.py:505` `sttPaused=gauge_bool(...)` |
| 모니터 페이지 상태 칩 | `infra/nginx/html/monitor/app.js:898` | 위 API 의 `sttPaused` bool |
| 경보 사이드카 | `infra/monitor/monitor.py:28·35·50·66-67·112` | `_stt_paused()` 판정 + `ffmpeg_stale` 억제 조건으로도 사용 |
| watch 스크립트 | `scripts/watch-service.sh` | `awk '/^neemba_stt_paused /'` — **라벨 붙으면 `{...}` 때문에 매칭 실패** |
| watch-service 스킬 | `.claude/skills/watch-service/SKILL.md` | 오탐 목록 "stt_paused stale" — 수정 후 이 항목 삭제/갱신 |

### 집계 의미론 (소비자 쪽 결정 필요)

라벨화하면 "게이지 하나" 가 "세션별 시리즈 N개" 가 된다. 기존 소비자는 bool
하나를 기대하므로 **any-of 집계**로 환원한다: 시리즈 중 하나라도 1이면
"어떤 세션의 STT 가 일시정지" (+가능하면 어느 세션인지 목록).
buffer_size 는 세션별 값이 각각 의미 있으므로 합계가 아니라 **max 또는
세션별 노출**을 권장 — 구현 시 소비자별로 결정하고 문서화할 것.

## 3. 변경 대상

| 파일 | 변경 |
|---|---|
| `services/node/src/monitoring/metrics.ts` | Gauge 에 `labelNames: ["sessionId"]`, setter 시그니처에 sessionId 추가, **세션 종료용 `remove` API 추가** (죽은 세션 시리즈 방치 금지 — cardinality 누수) |
| `services/node/src/usecases/StreamOrchestrator.ts` | setter 호출 3곳에 sessionId 전달, **stop 클로저에서 시리즈 remove** |
| `services/node/src/createStreamOrchestrator.ts` | publish buffer 콜백에 sessionId 바인딩 (§2 참고) |
| `services/python/src/monitor/node_metrics.py` | 라벨드 시리즈 수집 + any-of 집계 |
| `services/python/main.py` (status API) | 필요 시 세션별 상세 추가 (기존 bool 필드는 any-of 로 호환 유지) |
| `infra/monitor/monitor.py` | `_stt_paused` any-of 로, `ffmpeg_stale` 억제 조건 재검토 |
| `scripts/watch-service.sh` | awk 패턴을 라벨드 시리즈 대응으로 |
| `.claude/skills/watch-service/SKILL.md` | 오탐 목록의 stt_paused stale 항목 제거·판정 규칙 갱신 |

### 건드리지 말 것

- 카운터류(`ffmpeg_stale_total` 등) — last-writer-wins 문제가 없는 단조 증가.
  범위 밖
- `neemba_hub_active_session` (python 소유) — 이미 세션 dict 기반. 무관

## 4. 함정 (미리 표시)

- **시리즈 제거 누락 = cardinality 누수**: 세션마다 uuid 라벨이 영구히 쌓인다.
  stop 경로(정상 stop·유령 teardown·미접속 teardown·beginSession 실패 경로
  전부)에서 remove 를 보장할 것. StreamOrchestrator stop 클로저가 한 지점이다
- **기존 소비자 5곳 동시 갱신**: node 만 먼저 배포하면 watch-service.sh 와
  monitor.py 가 그 순간부터 조용히 장님이 된다. 한 PR 로 묶고, 배포 전
  로컬에서 `curl :3000/metrics` 출력을 스크립트에 물려 확인
- **prom-client 라벨드 gauge 의 `remove()`** 는 특정 라벨 조합 시리즈만
  지운다 — `reset()` 을 쓰면 전 세션이 지워진다. 혼동 금지
- monitor.py 의 `ffmpeg_stale` 억제(`:112`)는 "stt_paused 면 stale 경보
  억제" 인데, any-of 로 바꾸면 **다른 세션의 pause 가 이 세션의 stale 경보를
  억제**할 수 있다 — 세션 대응이 가능한지 확인하고, 불가하면 보수적으로
  (억제 축소) 갈 것

## 5. 검증 (전부 종료코드 0)

- **TDD**: 대조군 먼저 —
  1. 세션 A pause 후 세션 B 회전 성공해도 **A 라벨 시리즈는 1 유지** (수정
     전 전역 게이지에선 실패하는 대조군)
  2. 세션 stop 후 해당 라벨 시리즈가 메트릭 출력에서 **사라짐**
  3. 두 세션 buffer_size 가 서로 덮지 않음
- node: `cd services/node && npx tsc -p tsconfig.json --noEmit && npm test`
  (기준선 126 passed)
- python: 기존 테스트 러너 (repo CI 와 동일 명령) + node_metrics 집계 단위
  테스트 추가
- `bash scripts/watch-service.sh` 를 dev 스택에 물려 SESSION 섹션이 라벨드
  출력에서도 값을 찍는지

## 6. 리포 상태·절차

- develop 에서 새 브랜치. 커밋 게이트: 테스트/리뷰 후 사용자 승인 →
  `touch .git/REVIEW_ACK` → add(파일 명시)·commit 별도 명령
- 배포까지: PR(develop) → release PR(develop→main) → CI/CD Deploy →
  `/health` 확인. gh CLI 는 간헐 graphql timeout — CI 폴링은 재시도 내성
  필수
- 이 문서는 구현 커밋에 포함 (완료 표기 갱신)
