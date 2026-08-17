<!-- 파이프라인 산출물 (serial-agent-pipeline, 2026-08-18 실전 1회). 설계 단계가
     자동 생성한 것이고 **사람 검토를 통과하지 않았다.** 주장 감사는
     docs/session-pause-detail-judge.md 에 있고, 그 §2-A-3 근거 문장 1건이
     반박됐다 — 구현 전에 그것부터 읽을 것. -->

STATUS: DONE

# 설계: 상태 API 에 세션별 pause 상세 노출 (session-pause-detail)

한 줄 결론: `GET /api/monitor/status` 의 `node` 객체에 **세션별 게이지 배열
`node.sessions`** 를 추가한다. 기존 `node.sttPaused`/`node.publishBufferSize`
집계값은 계산식까지 그대로 둔다(하위 호환). 파서에 라벨 보존 경로를 하나 더
만드는 것이 변경의 전부이고, 게이지 구조·사이드카·프런트는 건드리지 않는다.

---

## 0. 브리프 주장 검증 결과 (확인한 것 / 못 한 것)

다음 단계는 이 절을 먼저 읽을 것. 브리프의 현황 주장 4개 중 **1개는 코드로 반박됐다.**

| 브리프 주장 | 판정 | 근거 |
|---|---|---|
| ① `/api/monitor/status` 는 `node.sttPaused` 를 bool 하나로만 내려보낸다 | **확인(코드)** | `services/python/main.py:266-273` `NodeStatus` 필드 3개(`stt_paused`·`rtmp_auth_enabled`·`publish_buffer_size`), `main.py:504-508` 생성부. 세션 식별 정보 없음 |
| ② `parse_gauges` 가 라벨드 시리즈를 이름별로 집계하며 sessionId 를 버린다 | **확인(코드)** | `services/python/src/monitor/node_metrics.py:74` `name = key.partition('{')[0]` — 라벨 문자열을 잘라 버리고 `:93` 에서 이름 키로 max 집계. sessionId 는 어디에도 남지 않는다 |
| ③ 모니터 페이지는 세션별 배열을 렌더할 구조가 이미 있어 프런트 변경 불필요 | **반박(코드)** | `infra/nginx/html/monitor/src/app.ts:1080-1083` `renderStatus` 는 `n.sttPaused` 하나로 칩 **1개**만 만든다. 상태 바에 세션별 배열을 도는 코드는 없다(`src/app.ts:1050-1091` 전체가 칩 6개 고정). 즉 **이 변경만으로는 모니터 화면에 어느 세션이 pause 인지 표시되지 않는다.** 다만 "프런트 변경 없이도 깨지지 않는다"는 결론 자체는 맞다 — §6 참조 |
| ④ 사이드카 억제 판정은 all-of 라 무관 | **확인(코드)** | `infra/monitor/monitor.py:141` `'ffmpeg_stale': {'suppress': _all_stt_paused}`, `:53-62` `_all_stt_paused`. 사이드카는 자체 파서(`fetch_metrics`)로 `/metrics` 를 직접 읽고 python 상태 API 를 경유하지 않는다 — 이 변경의 영향 없음 |

추가로 확인한 사실 (브리프에 없던 것):

- 이 작업은 즉흥 요청이 아니라 **이미 문서화된 후속 백로그**다:
  `docs/gauge-session-label-plan.md:208-210` — "상태 API(`main.py`) 세션별 상세
  노출 … 모니터 페이지에 '어느 세션이 pause 인지' 를 보여주려면 필드 추가 필요".
  같은 문서 `:132-135` 가 "상태 API 는 무변경, 세션별 상세는 후속" 으로 미뤄 둔 건.
- 브리프가 말한 "세션이 0개일 때도 STT 동작 초록" 의 기계적 원인도 코드로 확인됨:
  `node_metrics.py:63-69` 가 `# TYPE` 라인만 보고 라벨드 게이지에 `0.0` 을 시딩하므로
  세션 0개 → `sttPaused:false` → 프런트가 `"STT 동작"`(ok/초록). 이 시딩 자체는
  "게이지 미노출(null)" 과 "세션 없음(0)" 을 구분하려고 의도적으로 넣은 것
  (`docs/gauge-session-label-plan.md:149-151`, 리뷰 #4 `:193`)이므로 **제거하지 않는다.**
  대신 새 `node.sessions` 가 빈 배열이면 "세션 0개" 가 데이터로 드러나게 한다(§3-B).

확인하지 못한 것 (추정으로 표시):

- **(추정)** prod 배포 상태: `docs/gauge-session-label-plan.md:174-179` 기준으로
  라벨화 변경(PR #96 계열)은 2026-08-16 시점에 develop 머지·release PR 미완이다.
  이 설계는 "node 가 라벨드 시리즈를 낸다" 를 전제하지만, **무라벨 게이지를 내는
  구버전 node 와도 안전해야 한다** — 그 경우 `node.sessions` 는 빈 배열이 된다(§3-B).
  런타임 `/metrics` 실물은 이 세션에서 확인하지 못했다.
- 모니터 페이지 빌드 산출물(`app.js`)이 현재 `src/app.ts` 와 완전히 일치하는지는
  확인하지 않았다 — 이 변경은 두 파일 모두 건드리지 않으므로 무관.

---

## 1. 변경 대상 파일

| 파일 | 신규/수정 | 변경 내용 |
|---|---|---|
| `services/python/src/monitor/node_metrics.py` | 수정 | 라벨(sessionId) 보존 파싱 함수 추가 + `fetch_node_gauges` 반환 타입을 (집계, 세션별) 묶음으로 확장. 기존 `parse_gauges`/`gauge_bool`/`gauge_int` 의 **외부 계약은 불변** |
| `services/python/main.py` | 수정 | `SessionGaugeStatus` 응답 모델 신규, `NodeStatus` 에 `sessions` 필드 추가, `monitor_status` 라우트가 세션별 dict → 정렬된 배열로 변환 |
| `services/python/tests/test_monitor_status.py` | 수정 | §5 테스트 케이스 추가 (기존 테스트는 1줄도 수정하지 않는 것이 목표 — §5-0) |
| `docs/gauge-session-label-plan.md` | 수정 | `:206-210` 후속 백로그의 첫 항목을 완료로 표기하고 확정된 필드 계약을 1~2줄 기록 |

**건드리지 않는 파일** (의도적):
`services/node/src/monitoring/metrics.ts`, `infra/monitor/monitor.py`,
`infra/nginx/html/monitor/src/app.ts`, `infra/nginx/html/monitor/app.js`,
`scripts/watch-service.sh`.

---

## 2. 공개 인터페이스

### 2-A. API 스펙 — `GET /api/monitor/status` (응답 JSON)

`node` 객체에 `sessions` 키가 **추가**된다. 다른 필드는 이름·타입·의미 전부 불변.

```jsonc
{
  "activeSessions": 2,          // 이하 기존 필드 — 변경 없음
  "wsClientConnected": true,
  "listeners": 2,
  "natsConnected": true,
  "lastBroadcastAgoSec": 3.2,
  "nodeUp": true,
  "node": {
    "sttPaused": true,          // 기존: 라벨드 시리즈 any-of(=max) 집계. 계산식 불변
    "rtmpAuthEnabled": true,    // 기존: 라벨 없는 게이지. 불변
    "publishBufferSize": 7,     // 기존: 세션 중 최댓값(max). 계산식 불변
    "sessions": [               // ★ 신규
      { "sessionId": "aaa", "sttPaused": false, "publishBufferSize": 3 },
      { "sessionId": "bbb", "sttPaused": true,  "publishBufferSize": 7 }
    ]
  }
}
```

`sessions` 계약:

1. **타입**: `SessionGaugeStatus[]`. `null` 이 되는 경우는 없다 — `node` 자체가
   `null`(=`nodeUp:false`)이 아니면 최소한 `[]` 다. `null` 과 `[]` 두 가지
   "없음" 을 만들면 소비자가 둘을 다르게 다룰 여지가 생긴다.
2. **원소**: `{ sessionId: string, sttPaused: boolean | null, publishBufferSize: number | null }`.
   필드별 `null` 은 "그 게이지에 이 sessionId 시리즈가 없거나 값이 비유한값(NaN/Inf)"
   — 기존 최상위 필드의 `null` 의미(`main.py:267-268`)와 같은 규칙.
3. **원소 집합**: 두 라벨드 게이지(`neemba_stt_paused`, `neemba_publish_buffer_size`)에
   등장한 sessionId 의 **합집합**. 한쪽에만 있으면 다른 쪽 필드가 `null`.
   (근거: 두 게이지의 시딩 시점이 다르다 — `metrics.ts:87`/`:99` 를 부르는 주체가
   각각 STT 경로와 publisher 조립 경로이고, `docs/gauge-session-label-plan.md:190`
   리뷰 #1 이 buffer 시딩을 `start()` 안으로 옮겼다. 두 집합이 순간적으로 다를 수 있다.)
4. **정렬**: `sessionId` 문자열 오름차순. Prometheus 노출 순서는 prom-client
   내부 삽입 순서라 프로세스 재시작으로 바뀐다 — 정렬 없으면 응답이 비결정적이 되고
   테스트도 순서에 흔들린다.
5. **`[]` 의 의미**: node 는 응답했으나 라벨드 세션 시리즈가 0줄 —
   ⓐ live 세션 0개, 또는 ⓑ 구버전 node(무라벨 게이지). 둘을 구분하려면
   `activeSessions`(DB) 와 같이 보면 된다. **`sessions:[]` 이면서
   `sttPaused:false` 는 "정상 동작 중" 이 아니라 "볼 세션이 없음" 이다** —
   브리프가 말한 초록 오해의 데이터 측 해소는 여기까지다(화면 반영은 §6).
6. **상한 없음**: 배열 길이를 잘라내지 않는다. 길이가 `activeSessions` 보다 크게
   벌어지면 그것이 `docs/gauge-session-label-plan.md:89-91` 이 경고한 라벨
   cardinality 누수 신호다 — 자르면 그 신호를 가린다.

HTTP 상태 코드·에러 계약 변경 없음. `nodeUp:false` 열화 경로도 불변
(`node:null` 이므로 `sessions` 도 응답에 나타나지 않는다).

### 2-B. Pydantic 모델 — `services/python/main.py`

```python
class SessionGaugeStatus(BaseModel):
    # 신규. node /metrics 의 sessionId 라벨드 게이지 1개 세션분.
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    # None = 그 게이지에 이 세션 시리즈가 없음 / 값이 비유한값.
    stt_paused: bool | None = Field(default=None, alias="sttPaused")
    publish_buffer_size: int | None = Field(default=None, alias="publishBufferSize")


class NodeStatus(BaseModel):          # 기존 모델에 필드 1개 추가
    model_config = ConfigDict(populate_by_name=True)

    stt_paused: bool | None = Field(default=None, alias="sttPaused")
    rtmp_auth_enabled: bool | None = Field(default=None, alias="rtmpAuthEnabled")
    publish_buffer_size: int | None = Field(default=None, alias="publishBufferSize")
    # 신규. sessionId 오름차순. 빈 리스트 = 세션 시리즈 0줄(§2-A 5).
    sessions: list[SessionGaugeStatus] = Field(default_factory=list)
```

`default_factory=list` 를 쓰는 이유: 라우트가 이 인자를 빠뜨려도 `[]` 로 나가
계약 1(`null` 없음)이 깨지지 않는다. `MonitorStatusResponse` 의 `active_sessions`
가 default 를 **일부러 안 둔** 이유(`main.py:280-283`: 누락을 ValidationError 로
드러내려고)와 반대 선택인데, 근거가 다르다 — `activeSessions` 는 누락과 열화가
구분돼야 하는 값이고, `sessions` 는 "없음" 표현이 `[]` 하나뿐이라 누락과 빈 값의
의미가 같다.

### 2-C. `services/python/src/monitor/node_metrics.py` 시그니처

```python
# 신규 — 반환 묶음. dict 2개를 튜플로 돌려주면 호출부에서 순서를 헷갈릴 수 있어
# 이름 있는 컨테이너로 둔다. dataclass(frozen=True) 또는 NamedTuple 중 구현 선택.
@dataclass(frozen=True)
class NodeGaugeSnapshot:
    # 기존 parse_gauges 결과와 같은 dict — 이름 → 집계값(max, sticky NaN).
    aggregate: dict[str, float]
    # 게이지 이름 → sessionId → 값. 라벨드 게이지 2개만, sessionId 라벨이 있는
    # 시리즈만. 세션 0개면 {} (또는 이름 키만 있고 하위가 빈 dict).
    sessions: dict[str, dict[str, float]]


def parse_gauges(text: str) -> dict[str, float]:
    """기존 함수. 계약·동작 전부 불변 (§5-0 회귀 가드)."""


def parse_session_gauges(text: str) -> dict[str, dict[str, float]]:
    """신규. sessionId 라벨을 보존해 게이지 이름 → sessionId → 값 으로 반환."""


def gauge_bool(value: float | None) -> bool | None: ...   # 불변
def gauge_int(value: float | None) -> int | None: ...      # 불변


async def fetch_node_gauges(url: str | None = None) -> NodeGaugeSnapshot | None:
    """반환 타입 변경: dict → NodeGaugeSnapshot. 실패 시 None 은 그대로."""
```

`parse_session_gauges` 동작 규칙 (기존 파서와 일부러 다른 점 포함):

| 규칙 | 내용 | 기존 `parse_gauges` 와의 차이 |
|---|---|---|
| 대상 | `_LABELLED_GAUGES` 2개만 | `parse_gauges` 는 `rtmp_auth_enabled` 포함 |
| sessionId 없는 시리즈 | **버린다** (`neemba_stt_paused 1` 같은 무라벨 줄) | `parse_gauges` 는 집계에 넣는다 → 구버전 node 에서도 `sttPaused` 집계는 계속 동작 |
| `# TYPE` 시딩 | **하지 않는다** — 시딩할 sessionId 가 없다 | `parse_gauges:63-69` 는 `0.0` 시딩 |
| 파싱 못 한 값 | 그 시리즈만 건너뛴다 | 동일 |
| 비유한값(NaN/Inf) | **(name, sessionId) 단위로** sticky NaN — 그 세션 그 게이지만 NaN | `parse_gauges` 는 **이름 단위** sticky (한 세션의 NaN 이 이름 전체를 NaN 으로) |
| 라벨 파싱 한계 | 라벨 값 안의 공백 미지원(uuid 전제), `sessionId` 이외 라벨이 붙은 경우는 §7 참조 | 동일 제약 |

sticky NaN 의 단위가 다른 것이 의도인 근거: 집계값은 "한 세션이라도 알 수 없으면
전체를 단정할 수 없다" 가 맞지만(`docs/gauge-session-label-plan.md:192` 리뷰 #3),
세션별 값은 세션 A 의 NaN 이 세션 B 의 정상값을 가릴 이유가 없다.

구현 권고(강제 아님): 두 함수가 같은 렉싱을 중복하지 않도록
`_iter_samples(text) -> Iterator[tuple[str, str, float]]`(이름, 라벨문자열, 값)
같은 내부 제너레이터를 하나 두고 두 집계기가 그것을 소비하는 형태를 권한다.
단 **기존 `parse_gauges` 의 관측 가능한 동작이 바뀌면 안 된다** — 특히
`# TYPE` 시딩 대상 한정(`:68`), 이름 단위 sticky NaN(`:87-92`),
값 뒤 optional timestamp 무시(`:79`), `' ' not in line` 스킵(`:71`).

---

## 3. 데이터 흐름

### 3-A. 정상 경로 (라벨드 시리즈 2세션)

```
node /metrics 텍스트
  neemba_stt_paused{sessionId="aaa"} 0
  neemba_stt_paused{sessionId="bbb"} 1
  neemba_publish_buffer_size{sessionId="aaa"} 3
  neemba_publish_buffer_size{sessionId="bbb"} 7
  neemba_rtmp_auth_enabled 1
        │
        ▼  fetch_node_gauges()  (httpx, phase 2s / total 3s — 불변)
   ┌────┴─────────────────────────────────────────────┐
   │ parse_gauges(text)          parse_session_gauges(text)
   │  {stt_paused: 1.0,           {stt_paused: {aaa:0.0, bbb:1.0},
   │   publish_buffer_size: 7.0,   publish_buffer_size: {aaa:3.0, bbb:7.0}}
   │   rtmp_auth_enabled: 1.0}
   └────┬─────────────────────────────────────────────┘
        ▼  NodeGaugeSnapshot(aggregate=…, sessions=…)
   monitor_status (main.py)
     ① 집계 3필드: gauge_bool/gauge_int(aggregate.get(name))   ← 기존 코드 그대로
     ② 세션 목록: 두 게이지 dict 의 키 합집합 → sorted()
     ③ 각 sessionId 마다 SessionGaugeStatus(
            sessionId=sid,
            sttPaused=gauge_bool(sessions['neemba_stt_paused'].get(sid)),
            publishBufferSize=gauge_int(sessions['neemba_publish_buffer_size'].get(sid)))
        ▼
   NodeStatus(sttPaused=True, rtmpAuthEnabled=True, publishBufferSize=7,
              sessions=[aaa…, bbb…])
        ▼  FastAPI response_model → alias(camelCase) 직렬화 (기존 동작)
   JSON (§2-A)
```

`gauge_bool`/`gauge_int` 를 세션별 값에도 그대로 재사용하는 것이 핵심이다 —
`None`(키 없음)과 NaN/Inf 를 둘 다 `None` 으로 접는 로직이 이미 그 안에 있어
라우트에 새 분기를 만들 필요가 없다(`node_metrics.py:97-108`).

### 3-B. 경계 경로

| 입력 | `aggregate` | `sessions` | 응답 |
|---|---|---|---|
| node 연결 불가·타임아웃·비 2xx | — | — | `nodeUp:false`, `node:null` (기존과 동일) |
| `# TYPE` 만 있고 시리즈 0줄 (live 세션 0개) | `{stt_paused:0.0, publish_buffer_size:0.0}` | `{}` | `sttPaused:false`(기존 호환), `sessions:[]` |
| 무라벨 게이지 (구버전 node) | `{stt_paused:1.0, …}` | `{}` | 집계는 정상, `sessions:[]` — **구버전 node 와 섞여도 500 이 나지 않는다** |
| `stt_paused` 에만 세션 `aaa`, buffer 에는 없음 | `{stt_paused:1.0}` | `{stt_paused:{aaa:1.0}}` | `sessions:[{aaa, sttPaused:true, publishBufferSize:null}]` |
| 세션 `aaa` 값이 NaN, `bbb` 는 1 | `{stt_paused:nan}` → `sttPaused:null` | `{stt_paused:{aaa:nan, bbb:1.0}}` | `sessions:[{aaa,null},{bbb,true}]` — 집계는 null 이지만 bbb 의 pause 는 보인다 |
| DB 조회 실패 | 무관 | 무관 | `activeSessions:null` + 나머지 정상 (기존과 동일) |

---

## 4. 하지 않는 것 (범위 밖)

1. **모니터 페이지 표시 변경** — `infra/nginx/html/monitor/src/app.ts` 와
   빌드 산출물 `app.js` 를 건드리지 않는다. 결과적으로 이 변경 후에도 화면의
   STT 칩은 여전히 집계 bool 1개다(§0 ③ 반박). `StatusResponse` 인터페이스에
   `sessions` 를 추가하지도 않는다 — 응답에 없는 키를 읽지 않는 구조라
   추가 필드는 무해하고(`app.ts:1112` `fetchJson<StatusResponse>` 는 런타임
   검증 없음), 실제로 `listeners` 필드도 API 에는 있고 `app.ts:114-125`
   인터페이스에는 없는 선례가 이미 있다. **화면 반영은 후속 작업**이며,
   그것까지 원하면 별도 브리프로 올려야 한다.
2. **사이드카(`infra/monitor/monitor.py`) 일절 변경 없음** — 알림 문구도, all-of
   억제 판정도 그대로(브리프 범위 밖 + §0 ④).
3. **게이지 자체 구조 변경 없음** — `services/node/src/monitoring/metrics.ts`
   무변경. 라벨 추가·제거·이름 변경 전부 하지 않는다.
4. **`# TYPE` 0.0 시딩 제거하지 않음** — "세션 0개일 때 초록" 의 직접 원인이지만,
   그 시딩은 "게이지 미노출(null)" 과 구분하려고 리뷰를 거쳐 넣은 것이다
   (`docs/gauge-session-label-plan.md:193`). 되돌리면 그 리뷰 결정을 뒤집는
   변경이 되고 기존 테스트 2건과 충돌한다.
5. **`sttPaused` 집계 의미론 변경 없음** — any-of(max) 유지. `null` 로 바꾸거나
   세션 0개일 때 `null` 을 내는 등의 계약 변경은 구 클라이언트를 깨므로 하지 않는다.
6. **`scripts/watch-service.sh`·watch-service 스킬 변경 없음.**
7. **새 의존성 추가 없음** (표준 라이브러리 + 기존 pydantic/httpx 만).
8. **DB 스키마·마이그레이션 없음.**

---

## 5. 검증 기준

### 5-0. 회귀 가드 (가장 중요)

`services/python/tests/test_monitor_status.py` 의 **기존 테스트를 수정하지 않고**
전부 통과해야 한다. 특히 다음 8건이 파서 리팩터의 안전망이다:

- `test_parse_gauges_extracts_only_targets_when_given_prometheus_text` (:95)
- `test_parse_gauges_skips_unparseable_value_when_present` (:105)
- `test_parse_gauges_returns_empty_when_targets_absent` (:110)
- `test_라벨드_시리즈는_max로_집계해야_한다` (:129)
- `test_게이지_패밀리는_노출됐는데_시리즈가_없으면_0이어야_한다` (:141)
- `test_비유한_샘플이_섞이면_집계값도_비유한값이어야_한다` (:156)
- `test_비유한_샘플_뒤에_정상_샘플이_와도_집계값은_비유한값이어야_한다` (:168)
- `test_라벨_없는_게이지는_TYPE만_있을_때_시딩하지_않아야_한다` (:180)

예외: `fetch_node_gauges` 반환 타입이 바뀌므로 그 함수의 **성공 경로**를
단정하는 테스트가 있으면 조정이 필요하다. 확인 결과 현재 있는 것은
`test_fetch_node_gauges_returns_none_when_node_unreachable` (:205, 실패 경로만)
하나이고 `main.fetch_node_gauges` 를 `None` 으로 monkeypatch 하는 2건(:225, :260)뿐
이라 **수정 불필요**(코드 확인).

### 5-1. 신규 테스트 (같은 파일에 추가, AAA, "~하면 ~해야 한다" 명명)

DB 불필요하도록 전부 `pg_pool` 없이 작성한다 — 파서는 순수 함수, 라우트 테스트는
기존 선례대로 `pool=None` + `fetch_node_gauges` monkeypatch
(`test_monitor_status.py:237-271` 패턴). CI python 잡에 postgres 서비스가 없어
`pg_pool` 테스트는 docker 유무에 따라 skip 된다(`tests/conftest.py:3-7`).

| # | 테스트 | Arrange (입력) | Assert (기대) |
|---|---|---|---|
| 1 | 라벨드 시리즈가 있으면 세션별로 sessionId 와 값을 보존해야 한다 | 기존 `_LABELLED_METRICS_TEXT` (:118) | `parse_session_gauges` == `{'neemba_stt_paused': {'aaa':0.0,'bbb':1.0}, 'neemba_publish_buffer_size': {'aaa':3.0,'bbb':7.0}}` |
| 2 | 라벨 없는 게이지는 세션 상세에 들어가지 않아야 한다 | 같은 텍스트 | 반환 dict 에 `'neemba_rtmp_auth_enabled'` 키 없음 |
| 3 | 세션 시리즈가 0줄이면 세션 상세는 비어야 한다 | `# TYPE` 2줄만 | `parse_session_gauges` 가 세션 항목 0개 (`{}` 또는 값이 빈 dict), **동시에** `parse_gauges` 는 여전히 `0.0` 시딩 (대조군: 시딩 규칙이 세션 상세로 새지 않았음) |
| 4 | 무라벨 시리즈만 오면 세션 상세는 비어야 한다 | `neemba_stt_paused 1\n` (구버전 node) | 세션 항목 0개, `parse_gauges['neemba_stt_paused'] == 1.0` |
| 5 | 한 세션이 비유한값이면 그 세션만 알 수 없음이어야 한다 | `aaa` = `NaN`, `bbb` = `1` | `gauge_bool(sessions['neemba_stt_paused']['aaa'])` is `None` **and** `gauge_bool(...['bbb'])` is `True` (기존 이름 단위 sticky 와 대비되는 대조군) |
| 6 | 비유한값이 뒤 샘플로 되돌려지지 않아야 한다 | 같은 세션 `aaa` 가 `NaN` 다음 `1` | `gauge_bool(...['aaa'])` is `None` (순서 의존 제거) |
| 7 | 라우트가 세션별 pause 상세를 sessionId 오름차순으로 내려줘야 한다 | `fetch_node_gauges` stub → 세션 `bbb`(paused) 가 `aaa` 보다 **먼저** 오는 snapshot | `[s.session_id for s in res.node.sessions] == ['aaa','bbb']`, `bbb.stt_paused is True`, `aaa.stt_paused is False` |
| 8 | 한쪽 게이지에만 있는 세션은 다른 필드가 None 이어야 한다 | `stt_paused` 에만 `aaa` | `sessions[0].publish_buffer_size is None`, `stt_paused is True` |
| 9 | 세션이 없으면 세션 상세는 빈 배열이고 기존 bool 은 유지돼야 한다 | 시딩만 된 snapshot | `res.node.sessions == []` **and** `res.node.stt_paused is False` (하위 호환 + §2-A 5 의미) |
| 10 | node 수집이 실패하면 세션 상세도 나오지 않아야 한다 | `fetch_node_gauges` → `None` | `res.node_up is False`, `res.node is None` (기존 :211 테스트로 이미 커버되면 생략 가능) |
| 11 | 응답 JSON 은 camelCase 키로 직렬화돼야 한다 | 세션 1개 있는 snapshot | `NodeStatus.model_dump(by_alias=True)['sessions'][0]` 의 키가 `sessionId`·`sttPaused`·`publishBufferSize` (구 클라이언트가 읽는 표기 고정) |

### 5-2. 실행 명령 (판정은 파이프라인이 한다 — 이 설계는 명령만 지정)

```
cd services/python && uv run --extra dev pytest tests/ -q      # CI 와 동일 (.github/workflows/ci.yml:66)
cd services/python && uv run --extra dev ruff check .          # line-length 150 (pyproject.toml)
```

node 쪽은 변경이 없으므로 `npm test`/`tsc --noEmit` 은 이 작업의 판정 대상이 아니다
(단 파이프라인이 전체를 돌린다면 기준선 유지 확인).

### 5-3. "됐다" 의 최종 판정선

1. 위 명령이 종료코드 0.
2. 신규 11건 중 대조군 3·4·5·9 가 **구현 전에는 실패**해야 한다(TDD 순서).
3. 응답 예시(§2-A)와 실제 직렬화 결과가 키 이름·중첩 위치까지 일치.
4. **(미실측 / 다음 단계 또는 배포 후)** 실물 확인은 하지 않았다:
   dev 스택에서 `curl :8000/api/monitor/status` 로 마이크 2세션 동시 가동 시
   `sessions` 에 두 sessionId 가 찍히는지는 이 설계 단계에서 확인 불가.
   `docs/gauge-session-label-plan.md:177-182` 의 미완 항목(prod 첫 tick 확인)과
   같은 성격의 이월 항목으로 남긴다.

---

## 6. 후속 작업 (이번 범위 아님, 다음 브리프 후보)

1. 모니터 페이지에 세션별 pause 표시 — `src/app.ts` `StatusResponse.node` 에
   `sessions` 추가 + `renderStatus`(:1050)에서 pause 된 sessionId 를 칩으로.
   `npm run build` 로 `app.js` 재생성 필요(산출물 커밋 규약, `package.json` description).
   **브리프 ③ 이 "불필요" 라고 단정한 부분이 실제로는 여기 남는다.**
2. `sessions:[]` + `activeSessions>0` (node 가 세션을 모르는 상태)를 화면에서
   경고로 표시 — 브리프가 말한 "세션 0개인데 초록" 오해의 완전한 해소.
3. 사이드카 알림 문구에 pause 된 sessionId 목록 포함
   (`docs/gauge-session-label-plan.md:211`) — 브리프 범위 밖으로 명시됨.

---

## 7. 함정 (구현 단계에서 미리 볼 것)

1. **`parse_gauges` 리팩터가 조용히 계약을 바꾸는 것** — 특히 `# TYPE` 시딩
   대상(`_LABELLED_GAUGES` 한정)과 **이름 단위** sticky NaN. 세션별 파서의
   sticky 단위(세션별)와 헷갈려 한쪽 규칙을 다른 쪽에 적용하면 기존 테스트
   `:156`/`:168`/`:180` 중 하나가 깨진다 — 깨지면 규칙을 옮긴 것이다.
2. **라벨 파싱을 `partition('{')` 만으로 처리하면 sessionId 값을 못 얻는다.**
   sessionId 추출은 `{sessionId="uuid"}` 형태 전제(uuid 라벨, 값 내 공백 없음 —
   기존 파서와 같은 제약). `sessionId` 외 라벨이 추가되거나 라벨 순서가 바뀌면
   **매칭 실패로 그 시리즈를 버릴 것**(집계값은 살아 있으니 조용한 오값보다 낫다).
   현재 두 게이지의 `labelNames` 는 `["sessionId"]` 단독이다
   (`services/node/src/monitoring/metrics.ts:15`, `:43` — 코드 확인).
3. **`fetch_node_gauges` 반환 타입 변경은 호출부 1곳뿐이지만**(`main.py:501`,
   전수 grep 확인) 테스트가 이 함수를 monkeypatch 한다 — stub 의 반환도 새
   타입이어야 한다. `dict` 를 돌려주는 stub 을 남기면 라우트에서 `AttributeError`
   가 나고, 그 예외는 `monitor_status` 의 try 밖이라 **500 이 된다**(열화 원칙 위반).
   신규 라우트 테스트는 반드시 새 타입 stub 을 쓸 것.
4. **세션 배열을 `node` 밖(최상위)에 두지 말 것** — 최상위 `activeSessions` 는 DB
   기준 live 세션이고 `node.sessions` 는 node 게이지 기준이다. 두 출처를 같은
   레벨에 나란히 두면 "어느 쪽이 진실인가" 를 소비자가 판단해야 한다.
   node 게이지 스냅샷은 `node` 안에 모은다(기존 3필드와 같은 위치).
5. **정렬 누락** — prom-client 출력 순서에 의존하면 테스트가 간헐 실패하고
   프런트가 나중에 붙을 때 칩 순서가 흔들린다.
