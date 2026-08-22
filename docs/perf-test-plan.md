# 성능 테스트 계획 (2026-08-22 승인)

발주자: jinseong · 상태: **계획 승인 · P1 구현은 다음 세션**

**이 문서의 범위는 "무엇을 어떤 순서로 재는가" 다.** 현황·우선순위는
`docs/handover-2026-08-02.md`, 관측 스택의 배선은 `docs/monitoring-plan.md`.

---

## 1. 목표와 안 하는 것

**목표**: "느린 것 같다" 를 숫자로 바꾸고, 부하가 오를 때 **무엇이 먼저 무너지는지
순서를 확정**한다.

**명시적으로 안 하는 것**:

- HTTP RPS 측정 — `/api/sessions/*`·`/api/mic/*` 는 세션당 몇 번 불리는 제어 평면이다
- 동시접속 극한 탐색 — 청취자는 교회 인원 규모이고 `MIC_MAX_SESSIONS` 로 이미 상한
- **prod 합성 부하** (D5)
- 모니터 페이지 연동 (§6)

## 2. 왜 일반 부하 테스트가 안 맞는가

이건 요청/응답 API 가 아니라 **상시 스트림 파이프라인**이다. 사용자 1명 = 요청 1개가
아니라 **세션 1개 = 상시 점유**다. 그래서 부하 축이 사용자 수가 아니다.

**진짜 축은 발화 밀도(문장/분)** 이고, 근거는 아래 §3 이다.

## 3. 출발점이 된 코드 사실 (2026-08-21 확인)

| # | 사실 | 근거 |
|---|---|---|
| 1 | DeepL `translate()` 가 **동기**이고 **호출마다 `Translator(api_key)` 를 새로 만든다** | `src/deepL/deepL.py` |
| 2 | 그걸 **`await` 없이** 부른다 | `src/separator/kss_separator.py:207` |
| 3 | 그 자리는 `_push_loop` 안이고 **`sentence_queue` 를 직렬로 비우는 태스크 1개** | 같은 파일 |
| 4 | 번역기 주입 자리는 **조립 시점**이다 (런타임 스위치 없음) | `main.py:86-90` |
| 5 | prod 는 **t3.medium unlimited 단일 호스트에 전 컨테이너** | `.github/workflows/cpu-credit-watch.yml` |
| 6 | `_parse_request` 는 `data["키"]` 명시 접근 — **여분 필드를 안전하게 무시**한다 | `src/consumer/consumer.py` |

**1~3 에서 나오는 결론**: 문장 하나를 번역하는 왕복 시간 동안 **python 이벤트 루프
전체가 멈춘다.** 그 루프가 WS 허브 브로드캐스트·keepalive ping/pong·NATS 소비·
모니터 API 를 전부 돌린다.

### 검증할 가설 (참·거짓 둘 다 소득)

> 발화 밀도가 오르면 자막 지연만 늘어나는 게 아니라 **`ws_blips` 순단이 함께
> 발생한다** — pong 응답도 같은 이벤트 루프에 걸려 있으므로.

참이면 **순단 이력은 네트워크 지표가 아니라 부하 지표**이고, 장애 조사의 해석이
바뀐다. 거짓이면 위 모델이 틀린 것이니 그것도 결론이다.

## 4. 확정 결정

**D1. E2E 지연은 프로덕션 계측이 아니라 하니스로 잰다.**
하니스가 주입 시각 T0 과 소켓 수신 시각 T1 을 둘 다 갖는다 → 프로덕션 코드 변경 0.
*기각*: NATS 페이로드에 `publishedAt` 추가. 사실 6 때문에 안전하긴 하지만 node·python
양쪽 배포가 필요해 비용이 크다.

**D2. 프로덕션에는 "밖에서 못 보는 것" 3개만 계측한다.** → §5 의 표.

**D3. 가짜 번역기를 만들지 않는다.**
주입 자리가 조립 시점(사실 4)이라 런타임 스위치를 만들면 **프로덕션에 테스트 전용
분기**가 생긴다. DeepL 은 문자 수 과금이고 1회 스윕(1,000문장 × 30자 ≈ **30k자**)이
작다.
⚠️ **선행**: 현재 DeepL 플랜과 잔여 문자 수 확인. 미확인 상태로 돌리지 않는다.

**D4. 부하는 NATS 로 주입한다.**
발화 밀도를 정확히 통제하고 **STT 과금이 0**이다. 주입 형식은
`multi-listener-p1-plan.md` §6 (camelCase).
⚠️ **함정**: 형식이 틀리면 `term()` 되는데 **ack floor 가 올라가 정상 소비와 구분이
안 된다.** `neemba_consumer_unparseable_total` 을 매 실행 전후로 대조한다.

**D5. prod 합성 부하는 하지 않는다.**
t3 크레딧 알람(60/일)을 거짓으로 울려 **2026-07-12 유출을 잡으려고 만든 감시를
무디게 한다**(`cpu-credit-watch.yml`). prod 는 예배 실측 관측으로 대신한다.

**D6. 관측은 Grafana 로 한다. 모니터 페이지는 건드리지 않는다.** → §6

## 5. 계측 재고 — 무엇이 이미 있고 무엇이 없는가

**전부 실측이다** (두 `/metrics` 직접 스크레이프 + Prometheus 타깃 API, 2026-08-22).

### 스크레이프 타깃 (`infra/prometheus/prometheus.yml`, 15초 간격)

```
node     up     http://node:3000/metrics
python   up     http://python:8000/metrics
nginx    up     http://nginx-exporter:9113/metrics
```

**nginx 잡은 2026-08-22 까지 DOWN 이었다** — 익스포터 컨테이너가 안 떠 있었고,
띄워도 `stub_status` 가 어느 nginx.conf 에도 없어 404 였다. dev conf 에만
`location = /nginx_status` 를 추가해 해소했다(§7). **prod conf 에는 없다.**

### ① 호스트·프로세스 — 공짜, 이미 있음

| 지표 | 어느 쪽 | 답하는 질문 |
|---|---|---|
| `process_cpu_seconds_total` | node·python | CPU 사용량 (t3 크레딧과 직결) |
| `process_resident_memory_bytes` | node·python | 메모리 누수 |
| `process_open_fds` / `process_max_fds` | node | **소켓 팬아웃의 실제 상한** |
| `python_gc_collections_total` | python | GC 가 지연에 기여하나 |

### ② 이벤트 루프 — node 는 공짜, **python 은 없다**

```
nodejs_eventloop_lag_seconds   _min _max _mean _stddev _p50 _p90 _p99
```

node 는 `collectDefaultMetrics` 로 8개 계열이 이미 나온다.

**python 에는 없다** — `prometheus_client` 기본 익스포터가 이벤트 루프 지연을 주지
않는다. **그리고 블로킹이 있는 곳은 정확히 python 이다**(§3). node 쪽 공짜 지표를
보고 "이미 있네" 하면 정반대 결론에 간다.

**진단 조합**: python 의 loop lag 과 CPU 를 한 패널에 겹쳐 그리면 원인이 갈린다.

| loop lag | python CPU | 해석 |
|---|---|---|
| ↑ | **평평** | **DeepL 동기 HTTP 대기** (소켓 블로킹, CPU 안 씀) |
| ↑ | ↑ | kss 문장 분리 등 **CPU 바운드** |

### ③ 오디오 입력 (node)

| 지표 | 타입 | 답하는 질문 |
|---|---|---|
| `neemba_stt_paused{sessionId}` | gauge | 세션별 STT 정지 |
| `neemba_ffmpeg_stale_total` | counter | ffmpeg 이 10초간 멈춘 횟수 |
| `neemba_session_stopped_total` | counter | 종료 사유별 세션 수 |

### ④ 발행 버퍼 (node → NATS)

| 지표 | 타입 | 답하는 질문 |
|---|---|---|
| `neemba_publish_buffer_size{sessionId}` | gauge | **하류가 못 따라가는 첫 신호** |
| `neemba_publish_buffer_dropped_total` | counter | 실제 유실량 |

**부하 스윕에서 가장 먼저 움직일 지표다.** 여기가 차오르는 지점이 1차 포화점.

### ⑤ NATS·소비 (python)

| 지표 | 타입 | 답하는 질문 |
|---|---|---|
| `neemba_nats_connected` | gauge | 연결 생존 |
| `neemba_consumer_unparseable_total` | counter | **주입 형식 오류의 유일한 탐지 수단** (D4) |

### ⑥ 허브·팬아웃 (python)

| 지표 | 타입 | 답하는 질문 |
|---|---|---|
| `neemba_hub_listeners` | gauge | 실제 붙어 있는 소켓 수 |
| `neemba_hub_active_session` | gauge | 세션 생존 |
| `neemba_hub_last_broadcast_timestamp_seconds` | gauge | **staleness — 지연이 아니다** |
| `neemba_hub_send_failed_total` | counter | 전송 실패 |

⚠️ `last_broadcast_timestamp` 를 지연으로 읽지 말 것. `time() - <이 값>` 으로
**무자막 구간**은 볼 수 있지만 그건 침묵이지 지연이 아니다.

### ⑦ nginx (익스포터)

`nginx_up`, `nginx_connections_active`, `nginx_http_requests_total`.
팬아웃 테스트에서 연결 수·5xx 를 본다.

### ⑧ 쓸모가 적은 것

`neemba_requests_total` — 라벨이 없어 **경로·상태코드 구분이 안 된다.**
성능 분석에는 거의 못 쓴다.

### 없는 것 — P1 의 정확한 범위

| 추가할 것 | 타입 | 없으면 못 하는 것 |
|---|---|---|
| `neemba_event_loop_lag_seconds` (python) | Histogram | **블로킹 가설을 증명·반증할 수 없다** |
| `neemba_translate_duration_seconds` | Histogram | 지연 예산에서 DeepL 몫을 못 뗀다 |
| `neemba_sentence_queue_depth` | Gauge | 유입 > 처리인지 판정 불가 |

**딱 3개다.** E2E 지연은 계측하지 않는다(D1).

## 6. 왜 모니터 페이지가 아니라 Grafana 인가

| | 모니터 페이지 | Grafana |
|---|---|---|
| 질문 | **"지금 무슨 일이 벌어지나"** | **"지난 10분의 분포가 어땠나"** |
| 청중 | 예배 중 운영자 | 성능 분석하는 개발자 |
| 환경 | prod | dev |

**구조적으로 못 넣는 이유 3가지**:

1. **상태 API 파서가 히스토그램을 통과시키지 못한다.**
   `src/monitor/node_metrics.py:74` 가 `key.partition('{')[0]` 로 라벨을 잘라내고
   `:93` 이 이름 키로 `max` 집계한다. `_bucket{le=...}` 계열이 이름 하나로 뭉개져
   **분포가 파괴된다.** 게이지 전용 파서로 맞다
2. **프런트에 차트 렌더 수단이 0이다** — `app.ts` 에 `canvas`·`svg`·`chart` 0건.
   게다가 `monitoring-plan.md` 가 **외부 CDN 0** 을 못박아 뒀다
3. **30초 폴링이라 해상도가 안 맞는다** — 부하 스윕의 흥미로운 구간이 샘플 사이로
   빠진다

**Prometheus·Grafana 는 dev 전용이다** (`docker-compose.prod.yml` 에 0건).
prod 에 얹으면 t3.medium 2 vCPU 에 컨테이너를 2개 더 올리는 일이라
**측정 행위가 측정 대상을 바꾼다.**

## 7. 단계

| 단계 | 내용 | 선행 | 상태 |
|---|---|---|---|
| **P1** | 계기 3종 + 회귀 테스트 | 없음 | **완료 2026-08-22** (검증 4종 통과 · 미배포) |
| **P0** | 실제 발화 밀도 측정 (`published :` 로그) | 예배 | 8/23 |
| **P2** | 하니스 구축 + 지연 예산 분해 (dev) | P1 | — |
| **P2.5** | Grafana 대시보드 | P1 | — |
| **P3** | 포화 스윕 — 밀도 1×/2×/4×/8× | P2, (P0) | — |
| **P4** | **로컬 자원 제약 하니스** | P3 | 방향 확정 (§8) |

### P1 변경 대상 — 이 목록 밖은 읽기 전용

```
services/python/src/monitoring/metrics.py    Histogram 2 + Gauge 1 (기존 96줄 패턴 그대로)
services/python/src/separator/kss_separator.py  translate 구간 계측 + 큐 깊이 갱신
services/python/main.py                      lifespan 에 event loop lag 태스크 기동
services/python/tests/test_perf_metrics.py   신규 — 회귀 테스트
infra/grafana/provisioning/dashboards/perf.json  신규 (P2.5)
```

**하니스(P2)는 리포 밖** — `watch-service` 이후 이 리포의 방침이다. 재현 요건은 §9.

### P1 검증 방법

| 명령 | 통과 조건 |
|---|---|
| `cd services/python && .venv/bin/python -m pytest -q` | 종료코드 0, **`189+N passed`** (현재 189) |
| `cd services/python && uv run ruff check src/` | **30 errors 이하** (현재 baseline 30 — 신규 유입 0) |
| `docker restart python` 후 `docker exec python curl -fsS localhost:8000/metrics \| grep -c '^neemba_event_loop_lag_seconds_bucket'` | **≥ 1** |
| mutation check | 계측 호출을 지우면 신규 테스트가 **실패**하는지 확인 후 원복 |

`docker restart` 가 조건에 들어간 이유는 §10 의 마운트 함정 2건 때문이다.

## 8. P4 — 로컬에서 지속 성능을 재는 방법 (방향)

**t3 크레딧 회계는 로컬에서 재현할 수 없다.** 재현할 수 있고 실제로 중요한 것은
**지속 CPU 상한**이다.

- t3.medium = 2 vCPU, baseline **20% = 지속 0.4 vCPU**, 그 위는 크레딧 소모
- **짧은 테스트는 용량을 과대평가한다** — 5분 부하는 축적 크레딧을 타고 넘어가고
  실제 예배는 40~90분이다. **테스트 길이가 예배 길이와 같아야 의미가 있다**

→ **자원 제약 오버레이로 prod 를 위아래로 괄호친다.**

| 프로파일 | 제약 | 무엇을 재나 |
|---|---|---|
| 버스트 | `cpus: 2.0` | t3.medium 이 크레딧 있을 때의 상한 |
| 지속 | `cpus: 0.4` | **크레딧이 마른 뒤의 하한** |

별도 compose 오버레이 파일(로컬, gitignore)로 두어 **평소 dev 스택을 건드리지
않는다.** 실행은 `-f docker-compose.dev.yml -f docker-compose.perf.yml`.

⚠️ **한계를 문서에 못박을 것**: dev(맥)와 prod(t3.medium)는 성능 특성이 다르다.
dev 로 알 수 있는 건 **"어디가 먼저 막히는가"(순위)** 이고
**"몇 명까지 되는가"(절대값)가 아니다.**

## 9. 하니스 재현 요건 (리포 밖)

- 주입은 NATS. 형식은 `multi-listener-p1-plan.md` §6 (camelCase, `term()` 함정 포함)
- 청취자는 `ws://python:8000/ws?sessionId=` 또는 nginx 경유 `ws://localhost:8080/ws?...`
- 합성 마이크 세션은 `POST /api/mic/start` (무인증, body 선택) —
  `gauge-session-label-plan.md` §10 에 절차가 있다
- E2E 지연은 하니스가 T0(주입)·T1(소켓 수신)로 직접 계산한다 (D1)

## 10. 함정

- **`docker stop monitor` 를 먼저 할 것** — dev·prod 가 디스코드 웹훅을 공유해서
  테스트 알림이 예배 채널로 샌다 (`handover-2026-08-02.md` §5)
- **node 를 고쳤으면 `docker restart node`** — `tsx watch` 가 macOS bind mount 를
  못 잡는다. 안 하면 옛 코드를 측정한다
- **nginx.conf 를 고쳤으면 `docker restart nginx`** (2026-08-22 신규 실측).
  파일 단위 bind mount 라 편집기가 inode 를 갈아치우면 **컨테이너가 잘린 뷰를
  본다** — 호스트 165줄인데 컨테이너는 130줄을 보고 `nginx -s reload` 가
  `unexpected end of file` 로 실패했다. 호스트에서 돌린 `nginx -t` 는 통과하므로
  **문법 검사만 믿으면 원인을 못 찾는다.** reload 실패라 서비스는 구 설정으로
  살아 있었다(nginx 는 나쁜 설정을 적용하지 않는다)
- 계기 추가가 그 자체로 오버헤드다 — 히스토그램 버킷을 과하게 잡지 않는다
- **`neemba_consumer_unparseable_total` 를 매 실행 전후로 대조**할 것 (D4)
