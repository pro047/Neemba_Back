# 마이크·RTMP 세션 슬롯 분리 계획 (세션 핸드오프 문서)

2026-08-09 설계, 2026-08-10 구현 완료 (§3 재현 조건은 구현 중 코드 직독으로 정정).

**한 줄 결론**: node 의 전역 세션 슬롯(`ports/sessionStore.ts`)을 RTMP 와 마이크
두 경로가 함께 쓰는데, 마이크가 그 전역에 쓰기를 하면 **RTMP 방송의 자막이 조용히
죽는다.** 전역을 없애고 각 경로가 자기 sessionId 를 명시적으로 들고 다니게 한다.

이 수정은 마이크 폴백(youtube-ingest-rejected §6)의 **선행 조건**이다. prod nginx
`/api/mic` 블록을 열기 전에 이걸 먼저 고쳐야 한다 — 안 그러면 마이크를 켜는 순간
RTMP 방송의 자막이 죽는 경로가 prod 에 열린다.

## 1. 배경 — 무엇이 발견됐나

사용자 기억("마이크랑 rtmp 가 같은 api 를 쓴다")이 맞았다. **앱은 분리돼 있다**
(`/api/mic/*` vs `/api/sessions/*`). **서버가 상태 하나를 공유한다.**

`src/ports/sessionStore.ts` 는 문자열 **1개**짜리 모듈 전역이다:

```ts
let currentSessionId: string = "";
export const setSessionId / getSessionId / removeSessionId
```

## 2. 확정 사실 (2026-08-09 코드 직독)

### 쓰는 곳 — 두 경로 모두

| 위치 | 동작 |
|---|---|
| `rtmp.ts:238` | `onSessionIdChanged` → `setSessionId` / `removeSessionId` (RTMP 수명주기) |
| `mic.ts:238` | `setSessionId(sessionId)` (마이크 시작) |
| `mic.ts:201·248·364` | `removeSessionId()` (마이크 종료·실패) |

### 읽는 곳 — 하나뿐, 그런데 비대칭

`StreamOrchestrator.ts:57`:
```ts
const sessionId = context?.sessionId ?? getSessionId();   // context 없으면 전역
```

- **마이크**: `runMicPipeline.ts:20` 이 `consumer.start(input, {sessionId})` 로
  **context 를 명시**한다 → 전역을 안 읽는다. **이미 올바르다.**
- **RTMP**: `StreamlinkToConsumerService.ts:22` 이 `orchestra.start(pcm)` 만 부른다
  (context 없음) → **start() 시점에 전역을 1회 캡처**해 세션 내내 쓴다.

### 결정적 비대칭

`SessionLifecycle.start` 는 `sessionId` 를 만들어 `currentSessionId` 에 넣은 뒤
(`:133-134`) `startPipeline` 을 부르는데(`:148`), **`startPipeline` 시그니처가
`sessionId` 를 안 받는다**(`:42` — `languages` 만). 그래서 RTMP 오케스트레이터는
sessionId 를 알 방법이 전역밖에 없다.

## 3. 결함 재현 (레이아웃·실기기 불필요, 단위로 재현됨)

**정정(2026-08-10 구현 세션)**: `StreamOrchestrator` 는 전역을 매 자막마다 읽는 게
아니라 **`start()` 시점에 1회 캡처**한다. 따라서 오염 창은 "방송 도중 아무 때나"가
아니라 **RTMP 시작 구간** — `SessionLifecycle.beginSession` 이 전역에 sessionId 를
쓴 뒤 `startPythonSession` 을 await 하는 동안(파이프라인이 아직 안 뜬 창) — 이다.
이 창에 마이크 조작이 끼어들면:

1. **마이크 [시작]이 낀다** → `mic.ts` 가 전역을 `"mic-B"` 로 덮어씀 → 뒤이어 뜨는
   RTMP 오케스트레이터가 `"mic-B"` 를 캡처 → **RTMP 자막 전부가 `mic-B` 세션으로
   발행**된다(python 이 다른 세션으로 라우팅) → RTMP 청취자는 자막이 안 온다.
2. **마이크 start→stop 이 낀다** → `removeSessionId()` → 전역이 `""` → RTMP
   오케스트레이터가 `"sessionId required for mic streaming"` 으로 **throw** →
   세션 시작 자체가 500 으로 죽는다.

둘 다 **RTMP 방송(영상)은 멀쩡한데 자막만 조용히 죽는다.** 게다가 에러 문구가
"mic streaming" 이라 RTMP 장애를 조사하는 사람이 마이크 코드를 뒤진다.
단위 재현은 `services/node/test/sessionSlotIsolation.test.ts` 가 고정한다.

**왜 지금까지 안 터졌나**: prod nginx 에 `/api/mic` WS 블록이 없어 마이크가
prod 에서 아예 안 붙는다. 이 결함은 **마이크를 쓰기 시작하는 순간** 드러난다.

## 4. 결정할 것 — 수정 방향

### D1. 전역 제거 방식 (확정 권장: A)

**A. 전역을 없애고 RTMP 도 context 로 sessionId 를 넘긴다.**
`startPipeline` 이 이미 sessionId 를 아는 시점에 호출되므로(`:148`, `currentSessionId`
세팅 직후) 시그니처에 얹기만 하면 된다. 스레딩 경로:

```
SessionLifecycle.start
  → startPipeline({ sessionId, sourceLanguage, targetLanguage })   ← sessionId 추가
    → runPipelines(langs, sessionId)          (rtmp.ts:215 deps)
      → new StreamlinkToConsumerService(ffmpeg, orchestrator, sessionId)
        → orchestra.start(pcm, { sessionId })  ← context 명시
```

그러면 `getSessionId()` 의 독자가 0이 된다 → `ports/sessionStore.ts` 삭제,
`rtmp.ts:238`·`mic.ts` 의 set/remove 호출 전부 제거(죽은 쓰기).

- **왜 A 인가**: 마이크는 이미 이 방식이다. RTMP 만 맞추면 두 경로가 **대칭**이
  되고, "누가 전역을 마지막에 썼나"에 의존하는 상태가 사라진다. P1 이 python 허브에서
  `_sessions` 를 "진짜 등록된 것"과 "누가 주장한 것"이 한 자료구조를 겸하지 않게
  고친 것과 같은 종류의 정리다.
- **함정**: `StreamOrchestrator.ts:57` 의 `?? getSessionId()` 폴백을 **남기지 말 것.**
  남기면 context 를 빠뜨린 미래의 호출이 조용히 전역으로 떨어져 같은 결함이 부활한다.
  context 없으면 즉시 throw 로 바꾼다(지금도 sessionId 없으면 throw 하므로 관례 유지).

**B. 전역을 두 개로 쪼갠다(rtmpSessionId, micSessionId).** 기각 — 오케스트레이터가
어느 전역을 읽을지 또 분기해야 하고, 모듈 전역이라는 근본 문제(인스턴스가 아니라
프로세스 상태)가 그대로다.

### D2. 마이크와 RTMP 를 상호 배타로 강제할까? → **확정: 아니오(완전 독립)** (2026-08-10 사용자 결정)

전역을 없애면 **두 경로가 각자 독립 슬롯을 갖는다** — RTMP 는
`SessionLifecycle.currentSessionId`, 마이크는 `micRuntimeStore.activeSessionId`.
즉 D1 이후 **RTMP 방송과 마이크 세션이 동시에, 서로 간섭 없이 살 수 있다**(각자
다른 python 세션 생성). 청취자는 받은 webSocketUrl 로 각자 붙으니 downstream 은
깨지지 않는다.

- ~~옵션 ①: 상호 배타 강제(한쪽이 살아 있으면 다른 쪽 거절)~~ — **기각.** 이건
  두 경로를 **결합**시키는 것이라 "완전 독립"이라는 요구와 정반대다. 한쪽 장애가
  다른 쪽 시작을 막는 새 실패 모드를 만든다.
- **옵션 ②: 완전 독립 — 확정.** 두 경로는 서로의 존재를 모른다. 각자 자기 슬롯만
  소유하고, 공유 상태가 0이다. D1 이 이걸 그대로 달성한다(전역 삭제 = 마지막 공유
  상태 제거).

**따라서 이번 범위가 곧 완결이다** — D1 을 하면 D2 는 자동으로 ②가 된다. 추가 배선
없음. "상호 배타" 후속 작업은 없다(§7 에서 제거).

## 5. 변경 대상 (node 만, python·nginx·mvp·mic 로직 무변경)

| 파일 | 변경 |
|---|---|
| `ports/audioConsumerPort.ts` | (확인) `AudioConsumerContext` 에 `sessionId` 이미 있음 |
| `usecases/SessionLifecycle.ts` | `startPipeline` 시그니처에 `sessionId` 추가, 호출부에서 전달 |
| `router/rtmp.ts` | `runPipelines` 가 sessionId 를 받게, `onSessionIdChanged`/`setSessionId` 배선 제거 |
| `runPipeLines.ts` | sessionId 를 `StreamlinkToConsumerService` 로 전달 |
| `usecases/StreamlinkToConsumerService.ts` | 생성자에 sessionId, `orchestra.start(pcm, {sessionId})` |
| `usecases/StreamOrchestrator.ts` | `?? getSessionId()` 제거 → context 필수, 없으면 throw |
| `router/mic.ts` | `setSessionId`/`removeSessionId` 호출 제거(죽은 쓰기) |
| `ports/sessionStore.ts` | **삭제** (독자 0) |

### 읽기 전용 (건드리지 말 것)
- `runMicPipeline.ts` — 이미 context 를 올바르게 넘긴다. 손대면 회귀 위험만 는다.
- `micRuntimeStore` — 마이크의 활성 세션 추적은 이 결함과 무관. 유지.

## 6. 검증 (전부 종료코드 0)

- **회귀(vitest)**: 기존 113 passed 무붕괴
- **신규 단위** — 결함을 고정하는 대조군이 핵심이다:
  - RTMP 세션이 도는 중 마이크 start 를 부른 뒤, **RTMP 자막이 여전히 자기
    sessionId 로 발행**되는지 (전역 오염 재현 → 수정 후 통과)
  - 마이크 stop 후 RTMP 자막이 throw 하지 않는지
  - `getSessionId` import 가 코드베이스에서 사라졌는지 (grep 0)
- **mutation check**: `StreamOrchestrator` 에 `?? getSessionId()` 를 되돌리면 위
  대조군이 실패하는지 확인 후 원복
- `tsc --noEmit` + `npm test`

**주의**: 이 결함은 **레이아웃·실기기 없이 단위로 재현된다**(§3). 두 start 를
순서대로 부르고 자막 발행 sessionId 를 관찰하면 된다. 헤드리스·jsdom 불필요.

## 7. 이후 순서 — 마이크 폴백 전체 그림에서 이 작업의 위치

```
1. [이 문서] 세션 슬롯 분리        ← 먼저. 안 하면 마이크가 RTMP 자막을 죽인다
                                     (D2 완전 독립까지 이 단계에서 완결)
2. prod nginx /api/mic WS 블록      ← dev/nginx.conf:31 을 prod 로 이식
3. 실기기 STT 품질 검증             ← 현장 마이크가 예배에서 쓸 만한지 (진짜 리스크)
```

2번을 1번 없이 하면 마이크를 켜는 순간 RTMP 방송의 자막이 죽는 경로가 prod 에 열린다.
3번은 코드로 판정 불가 — RTMP 는 믹서 출력이라 깨끗했지만 기기 마이크는 잔향·소음이
섞인다. **마이크 폴백의 진짜 리스크는 이 슬롯 버그가 아니라 STT 품질이다.**
