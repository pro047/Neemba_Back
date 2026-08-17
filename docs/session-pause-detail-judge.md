<!-- 파이프라인 판단검증 단계 산출물 (serial-agent-pipeline, 2026-08-18).
     감사 대상 설계는 docs/session-pause-detail-plan.md 이고, 본문의 §2-A·§3-B
     같은 절 참조는 모두 그 문서를 가리킨다. 원본 첫 두 줄(STATUS/UNVERIFIED)은
     셸 게이트가 파싱하는 형식이라 그대로 남겼다. -->

STATUS: DONE
UNVERIFIED: 2 REFUTED: 1

# 판단 검증: session-pause-detail DESIGN.md

DESIGN.md 의 하중받는 주장 20건을 뽑아 전부 저장소에서 직접 확인했다.
결과: **확인 17 · 반박 1 · 미확인 2**. 반박 1건은 §2-A 계약 3(합집합)의
**근거 문장**이 코드와 어긋나는 것으로, 계약 자체를 바꿀 필요는 없어 보이나
설계 문서의 사실 진술이 틀린 것이므로 사람 판단 대상이다.

확인 방법: 이 워크트리에서 파일 직접 읽기 + grep. 테스트·서버 실행은 하지
않았다(설계 단계 산출물이라 실행 대상 코드가 아직 없음). 아래 "실측" 은
grep/read 도구 출력 기준이다.

## 주장별 판정

| # | 주장 (원문 인용, 축약) | 등급 | 판정 | 근거 / 실제 |
|---|---|---|---|---|
| 1 | ① "`NodeStatus` 필드 3개, 세션 식별 정보 없음" (`main.py:266-273`, `:504-508`) | 코드확인 | **확인** | `main.py:271-273` 필드 3개(`stt_paused`·`rtmp_auth_enabled`·`publish_buffer_size`)뿐, `:504-508` 생성부에 sessionId 없음 |
| 2 | ② "`parse_gauges` 가 라벨을 잘라 버리고 이름 키로 max 집계, sessionId 소실" (`node_metrics.py:74`, `:93`) | 코드확인 | **확인** | `node_metrics.py:74` `name = key.partition('{')[0]`, `:93` `max(previous, value)` — 라벨 문자열은 어디에도 저장 안 됨 |
| 3 | ③ 브리프 반박: "renderStatus 는 `n.sttPaused` 로 칩 1개만, 상태 바에 세션별 배열을 도는 코드는 없다" (`app.ts:1080-1083`, `:1050-1091`) | 코드확인 | **확인** | `app.ts:1080-1083` STT 칩 1개, `:1050-1091` 에 세션 배열 순회 없음. 주의 2가지: (a) "칩 6개 고정" 은 오기 — node up 시 7개, down 시 5개다 (방향 무관). (b) 모니터 페이지의 **세션 탭**(`app.ts:330-530` `sessionsState`/`refreshSessions`)은 세션 배열을 렌더하지만 이는 `/api/monitor/sessions` 를 먹는 별도 패널이고 상태 바가 아니다 — pause 표시에 관한 반박 결론은 유지됨 |
| 4 | ④ "사이드카 억제는 all-of, `/metrics` 직접 읽음 — 이 변경 영향 없음" (`monitor.py:141`, `:53-62`) | 코드확인 | **확인** | `monitor.py:141-142` `'ffmpeg_stale': {'suppress': _all_stt_paused, ...}`, `:53-63` all-of 판정, `:279` `fetch_metrics(urls)` + `:336-337` node `/metrics` 직접 스크레이프 — python 상태 API 미경유 |
| 5 | "이미 문서화된 후속 백로그다" (`docs/gauge-session-label-plan.md:208-210`, `:132-135`) | 코드확인 | **확인** | 실측 grep: `:208` "상태 API(`main.py`) 세션별 상세 노출 … `:210` 필드 추가 필요", `:133-135` "상태 API 는 무변경 … 세션별 상세는 후속" — 줄 번호까지 일치 |
| 6 | "`# TYPE` 시딩(0.0)이 세션 0개 → `sttPaused:false` → 초록의 기계적 원인이며, 의도적이라 제거 안 함" (`node_metrics.py:63-69`, doc`:149-151`, 리뷰#4 `:193`) | 코드확인 | **확인** | `node_metrics.py:63-69` `_LABELLED_GAUGES` 한정 0.0 시딩, doc`:149-151` "게이지 미노출(null)과 구분", doc`:193` 리뷰 #4 원문 일치. 기존 테스트 `test_monitor_status.py:141`(0.0 시딩)·`:180`(무라벨 미시딩)이 이 동작을 고정 |
| 7 | §2-A-3 근거: "두 게이지의 **시딩 시점이 다르다** — `metrics.ts:87`/`:99` 를 부르는 주체가 각각 STT 경로와 publisher 조립 경로" | 코드확인(이라 주장) | **반박** | 실제: 리뷰 #1(doc`:190`, 설계 자신이 인용) 이후 두 게이지는 **같은 지점에서 인접 시딩**된다 — `StreamOrchestrator.ts:73-74` `setSttPaused(sessionId,false); setPublishBufferSize(sessionId,0);` (주석 원문 "Both gauges are seeded here"). 동기 블록이라 시딩 사이에 스크레이프가 끼어들 수도 없다. **이후 갱신** 경로가 다르다는 것(stt: `StreamOrchestrator.ts:143`,`:207` / buffer: `createStreamOrchestrator.ts:65` onQueueSize)은 맞지만, 설계가 쓴 "시딩 시점이 다르다"는 틀렸다. 합집합+한쪽 null 계약 **자체**는 여전히 방어적으로 타당(잘린 응답, 향후 드리프트)하므로 계약 변경은 불필요해 보인다 — 근거 문장이 틀린 것 |
| 8 | §2-A-4 정렬 근거: "Prometheus 노출 순서는 prom-client 내부 삽입 순서라 프로세스 재시작으로 바뀐다" | 추정 | **미확인** | prom-client 소스 확인 시도 → `services/node/node_modules` 가 워크트리 밖 실경로로의 심볼릭 링크라 샌드박스가 읽기를 차단(도구 출력: "blocked … only … allowed working directories"). 미확인이지만 결론(정렬)은 순서가 결정적이든 아니든 무해 — 아래 "그대로 진행" 참조 |
| 9 | (추정 표시된 주장) "prod 는 무라벨 구버전 node 일 수 있다 — release PR 미완" (doc`:174-179`) | 추정 | **미확인** | 문서 근거는 확인됨: doc`:175` `- [ ] release PR(develop→main)` 미체크, doc`:176-178` "prod 는 무라벨 전역 게이지였음". 그러나 **현재 시점 prod 런타임**은 이 세션에서 접근 불가. 설계 §3-B 가 무라벨 케이스를 `sessions:[]` 로 안전 처리하므로 방향에는 영향 없음 |
| 10 | "`gauge_bool`/`gauge_int` 가 None·NaN/Inf 를 None 으로 접는다" (`node_metrics.py:97-108`) | 코드확인 | **확인** | `node_metrics.py:99`,`:106` `if value is None or not math.isfinite(value): return None` |
| 11 | §5-0 기존 테스트 8건의 이름·줄 번호 | 코드확인 | **확인** | `test_monitor_status.py` 전체 읽음 — `:95`,`:105`,`:110`,`:129`,`:141`,`:156`,`:168`,`:180` 전부 이름·위치 일치. `_LABELLED_METRICS_TEXT` 도 `:118` 일치 |
| 12 | 부재 주장: "`fetch_node_gauges` 성공 경로를 단정하는 테스트는 없고, 실패 경로 1건(`:205`)과 monkeypatch 2건(`:225`,`:260`)뿐 → 기존 테스트 수정 불필요" | 코드확인 | **확인** | 전수: 테스트 파일 348줄 전체 읽음 + 저장소 grep — `fetch_node_gauges` 등장은 `:205-207`(실패 경로 직접 호출)·`:225`·`:260`(None stub monkeypatch)뿐. 성공 경로 단정 테스트 없음 |
| 13 | 부재 주장: "`fetch_node_gauges` 호출부는 `main.py:501` 1곳뿐 (전수 grep)" | 코드확인 | **확인** | 실측 grep(저장소 전체, .venv·node_modules 제외): 프로덕션 호출은 `main.py:501` 유일. `main.py:27` 은 import, `:478` 은 주석 |
| 14 | 함정 3: "dict stub 을 남기면 라우트 AttributeError — 그 예외는 try 밖이라 500" | 코드확인 | **확인** (구조) | `main.py:493-518` — node 수집·NodeStatus 조립 구간에 try 없음(try 는 `:485` 이전 DB 조회에만). 새 타입 접근(`snapshot.aggregate`)에서 예외가 나면 라우트 밖으로 전파되는 구조 맞음. 실제 500 재현은 구현 후에나 가능 |
| 15 | "CI 와 동일 명령 `uv run --extra dev pytest tests/ -q` (`ci.yml:66`), ruff line-length 150 (pyproject)" | 코드확인 | **확인** | 실측 grep: `.github/workflows/ci.yml:66` 정확히 그 명령, `python-test` 잡 `working-directory: services/python`. `services/python/pyproject.toml:40` `line-length = 150`, `:32` dev extra 에 `ruff` 포함 |
| 16 | "CI python 잡에 postgres 서비스가 없어 `pg_pool` 테스트는 docker 유무에 따라 skip" (`conftest.py:3-7`) | 코드확인 | **확인** | ci.yml `python-test` 잡에 `services:` 블록 없음(실측), `conftest.py:69-70` docker 부재 시 skip. 참고: GH ubuntu 러너에는 보통 docker 가 있어 CI 에서 실제로는 **skip 이 아니라 자가 컨테이너로 실행**될 가능성이 높다(추정) — 어느 쪽이든 신규 테스트를 `pg_pool` 없이 쓰겠다는 설계 결정에는 영향 없음 |
| 17 | "httpx phase 2s / total 3s — 불변" | 코드확인 | **확인** | `node_metrics.py:27-28` `_PHASE_TIMEOUT_SECONDS = 2.0`, `_TOTAL_TIMEOUT_SECONDS = 3.0` |
| 18 | "현재 두 게이지의 `labelNames` 는 `["sessionId"]` 단독" (`metrics.ts:15`, `:43`) | 코드확인 | **확인** | `metrics.ts:15`,`:43` 둘 다 `labelNames: ["sessionId"] as const` |
| 19 | "`fetchJson<StatusResponse>` 는 런타임 검증 없음(`app.ts:1112`) + `listeners` 가 API 에는 있고 인터페이스에는 없는 선례(`:114-125`)" | 코드확인 | **확인** | `app.ts:1112` 타입 파라미터만, 검증 없음. `main.py:287` `listeners` 존재, `app.ts:114-125` `StatusResponse` 에 `listeners` 없음 — 부재 전수 확인(인터페이스 전문 읽음) |
| 20 | "sticky NaN 은 이름 단위(`:87-92`), 리뷰 #3 의 의도(doc`:192`)" + "`app.js` 산출물 커밋 규약(package.json description)" | 코드확인 | **확인** | `node_metrics.py:87-92` 이름 단위 sticky, doc`:192` 리뷰 #3 원문 일치, `infra/nginx/html/monitor/package.json:4` "산출물(app.js) 커밋 (D7)" |

설계가 스스로 "추정/미확인" 으로 표시한 항목(§0 말미 2건, §5-3-4)은 표시가
정확했고, 그중 방향에 닿는 것은 #9 로 위 표에 올렸다. `app.js`↔`src/app.ts`
일치 여부는 설계 말대로 이 변경과 무관해 판정하지 않았다.

## 구현 전에 해소해야 할 것

1. **[반박 #7] §2-A 계약 3 의 근거 문장 "두 게이지의 시딩 시점이 다르다"**
   — 실제로는 `StreamOrchestrator.start()` 안에서 인접 시딩된다
   (`StreamOrchestrator.ts:73-74`). 푸는 방법: 설계 단계 담당(사람 또는
   재설계 턴)이 §2-A-3 의 근거를 "시딩은 동일 지점이나 **이후 갱신 경로가
   다르고**(stt: `StreamOrchestrator.ts:143`·`:207`, buffer:
   `createStreamOrchestrator.ts:65`), 잘린 응답·부분 파싱에서 두 집합이
   어긋날 수 있다" 로 고쳐 쓰거나, 합집합 계약을 그대로 두고 근거만 틀렸음을
   인지한 채 진행할지 결정. **계약(합집합+한쪽 null) 자체는 코드를 다시 봐도
   여전히 타당**하므로 구현 내용이 바뀔 가능성은 낮다 — 다만 이 판정은 판단
   검증 단계의 몫이 아니라 사람 게이트의 몫이다.

## 그대로 진행해도 되는 것

1. **[미확인 #8] prom-client 노출 순서 비결정성** — 순서가 실제로는
   결정적이더라도 `sorted()` 는 무해하고(비용 무시 가능, 테스트 안정성은
   어차피 확보), 비결정적이라면 정렬이 필수다. 어느 쪽이든 설계 결정(정렬)은
   동일하므로 틀려도 손해가 없다.
2. **[미확인 #9] prod 가 무라벨 구버전 node 인지** — 설계 §3-B 가 무라벨
   케이스를 `sessions:[]` + 집계 정상으로 이미 안전 처리한다. 어느 배포
   상태여도 이 설계는 깨지지 않으므로 확인 없이 진행 가능. (확인하려면
   prod `/metrics` 를 한 번 curl 하면 된다 — 배포 접근 권한이 있는 사람 몫.)
