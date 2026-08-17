# 전역 게이지 sessionId 라벨화 계획 (세션 핸드오프 문서)

2026-08-14 설계 → 같은 날 구현 완료 (§7 구현 결정, §8 진행 상태).
**2026-08-16 커밋·PR 완료 — 남은 절차(배포·실전 검증)는 §8.**

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

## 7. 구현 시 결정 (2026-08-14, "구현 시 결정" 항목의 확정치)

- **publish_buffer 라벨 바인딩 (§2)**: `createStreamOrchestrator(sessionId,
  languages)` 로 시그니처 확장 (호출부 runPipelines·runDefaultMicPipeline 이
  이미 sessionId 를 앎). 콜백 바인딩 이동보다 침습이 작다.
  `runDefaultMicPipeline` 의 sessionId 는 필수로 승격 (PR #87 후속 백로그와
  일치).
- **집계 의미론 (§2)**: 소비자 4곳 모두 이름별 **max** 환원 —
  stt_paused 는 any-of 와 동치, buffer_size 는 세션 중 최댓값. 상태 API
  (`main.py`) 는 무변경 (node_metrics 가 같은 키로 집계값을 돌려주므로 bool
  호환 유지, 세션별 상세는 후속).
- **단, 사이드카의 억제 판정 2곳은 all-of** (`_all_stt_paused`):
  심장박동 억제와 ffmpeg_stale 폐기는 "전 세션 무오디오 = 방송 종료 수순"
  일 때만. any-of 면 마이크 한 세션의 pause 가 RTMP 의 진짜 장애 경보를
  삼킨다 (§4 함정 4 — 세션 대응이 불가해 보수적 축소 채택). 정보성
  '송출 중단' 알림은 any-of. 시리즈 0개는 공허참으로 삼키지 않음.
- **시딩**: 세션 시작 시 stt_paused=0, publisher 조립 시 buffer_size=0 을
  시딩 — "세션 존재·정상"과 "세션 없음"을 시리즈 유무로 구분. start 실패
  경로는 catch 에서 remove.
- **remove 경로**: StreamOrchestrator stop 클로저 한 지점 (모든 teardown
  경로가 이 클로저를 경유) + start 실패 catch. 추가 발견·수정:
  RetryingTranscriptPublisher 의 drain 루프가 stop **이후** 늦게 resolve 된
  publish 에서 onQueueSize 를 재발화해 지운 시리즈를 0 으로 부활시키는 경합
  → notify() 에 stopped 가드 (stop() 의 종료 0 보고는 직접 호출로 유지).
- **python 파서**: `# TYPE` 라인이 있으면 기본값 0.0 (라벨드 게이지는 세션
  0개면 시리즈가 0줄 — "게이지 미노출(null)"과 구분). 라벨 값 내 공백
  미지원 (uuid 전제, 사이드카 파서와 같은 제약).
- **검증 결과**: node tsc 통과 + 132 passed (기준선 126 + 신규 6 — 대조군
  §5-1·2·3 포함), python 180 passed (규칙 7·파서 2 신규). watch-service.sh
  awk 는 라벨드/무라벨 혼합 입력으로 로컬 검증.

## 8. 진행 상태·다음 단계 (2026-08-16 갱신)

### 완료

- [x] 구현·테스트 전부 (§3 변경 대상 8곳 중 리포 내 7곳 + 로컬 스킬 1곳,
  §7 결정 반영). 브랜치 `feat/gauge-session-label` (develop 기점)
- [x] node `tsc --noEmit` + 132 passed / python 180 passed (2026-08-14 실행)
- [x] `.claude/skills/watch-service/SKILL.md` 갱신 — **gitignore 된 로컬
  파일**이라 diff·PR 에 안 잡힌다. 오탐 목록에서 stt_paused stale 항목
  삭제, "진짜 순단 신호"에 라벨드 판정 규칙 추가, 추후 수정 항목 체크 완료
- [x] `.git/REVIEW_ACK` 생성됨 (커밋 게이트 통과 승인 상태)

### 미완 — 다음 세션은 여기부터

- [x] **커밋 전 리뷰 반영** (2026-08-16, §9)
- [x] **커밋·PR** (2026-08-16): 수정 13파일 + `scripts/watch-service.sh`
  최초 커밋, PR(develop). 커밋 전 재검증: node tsc + 137 passed /
  python 183 passed. 참고: 2026-08-16 주일 예배는 이 변경 미배포 상태로
  진행 — prod 는 무라벨 전역 게이지였음 (실전 검증 이월 확정)
- [ ] release PR(develop→main) → CI/CD Deploy → `/health` 확인.
  gh CLI 간헐 graphql timeout — CI 폴링은 재시도 내성 필수
- [ ] §5 의 dev 스택 실측(`curl :3000/metrics` 를 watch-service.sh 에 물려
  SESSION 섹션 확인)은 미실시 — 로컬 awk 합성 입력 검증으로 대체했다.
  배포 후 prod 첫 tick 에서 라벨드 시리즈가 찍히는지 반드시 확인할 것
- [ ] 실전 검증: RTMP + 마이크 동시 가동 예배에서 세션별 시리즈가 정직하게
  보이는지 (§1 의 은폐 시나리오가 재현되지 않는지) — 2026-08-16 주일이
  첫 실전 예정이었다. 배포가 예배 전에 안 됐다면 다음 송출로 이월

## 9. 커밋 전 리뷰 반영 (2026-08-16)

리뷰 8건 수정, 회귀 테스트 8건 추가(node +5, python +3).

| # | 결함 | 수정 |
|---|---|---|
| 1 | **조립 시점 시딩 누수** — `createStreamOrchestrator` 가 `publish_buffer_size` 를 0 으로 시딩하는데, `start()` 가 끝내 호출되지 않으면(ffmpeg spawn 동기 throw 등) 제거 경로 두 곳 모두 도달 불가 → §7 이 막으려던 그 누수를 시딩이 되살림 | 시딩을 `StreamOrchestrator.start()` 의 try 앞으로 이동 — **시딩·제거를 같은 스코프에** (§7 "publish_buffer 라벨 바인딩" 결정의 정정) |
| 2 | `start()` 실패 catch 가 시리즈만 지우고 열린 STT 핸들·퍼블리셔를 방치 — 늦게 도착한 gRPC 에러가 회전으로 들어가 과금 스트림을 새로 열고 지운 시리즈를 1 로 부활 | catch 에서 `stopFlag=true` → 타이머 해제 → `dispose()` → remove 순서 (stop 클로저와 동일 순서) |
| 3 | **NaN 삼킴** — `max(0.0, nan) == 0.0` 이라 '알 수 없음'이 '정상 0' 으로 둔갑, 결과가 라인 순서에도 의존 | 비유한 샘플이 하나라도 있으면 이름째 `nan` 고정 (sticky) → `gauge_bool/int` 가 `None` |
| 4 | `# TYPE` 0.0 시딩이 라벨 **없는** `rtmp_auth_enabled` 에도 적용 — 잘린 응답에서 '미상' 을 '인증 꺼짐' 으로 단정 | 시딩 대상을 `_LABELLED_GAUGES` 2개로 한정 |
| 5 | `watch-service.sh` 가 ssh 실패 tick 에 상태 파일을 빈 값으로 덮어써 baseline 파괴 — 장애 구간 증분이 소리 없이 사라짐 | CTR 0줄이면 상태 파일 미갱신 + 부분 수집 시 미보고 키 캐리포워드 |
| 6 | `RetryingTranscriptPublisher.stop()` 비멱등 — 종단 0 보고가 notify 가드를 의도적으로 우회하므로 두 번째 stop 이 지운 시리즈를 부활 | `stop()` 첫 줄 멱등 가드 |
| 7 | `InterimChunkOrchestrator.dispose()` 가 `trailingTimer` 미해제 — 정지된 버퍼로 늦은 publish → drop 카운터 → 사이드카가 정상 종료를 'NATS 순단' 으로 경보 | dispose 에서 함께 clear |
| 8 | 신규 파일 `watch-service.sh` 전체·`monitoringMetrics.test.ts` 추가분이 한국어 주석 (전역 규칙: 코드 주석은 영어) | 영어로 교체. 테스트 **이름**은 규칙상 한국어 유지 |

리뷰에서 **기각**된 지적 3건 (근거 남김): stop 클로저 throw-skip(선행 3단계가
동기 throw 불가), `_all_stt_paused` 상시 오경보(§7-3 의 의도된 트레이드오프),
python 파서 중복(사이드카는 stdlib 전용 독립 컨테이너 — 공유 불가).

후속 백로그로 내린 정리성 2건: 테스트 게이지 헬퍼 3중복(`test/helpers/metrics.ts`
추출), `watch-service.sh` 의 `eval` → bash 간접 확장 + 컨테이너 로그 중복 스트리밍.

### 후속 백로그 (이 PR 범위 밖)

- 상태 API(`main.py`) 세션별 상세 노출 — 현재는 any-of/max 집계 bool·int
  만 유지 (§7 집계 결정). 모니터 페이지에 "어느 세션이 pause 인지" 를
  보여주려면 필드 추가 필요
- 사이드카 알림 문구에 pause 된 sessionId 목록 포함 (지금은 발생 여부만)
