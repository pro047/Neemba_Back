# Prometheus·Grafana prod 전환 계획 (2026-08-31 작성 · 구현 미착수)

큐 14번. 조사 근거는 `observation-2026-08-31.md` §6.

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
