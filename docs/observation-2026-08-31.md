# 2026-08-31 저녁예배 관측 — 첫 마이크 세션 기준선 + release #113 실전 검증

세션 `ee2827ad-b789-4377-88e7-91e26a9bf761` · 20:01:15~20:42:44 KST (41.5분) · 브로드캐스트 416건.
**정규 예배일(주일·수요)이 아닌 월요일 저녁**이고, **입력이 RTMP 가 아니라 마이크**였다 — 그래서
8/30 주일예배와 나란히 놓으면 입력 경로 비교가 된다.

**회수한 원본** (이 맥에만 있다):

| 파일 | 규모 |
|---|---|
| `~/neemba-source-2026-08-31.txt` | 1,557줄 — node `published :` 한국어 원문 |
| `~/neemba-translations-2026-08-31.txt` | 503줄(오늘분 416) — python `hub: broadcast:` |

## 1. release #113 실전 검증 — **2회 확인, 의도적 재현 불필요했다**

큐 10번에 "빈도가 낮아 **증분 0 이 정상**이라 배포 직후 관측으로 정상 동작을 확인할 수단이
없다 — 확인하려면 의도적 재현이 필요하다"고 적어뒀는데, **재현 없이 실전에서 두 번 잡혔다.**

| # | 세션 | 등록 | 자동 종료 | 유예 |
|---|---|---|---|---|
| 1 | `7c6cf0a7` | 2026-08-30 15:50경 | **8/30 16:20:15** | 30분 |
| 2 | `b65fe70d` | 2026-08-31 20:07:02 | **8/31 20:37 경** | 30분 |

2번은 **관측 중에 예측 → 확인**까지 됐다. 20:07:02 등록 + 30분 = 20:37:02 를 미리 적어두고
tick 을 돌렸고, 20:39 tick 에서 종료 로그가 잡혔다:

```
sessionId: b65fe70d-4dfb-4cc5-8a9f-2a9e9b6e7eb5 stopped
hub: detached b65fe70d-4dfb-4cc5-8a9f-2a9e9b6e7eb5 sockets=0
monitor: alerted: ✅ stt_paused 복구 (지속 29분 3초)
monitor: alerted: ℹ️ 방송 없음 — 발행자가 끝내 붙지 않아 세션을 정리했습니다 1회
```

`stt_paused` **지속 29분 3초**가 유예 30분을 뒷받침한다. 종료와 함께 `stt_paused{b65fe70d}`
시리즈가 사라지고 `ffmpeg_stale_total` 증분이 +45 → +20 → **+0** 으로 잦아들었다.

**발생 경위(2번)**: 20:07:01 에 직전 세션 `4b5649f4` 가 닫히고, 20:07:02 [시작] →
20:07:18 [정지](P1 D4 no-op) → 20:07:20 다시 [시작]. **사용자의 재시도 조작이 원인이고
장애가 아니다.** [정지]가 no-op 인 한 이 패턴은 계속 나오며, #113 이 30분 뒤 치운다.

### 타이머 무장 조건 — 마이크 세션도 대상이다 (오해 정정)

`SessionLifecycle.ts:215` 의 `if (!publisherClientId) scheduleAutoStop("no_publisher")` 는
**세션 시작 시 publisher 슬롯이 비면 마이크 여부와 무관하게 무장한다.** 핸드오프 큐 10번의
"마이크 세션은 별도 수명 경로라 범위 밖" 은 **수명 관리 전반**을 말한 것이지 이 타이머의
무장 조건이 아니다 — 관측 중 이 문장을 근거로 "마이크면 타이머가 안 걸린다"고 잘못 판단했다가
정정했다. 마이크 세션이 안 죽는 이유는 **취소 조건에 pcm 청크 도착이 있기 때문**이다.

## 2. 마이크 입력 품질 — 서버측 기준선 (큐 3번)

**같은 서버, 다른 입력 경로.** 8/30 은 RTMP(OBS), 8/31 은 마이크다.

| 지표 | 8/30 RTMP | 8/31 마이크 | 차이 |
|---|---|---|---|
| 줄수 / 구간 | 875줄 / 68.7분 | 416줄 / 41.5분 | — |
| 분당 출력 | 12.7줄 | **10.0줄** | −21% |
| 단어수 중앙값 | 8 | **7** | −1 |
| 3단어 이하 파편 | 17.9% | **24.3%** | **+6.4%p** |
| 문장부호로 끝남 | 61.5% | **47.6%** | **−13.9%p** |

**"문장부호로 끝남" 13.9%p 하락이 가장 큰 격차다.** 문장이 끝까지 잡히면 마침표로 끝나고
중간에 잘리면 안 끝나므로, 이것이 STT 분절 품질의 대리 지표다. 파편률 +6.4%p 는 같은 현상의
다른 얼굴이다.

> **⚠️ 이 비교를 인과로 읽지 말 것 — 교란 변수가 통제되지 않았다.**
> ① 8/30 은 설교+찬양+기도, 8/31 은 **대화체 간증**이다. 대화체는 원래 문장이 짧고 안 끝난다
> (`Exactly.` `No.` `Yes, there is.`) ② 화자가 다를 수 있다 ③ 표본이 416 vs 875 다.
> **13.9%p 중 얼마가 마이크 탓이고 얼마가 대화체 탓인지 이 데이터로는 못 가른다.**
> 제대로 재려면 같은 발화를 두 경로로 동시에 흘려야 한다.

**그럼에도 쓸모가 있다**: 앱을 고친 뒤 **같은 성격의 발화**로 다시 재면 before/after 가 된다.
이 표를 그 before 로 쓴다.

**원인 계측은 서버가 아니라 앱에 있다**(큐 3번). `mvp` `06a813c` 가 캡처 레이트 3중 비교를
이미 넣었으나 `log.dart:14` 의 `logD` 가 `kReleaseMode` 에서 no-op 이라 릴리스 빌드로는
안 찍힌다. **오늘처럼 마이크가 실사용되는 자리가 있으니 debug/profile 빌드를 넣으면 바로 나온다.**

## 3. 최대 공백 — 감시 시작 전 1건

| 공백 | 구간 | 판정 |
|---|---|---|
| **222.6초** | 20:03:02 → 20:06:45 | **감시 시작(20:09) 전이라 미조사** |
| 35.2초 | 20:34:00 → 20:34:36 | 발화 공백 추정 |
| 35.0초 | 20:35:28 → 20:36:03 | 〃 |

**222초 공백은 원인을 모른다.** 관측을 20:09 에 시작했고 그때는 이미 지나간 뒤였다.
직전 세션 `4b5649f4`(20:01 마이크 연결)에서 `b65fe70d`/`ee2827ad`(20:07) 로 넘어가는
전환 구간과 겹치므로 **세션 교체 중 공백일 가능성이 높다(추정)**. 로그는 회수해 뒀으니
필요하면 나중에 팔 수 있다.

`publish_buffer_dropped_total` 이 이 시간대에 **+1** 되어 총 1이 됐고
(`publish buffer: span dropped after stop, total dropped=1`), 이후 예배 끝까지 +0 이었다.
**자막 1건 유실이 실제로 있었다.**

## 4. 고유명사 오인식 — `나사로` (큐 1번, **위험 케이스**)

원문 전수 집계 결과 **`나사로` 를 7회 중 3회만 맞혔다.**

| 판정 | 원문 조각 |
|---|---|
| ✅ | `아브라함과 그의 품에 있는 **나사로**를 보고` |
| ✅ | `**나사로**가 이름하는 거룩한 거지가` |
| ✅ | `아브라함으로 몸에 있는 **나사로**는` |
| ❌ | `이 **나사를** 천사의천사들이` → `This screw is an angel's` |
| ❌ | `그 **나사도**이 사람이 벌어진 채` |
| ❌ | `하나님이 **나사를** 내 아버지 집에` |
| ❌ | `**나사를** 죽어 가지고 여기 왔는데 살려 가지고` → `came here with the screw all stripped` |

> **`나사 → 나사로` 는 `transcriptNormalization` 에 넣으면 안 된다.**
> `나사`(螺絲, screw)는 **일반명사 동형**이다. 무조건 치환하면 진짜 나사 이야기가 망가진다.
> 큐 1번의 `에서`(Esau) 제외 규칙과 **같은 부류**이며, 오늘 그 부류의 두 번째 사례가 나왔다.
> 조사 결합형(`나사를`·`나사도`)까지 있어 문맥 없이는 가를 수 없다.

**의미**: 큐 1번의 "조사·일반명사 동형은 제외" 항목이 예외가 아니라 **반복되는 패턴**임이
확인됐다. 제외 목록을 별도로 관리할 가치가 있다.

## 5. 번역 품질 — 오역 책임은 이번에도 전부 STT

tick 중 번역기 오류로 의심했던 것을 원문으로 확인한 결과 **원문이 이미 깨져 있었다**:

```
번역: The Calling is not merely a matter of religion, electricity, and strategy
원문: 부르심도 종교는 전기와 전략으로뿐만 아니라 온 세계 마귀와 싸우니
```

`electricity` 는 번역기가 지어낸 것이 아니라 원문의 **`전기`** 를 그대로 옮긴 것이다.
8/30 의 `Data: No data available`(원문 `자료 자료 없습니다`)과 **같은 구조**다 —
SOURCE 섹션 없이 영문만 봤으면 두 번 다 번역기 버그로 오판했을 것이다.

STT 가 제대로 받은 구간의 번역은 온전하다:

> `If a dead person were to speak while still alive, wouldn't you hear them?` (눅 16:31)
> `On a drop of water on my fingertip` (눅 16:24)

## 6. 모니터링 대시보드 — 조사 결과 (구현 미착수)

예배 중 사용자 요청으로 조사했다. **결론: 새로 만들 게 아니라 dev 에만 있는 것을 prod 로
올리는 일이다.**

| 항목 | 상태 |
|---|---|
| `infra/prometheus/prometheus.yml` | **develop 에 커밋됨** |
| `infra/grafana/provisioning/datasources/prometheus.yml` | **develop 에 커밋됨** |
| `infra/grafana/provisioning/dashboards/dashboards.yml` | **develop 에 커밋됨** |
| `docker-compose.dev.yml` | prometheus·grafana·nginx-exporter **3개 정의됨**(149·164·138줄) |
| `docker-compose.prod.yml` | **없음** (서비스 15개 중 미포함) |
| `infra/grafana/.../perf.json` | **미커밋** — 워크트리 `pipeline/perf-grafana-dashboard` 에만 |
| `services/python/tests/test_perf_dashboard.py` | **미커밋** — 〃 |

**EC2 여유는 있다 (실측 2026-08-31 20:36)**: t3.medium · 메모리 1.2Gi/3.7Gi 사용
(**2.5Gi available**) · 디스크 29G 여유 · 로드 **0.01**. Prometheus+Grafana 는 통상
300~500MB 라 문제없다. **단 t3 는 버스터블이고 상시 스크레이프는 새 부하다 — 배포 후
CPU 크레딧 잔고를 며칠 봐야 한다(현재 예측치 없음, 미측정).**

### `perf.json` 검토 — 재작성 불필요, 5곳 수정

19패널(5행). PromQL 수준에서 지적할 것이 없다 — `$__rate_interval` 사용,
`histogram_quantile(sum(rate(...)) by (le))` 정석, 유실 카운터는 `increase(...[5m])`.
패널 제목이 성능 계획 절 번호를 달고 있어 존재 이유가 추적된다.

의존 메트릭도 실재한다: node 는 `app.ts:58` 에서 `collectDefaultMetrics({register})` 를
호출하고 `Registry.merge` 로 합쳐 노출하므로(`app.ts:77-79`) `nodejs_eventloop_lag_*` ·
`process_open_fds` · `process_max_fds` 가 나온다. python 은 `prometheus_client` 기본
컬렉터라 `process_cpu_seconds_total` · `python_gc_collections_total` 이 나온다.

**고칠 4가지:**

1. **`nginx_connections_active` 가 prod 에서 죽는다.** `nginx-prometheus-exporter` 는
   dev compose 에만 있는데 `prometheus.yml` 에는 `nginx-exporter:9113` 잡이 박혀 있다.
   그대로 올리면 해당 패널이 비고, 더 나쁘게는 **⑤ "타겟 up" stat 이 `nginx` job 에 대해
   영구 0(빨간불)이 되어 진짜 장애와 구분되지 않는다.** exporter 를 prod 에도 넣거나,
   잡과 두 패널을 뺀다
2. **제목이 `Neemba Perf (dev)`** (uid `neemba-perf`) — prod 용으로 갈라야 한다
3. **`neemba_separator_duplicate_dropped_total` 이 19패널 어디에도 없다** — release #111 이
   실제로 막은 건수를 보여주는 유일한 메트릭이고, 지금 "관측만 남았다"고 되어 있는 항목이다.
   ② 유실 패널에 시리즈 1개 추가면 된다
4. **`time: now-15m` / `refresh: 10s`** — 부하 테스트용 설정이다. 예배 1시간을 보려면
   `now-2h` 가 맞다
5. **비-row 패널 14개에 `datasource` 누락** — `{"type":"prometheus","uid":"${DS}"}` 를
   넣어야 한다. **이 항목만 테스트가 이미 잡아준다**(아래 실행 결과). 기계적 변환이라
   스크립트 한 번이면 끝나고 `test_perf_dashboard.py` 가 즉시 검증한다

**미커밋 사유 확정 (2026-08-31 실행) — 테스트가 실패해서 멈춘 것이다.**

```
1 failed, 12 passed
FAILED test_every_non_row_panel_and_target_uses_the_DS_template_datasource
```

**비-row 패널 14개 전부가 `datasource` 가 `None`** 이다. 대시보드에 `DS` 템플릿 변수가
선언돼 있는데 패널들이 그것을 참조하지 않는다 — 프로비저닝으로 데이터소스를 주입하는
구조에서 패널이 데이터소스를 명시하지 않으면 Grafana 는 기본 데이터소스에 붙거나,
없으면 **조용히 빈 그래프**를 그린다.

```
panels not on {'type': 'prometheus', 'uid': '${DS}'}:
[(2,None),(3,None),(4,None),(6,None),(7,None),(9,None),(10,None),
 (12,None),(13,None),(14,None),(16,None),(17,None),(18,None),(19,None)]
```

**나머지 12개는 통과한다** — 지표 이름 계약(PromQL 이 참조하는 메트릭이 실제 코드에
존재하는지), `.gitignore` 예외 줄, 구조 검증 전부 정상이다.

> **정정**: 이 절을 처음 쓸 때 `verify.result.json` 의 `is_error: false` 를 근거로
> "실패해서 멈춘 게 아니라 커밋 직전에 세션이 끝난 쪽" 이라고 추정했는데 **틀렸다.**
> 파이프라인의 `is_error` 는 **에이전트가 정상 종료했는지**를 뜻하지 **테스트가
> 통과했는지**를 뜻하지 않는다. 파이프라인 결과 파일을 게이트 통과의 근거로 쓰지 말 것.

### 통합 방식 — B(iframe 임베드) 우세

`monitor.neemba.app` 은 nginx 가 `auth_basic` 으로 통째로 감싸므로(`nginx.conf:200-201`)
그 아래 `location` 을 추가하면 **Basic Auth 가 상속된다** — 인증 지점이 하나로 유지된다.

C(모니터가 Prometheus 를 직접 쿼리)를 기각한 근거 2가지:

1. **19패널을 TypeScript 로 재구현해야 한다** — 이미 만든 산출물이 버려진다
2. **모니터 프런트엔드는 의존성이 0이고 그것이 의도된 결정이다** — `package.json` 설명에
   `tsc 단일 출력, 번들러 없음, 산출물(app.js) 커밋 (D7)` 이라 박혀 있다. 차트 라이브러리
   도입은 이 결정을 깬다

**단 역할은 나눈다**: 현재 상태(리스너 0·세션 목록·유예 잔여)는 **모니터가 직접** 그리고
(실시간성이 중요하고 WebSocket 이 이미 있다), 시계열은 **Grafana iframe** 에 맡긴다.
모바일 대비로 임베드는 `/d-solo/` 패널 2~3개만 붙이고 전체는 `/grafana` 링크로 넘긴다.

**남은 위험**: 모니터 페이지는 `/ws/monitor` 로 실시간 자막을 받는데 그 옆에서 iframe 이
갱신되면 모바일에서 리소스 경합이 날 수 있다(**추정, 미측정**). 뷰 전환 시 iframe `src` 를
비우는 처리로 예방한다. 그리고 `GF_SECURITY_ALLOW_EMBEDDING=true` 는 외부 사이트 임베드도
허용하므로 nginx 에 `Content-Security-Policy: frame-ancestors 'self'` 를 함께 박는다.
