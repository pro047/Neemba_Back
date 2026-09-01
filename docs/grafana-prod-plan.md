# Prometheus·Grafana prod 전환 계획 (2026-08-31 작성 · 구현 미착수)

큐 14번. 조사 근거는 `observation-2026-08-31.md` §6.

> ## ⚠️ 배포 전 반드시 먼저 할 것 — 순서를 어기면 배포가 통째로 죽는다
>
> `docker-compose.prod.yml` 의 Grafana 는 `${GF_SECURITY_ADMIN_USER:?}` ·
> `${GF_SECURITY_ADMIN_PASSWORD:?}` 를 요구한다. **compose 의 interpolation 실패는
> 해당 서비스만이 아니라 파일 전체 파싱을 중단시킨다**(실측) — `deploy.yml:149` 가
> `set -euo pipefail` 아래에서 `up -d --build --force-recreate` 를 돌리므로,
> 시크릿에 이 두 줄이 없으면 **Grafana 와 무관한 앱 변경까지 포함해 배포 전체가 실패**한다.
>
> **머지 전에 이 순서로:**
>
> 1. 로컬 `.env.prod` 의 `GF_SECURITY_ADMIN_USER` / `GF_SECURITY_ADMIN_PASSWORD` 를
>    **`admin/admin` 이 아닌 값**으로 교체한다 (2026-08-31 기준 둘 다 `admin` 이었다)
> 2. `gh secret set ENV_PROD -R pro047/Neemba_Back < .env.prod`
> 3. 그다음에 release PR 을 머지한다
>
> 서버의 `.env.prod` 를 손으로 고치는 것은 무의미하다 — 매 배포마다 시크릿이 덮어쓴다
> (2026-07-12 확인). **`:?` 대신 `:-admin` 기본값을 쓰면 배포는 안 막히지만 prod 에
> `admin/admin` 이 조용히 남는다** — 그래서 시끄럽게 실패하는 쪽을 골랐다(사용자 결정 2026-08-31).
>
> **왜 자격증명이 문제인가**: Grafana admin 은 데이터소스를 임의 URL 로 만들 수 있고,
> 이 EC2 는 IMDS hop limit 2 라 컨테이너에서 `169.254.169.254` 에 닿는다(2026-07-15 실증).
> 익명 Viewer 는 데이터소스를 못 만들어 막혀 있지만 admin 로그인은 그 제약을 우회한다.
> nginx `auth_basic` 이 앞을 막으므로 즉시 위험은 아니나, 기본 자격증명을 prod 에 둘 이유가 없다.

**이 작업의 성격**: 새로 만드는 것이 아니라 **dev 에만 있는 것을 prod 로 올리는 것**이다.
`infra/prometheus/` · `infra/grafana/provisioning/` 은 이미 develop 에 있고
`docker-compose.dev.yml` 에 3개 서비스가 정의돼 있다. **prod compose 에만 없다.**

## 0. 확정된 결정

| 항목 | 결정 | 근거 |
|---|---|---|
| 통합 방식 | **B — iframe 임베드** | C(모니터가 Prometheus 직접 쿼리)는 19패널 재구현 + 모니터 프런트엔드의 **의존성 0·번들러 없음(D7)** 결정 파기. 관측 문서 §6 |
| 노출 경로 | `monitor.neemba.app/grafana` **서브패스 프록시** | `nginx.conf:200-201` 의 `auth_basic` 이 하위 `location` 에 상속돼 **인증 지점이 하나로 유지**된다 |
| 역할 분담 | 시계열=Grafana · 현재상태=기존 모니터 | 합치면 둘 다 어정쩡해진다. 리스너 0·세션 목록은 실시간성이 중요하고 WS 가 이미 있다 |
| 대시보드 | **`perf.json` 재사용** (5곳 수정) | 19패널·PromQL 정상. 재작성 비용만 든다 |

## 1. `perf.json` 수정 5곳

`test_perf_dashboard.py` 가 **1 failed / 12 passed** 다. 5번만 테스트가 잡아준다.

| # | 수정 | 테스트가 잡나 |
|---|---|---|
| 1 | **nginx exporter 처리** — 아래 §2-3 참조. 미처리 시 `up{job="nginx"}` 가 **영구 0(빨간불)** 이 되어 진짜 장애와 구분 불가 | ✕ |
| 2 | 제목 `Neemba Perf (dev)` → prod 용으로 분리. `uid` 도 함께 | ✕ |
| 3 | `neemba_separator_duplicate_dropped_total` 추가 (② 유실 패널에 시리즈 1개) — release #111 이 막은 건수를 보는 **유일한** 메트릭 | ✕ |
| 4 | `time: now-15m`→`now-2h`, `refresh: 10s`→`30s` — 부하테스트용 설정이지 예배 1시간용이 아니다 | ✕ |
| 5 | **비-row 패널 14개에 `datasource: {type: prometheus, uid: "${DS}"}`** — 누락 시 조용히 빈 그래프 | **○** |

> 4번의 `refresh` 를 10s 로 두면 t3 에서 상시 쿼리 부하가 된다. 예배 관측에 10초 갱신은 불필요하다.

## 2. prod compose 배선

### 2-1. prod 관례를 따를 것 (dev 정의를 그대로 복사하면 안 된다)

`docker-compose.prod.yml` 의 기존 서비스는 전부 다음을 지킨다:

- `env_file: .env.prod`
- `logging: json-file` + `max-size: "10m"` + `max-file: "3"`
- `restart: unless-stopped`
- **`ports:` 를 쓰지 않는다** — 포트를 노출하는 서비스는 15개 중 **2개뿐**이다(47·74줄)

**dev 정의는 `9090`·`3001`·`9113` 을 전부 호스트로 노출한다. prod 에서는 셋 다 노출하지 않는다.**
Prometheus 는 인증이 없어 9090 을 열면 메트릭이 그대로 공개된다. Grafana 도 nginx 뒤에만 둔다.

### 2-2. 볼륨 2개 신설

dev 는 볼륨이 없어 데이터가 휘발한다. prod 는 `nats-data`·`pg-data`·`monitor-state` 관례를 따라:

```yaml
volumes:
  prometheus-data:   # 시계열. retention 명시 필수(§4)
  grafana-data:      # 대시보드 변경·사용자. 없으면 재시작마다 초기화
```

### 2-3. nginx exporter — **결정이 필요하다**

**prod nginx 에는 `stub_status` 가 없다**(실측: `infra/nginx/nginx.conf` 0건, dev 는 `dev/nginx.conf:127`).
그런데 `prometheus.yml` 에는 `nginx-exporter:9113` 잡이 이미 박혀 있다.

| 안 | 작업 | 대가 |
|---|---|---|
| **A. exporter 도 올린다** | prod nginx 에 `location = /nginx_status { stub_status; }` 추가(+ 내부 전용 접근 제한) · compose 에 exporter 추가 | nginx 설정 변경 = **송출 경로를 건드린다.** 예배 전에는 하지 말 것 |
| **B. 뺀다** | `prometheus.yml` 에서 nginx 잡 삭제 · `perf.json` 에서 `nginx_connections_active` 시리즈 삭제 | 연결 수 관측을 잃는다 |

**B 를 먼저 권한다.** nginx 연결 수는 지금까지 장애 판정에 쓴 적이 한 번도 없고,
A 는 송출 경로(nginx)를 건드리므로 **얻는 것 대비 위험이 크다.** 나중에 필요해지면 A 로 올린다.

## 3. nginx 라우팅

```nginx
# monitor.neemba.app 서버 블록(193줄~) 안, auth_basic 아래
location /grafana/ {
    proxy_pass http://grafana:3000/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    # iframe 임베드를 같은 오리진으로만 제한
    add_header Content-Security-Policy "frame-ancestors 'self'" always;
}
```

Grafana 쪽 환경변수:

```
GF_SERVER_ROOT_URL=https://monitor.neemba.app/grafana/
GF_SERVER_SERVE_FROM_SUB_PATH=true
GF_SECURITY_ALLOW_EMBEDDING=true
GF_AUTH_ANONYMOUS_ENABLED=true
GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
```

**익명 접근이 위험해 보이지만 아니다** — nginx `auth_basic` 이 앞을 막으므로 Grafana 에
도달하려면 먼저 Basic Auth 를 통과해야 한다. 인증 지점을 둘로 늘리지 않는 것이 목적이다.
**단 `ALLOW_EMBEDDING=true` 는 외부 사이트 임베드도 허용하므로 위 CSP 를 반드시 함께 건다.**

## 4. 보존과 자원

- **Prometheus retention 을 명시한다** — 기본 15일. `--storage.tsdb.retention.time=30d` 처럼
  박아두지 않으면 몇 달 뒤 디스크로 문제가 온다. 현재 디스크 여유 **29G**(실측)
- **스크레이프 간격은 15s 유지** — `prometheus.yml` 현재값. 줄일 이유 없다
- **t3.medium 버스터블 주의**: 현재 로드 **0.01** · 메모리 **2.5Gi available**(실측 2026-08-31 20:36)
  라 용량은 되지만, 상시 스크레이프는 **새 부하**다. **배포 후 며칠 CPU 크레딧 잔고를 볼 것**
  (`cpu-credit-watch.yml` 워크플로가 이미 있다). **현재 예측치 없음 — 미측정**

## 5. 모니터 페이지 iframe 뷰

기존 3뷰(`view-sessions`·`view-search`·`view-blips`) 옆에 4번째를 추가한다.

- 임베드는 **`/d-solo/` 로 패널 2~3개만** 붙인다. 19패널 전체는 모바일에서 못 읽는다.
  전체 대시보드는 `/grafana` 링크로 넘긴다
- **뷰 전환 시 iframe `src` 를 비운다.** 모니터는 `/ws/monitor` 로 실시간 자막을 받는데
  그 옆에서 iframe 이 갱신되면 모바일에서 리소스 경합이 날 수 있다(**추정·미측정**).
  안 쓰는 iframe 을 죽이면 예방된다
- **차트 라이브러리는 넣지 않는다** — D7(의존성 0·번들러 없음)을 유지한다

## 6. 검증 절차

1. `test_perf_dashboard.py` **13/13 통과** (현재 12/13)
2. dev 스택에서 `docker compose -f docker-compose.dev.yml up` → Grafana 19패널이 **전부 데이터를 그리는지** 눈으로 확인
   (§1-5 를 고치기 전에는 빈 그래프가 정상이었을 수 있다 — 고친 뒤 비교)
3. prod 배포 후: `up` stat 이 **전부 초록**인지. 빨간 job 이 있으면 §2-3 처리가 덜 된 것이다
4. `monitor.neemba.app/grafana/` 가 **Basic Auth 한 번으로** 열리는지 (Grafana 로그인 화면이 뜨면 익명 설정 실패)
5. 모니터 4번째 뷰에서 iframe 이 **뜨는지** — CSP 가 과하면 자기 자신도 막는다
6. **배포 후 3일간 CPU 크레딧 잔고 관측**

## 7. 미결정 2건

1. **§2-3 의 A/B** — nginx exporter 를 올릴지. **B 권장**
2. **`perf.json` 을 prod 용으로 가를지, 하나를 공유할지** — 제목/시간범위가 dev 와 다르므로
   가르는 쪽이 자연스러우나, 파일이 둘이면 패널 추가 시 양쪽을 고쳐야 한다.
   **대안**: 하나로 두고 제목에서 `(dev)` 만 떼고 시간범위는 prod 기준으로 맞춘다(dev 에서는
   시간범위를 손으로 좁히면 된다). **이쪽을 권한다**

## 8. 착수 순서

```
① perf.json 5곳 수정 → test 13/13
② prod compose 2서비스 + 볼륨 2개
③ nginx location + Grafana env
④ dev 에서 전체 확인 (검증 1·2)
⑤ 배포 → 검증 3·4·6
⑥ 모니터 4번째 뷰 (검증 5)
```

**⑥은 ⑤ 이후로 미룬다** — Grafana 가 prod 에서 뜨기 전에는 임베드 URL 이 확정되지 않는다.

## 9. 구현 결과 (2026-08-31)

§8 의 ①~③ 완료. 배포는 아직이다.

| 파일 | 변경 |
|---|---|
| `infra/grafana/provisioning/dashboards/perf.json` | 5곳 수정 → `test_perf_dashboard.py` **13/13** |
| `infra/prometheus/prometheus.yml` | nginx 잡 제거(§7-1) |
| `docker-compose.prod.yml` | prometheus·grafana 2서비스 + 볼륨 2개 |
| `infra/nginx/nginx.conf` | `/grafana/` location + CSP `frame-ancestors 'self'` |
| `.gitignore` | `!infra/grafana/provisioning/dashboards/*.json` 예외 |

**검증**: nginx `-t` 통과(스텁 호스트·자체서명 인증서로 컨테이너에서 실행) ·
compose `config` 통과 · python 217 passed/5 skipped · node 147 passed.

### 구현 중 내린 결정 3가지

1. **`proxy_pass` 에 후행 URI를 붙이지 않았다.** `proxy_pass http://grafana:3000/;`(후행 슬래시)를
   쓰면 `/grafana/` 접두사가 잘리는데, `GF_SERVER_SERVE_FROM_SUB_PATH=true` 는 접두사를 포함해
   받겠다는 선언이다. 자르면 첫 화면은 뜨지만 **Grafana 가 내보내는 자산 URL 이 전부 404** 가 된다
2. **upstream 을 `set $grafana_upstream` 으로 우회했다.** 리포 관용구(`nginx.conf:10` 의
   `resolver 127.0.0.11`)를 따랐다. 직접 지정하면 nginx 가 **기동 시점에 DNS 를 해석**하므로
   grafana 컨테이너가 없거나 죽으면 **nginx 자체가 크래시 루프**에 빠진다 — 자막 송출 경로가
   관측 스택 때문에 죽는 구조가 된다
3. **이미지 태그를 `latest` 에서 고정으로 바꿨다** (`prom/prometheus:v3.1.0`,
   `grafana/grafana:11.5.1`). dev 는 `latest` 지만 prod 는 `nginx:1.27-alpine`·`postgres:16-alpine`·
   `nats:2.10.17-alpine` 로 전부 고정하는 관례다

### 정정 — `datasource` 누락은 런타임 고장이 아니었다

§1-5 를 쓸 때 "누락 시 조용히 빈 그래프" 라고 했으나, `provisioning/datasources/prometheus.yml` 에
**`isDefault: true`** 가 있어 런타임에는 기본 데이터소스로 붙는다. 실제 성격은 **프로비저닝
이식성 계약 위반**이지 즉시 고장이 아니다. 고치는 판단 자체는 유효하다.

### `stt_overview.json` 삭제 (2026-08-31, 사용자 승인)

`.gitignore` 의 `*.json` 에 가려 **1년간 보이지 않던 대시보드**가 예외 줄을 추가하면서 드러났다.
`dashboards.yml` 이 디렉터리 통째를 프로비저닝하므로 prod 에 함께 올라갔을 것이다.

**6패널 중 5개가 존재하지 않는 메트릭을 참조**한다 — 2025-08-14 에 만들다 만 스캐폴드다.
**한 번도 커밋된 적이 없어** 삭제하면 복구가 불가능하므로 쿼리를 여기 남긴다:

| 패널 | 쿼리 | 실재 |
|---|---|---|
| Targets UP | `sum(up)` | ✅ |
| Node API Requests (rate) | `rate(demo_requests_total[1m])` | ❌ Grafana 예제 이름. 우리 것은 `neemba_requests_total` |
| Python WS Messages (rate) | `rate(python_ws_messages_total[1m])` | ❌ |
| Nginx Active Connections | `nginx_connections_active` | ❌ exporter 없음 |
| Nginx HTTP Requests (rate) | `rate(nginx_http_requests_total[1m])` | ❌ |
| Nginx Accepted Connections (rate) | `rate(nginx_connections_accepted[1m])` | ❌ |

`sum(up)` 은 `perf.json` ⑤ 의 "타겟 up" stat 이 대체한다.

> **교훈**: `.gitignore` 의 광범위한 `*.json` 이 산출물을 1년간 감췄다. 예외 줄을 넣기 전까지
> **`git status` 에도 안 나오므로** 존재 자체를 알 수 없었다. 같은 패턴이 다른 디렉터리에도
> 있을 수 있다.

### 다음 (§8 의 ④~⑥)

④ dev 스택에서 19패널 렌더 확인 → ⑤ 배포 + 검증(§6) → ⑥ 모니터 4번째 뷰

### `/security-review` · `/code-review high` 반영 (2026-08-31)

**보안 리뷰**: 신규 취약점 0건. `auth_basic` 상속·우회 경로 부재·`add_header` 상속 취소
없음·변수 `proxy_pass` 의 업스트림이 정적 리터럴·Prometheus 관리 API 미개방을 각각 확인했다.
다만 `.env.prod` 는 gitignore 라 리뷰 범위 밖이었고, **거기 `admin/admin` 이 있었다**(위 경고 절).
부분 조치로 Grafana 에서 `env_file: .env.prod` 를 떼고 필요한 두 값만 `${}` 로 주입한다 —
`DEEPL_API_KEY`·`POSTGRES_PASSWORD`·`NATS_PASSWORD`·`RTMP_PUBLISH_KEY` 를 서드파티
이미지에 넣을 이유가 없다.

**코드 리뷰 5건 중 4건 수정**(HIGH 1건은 위 경고 절로 처리):

| 등급 | 발견 | 조치 |
|---|---|---|
| medium | `time() - neemba_hub_last_broadcast_timestamp_seconds` 가 게이지 미설정 시 `time() - 0` = **약 56.7년**을 표시한다. 매 배포 직후~그날 첫 번역까지가 그 구간이고 **예배 전에 이 패널을 여는 시점이 정확히 거기다** | `time() - (…_seconds > 0)` 로 감싸 값이 없으면 패널이 빈다 |
| low | `location /grafana/` 는 끝 슬래시 있는 형태만 매칭 → `/grafana` 가 `location /` 의 SPA 로 떨어져 모니터 페이지가 200 을 준다. §5 가 링크로 쓰겠다고 한 그 형태다 | `location = /grafana { return 301 /grafana/; }` |
| low | `test_perf_dashboard.py` 의 `NGINX_METRIC_NAMES` 허용목록이 남아, nginx 패널을 되살리면 **테스트는 통과하고 그래프만 빈다** | 항목 제거 |
| low | `prometheus.yml` 은 **dev 도 같이 마운트**한다 — nginx 잡을 지우자 dev 의 `nginx-exporter` 와 `depends_on` 이 고아가 됐다 | dev compose 에서 제거. **단 `docker-compose.dev.yml` 은 gitignore(`​.gitignore:15`)라 이 수정은 커밋되지 않는다 — 다른 환경에서는 고아 exporter 가 그대로 뜬다** |

리뷰어가 실측으로 기각한 가설 1건도 기록해 둔다: nginx 가 `Authorization: Basic` 을
그대로 전달하면 Grafana 자체 basic auth 가 401 을 낼 것이라는 의심 — 실제로 컨테이너를
같은 env 로 띄워 확인한 결과 **200 + 익명 Viewer 권한**이었다(11.5.1·12.4.1 동일).

**최종 검증**: `test_perf_dashboard.py` 13/13 · python 217 passed/5 skipped ·
node 147 passed · nginx `-t` 통과 · prod·dev compose `config` 통과.

## 10. 배포 완료 (2026-09-01, release #114)

워크플로 `33498615350` **completed success**. prod 커밋은 PR #114 머지본.

**외부 검증 5/5** (SSH 없이 확인 가능한 범위):

| 확인 | 기대 | 실제 |
|---|---|---|
| `neemba.app/health` | 200 | **200** — 기존 서비스 무사 |
| `monitor.neemba.app/` | 401 | **401** — Basic Auth 유지 |
| `/grafana` (끝 슬래시 없음) | 301 | **301 → `/grafana/`** — §9 의 코드리뷰 수정이 실제로 작동 |
| `/grafana/` (인증 없이) | 401 | **401** — `auth_basic` 상속 확인 |
| CSP 헤더 | `frame-ancestors 'self'` | **응답에 존재** |

`X-Frame-Options` 가 응답에 **없는 것도 의도대로**다 — `ALLOW_EMBEDDING=true` 가 그것을 없애고
CSP 가 유일한 경계가 되는 구조다.

### ⚠️ 함정 — 웹뷰에서는 401 페이지가 그대로 뜬다 (2026-09-01 실측)

배포 직후 `https://monitor.neemba.app/grafana/` 가 401 로 보였는데 **설정 문제가 아니었다.**
앱 내장 브라우저(웹뷰)는 Basic Auth 다이얼로그를 띄우지 못해 401 페이지를 그대로 렌더한다.
**Chrome/Safari 에서는 정상**이었다. `watch-service` 스킬이 모니터 UI 에 대해 기록해 둔 것과
같은 함정이며, `/grafana/` 도 같은 오리진·같은 realm(`Basic realm="Neemba Monitor"`)이라
동일하게 적용된다.

**구분법**: 같은 브라우저에서 `monitor.neemba.app/` 도 401 이면 웹뷰 문제다.
모니터 페이지는 열리는데 `/grafana/` 만 401 이면 그건 다른 문제다(자격증명·컨테이너 상태).

### 남은 확인 2건

- **`⑤ 생존·오디오 입력` 의 `타겟 up` 이 전부 초록인지** — `nginx` job 이 빨갛게 남아 있으면
  Prometheus 가 옛 설정을 들고 있다는 뜻이다(= 컨테이너 재생성이 덜 됨)
- **배포 후 3일간 CPU 크레딧 잔고** — t3.medium 에 상시 스크레이프는 새 부하다.
  `cpu-credit-watch.yml` 워크플로가 이미 있다. **현재 예측치 없음(미측정)**

### 다음: §8 의 ⑥ — 모니터 페이지 iframe 뷰

Grafana 가 prod 에 떴으므로 임베드 URL 이 확정됐다: `https://monitor.neemba.app/grafana/d-solo/neemba-perf?panelId=<N>`.
기존 3뷰(`view-sessions`·`view-search`·`view-blips`) 옆에 4번째를 붙인다.
패널 2~3개만 고르고, 뷰 전환 시 iframe `src` 를 비운다(§5).
