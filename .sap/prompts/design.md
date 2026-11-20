### 읽을 문서

- `$ROOT/docs/handover-2026-08-02.md` — **현행 핸드오프**. 현황·우선순위·지뢰가 여기 있다
- `$ROOT/docs/` 의 해당 계획 문서(`*-plan.md`) — 있으면 **그것이 정본**이다. 설계를 새로 짜지 말고
  그 계획의 미완료분을 구현 가능한 형태로 내려 쓴다. 계획과 어긋나면 그 사실을 명시한다
- 문서 규칙: 계획 문서가 정본이고 핸드오프는 현황 레이어만 갱신한다. 복제하지 않는다.
  **핸드오프·계획 문서는 이 단계에서 수정하지 않는다**

### 구조

실시간 예배 통역 파이프라인이다. 요청/응답 API 가 아니라 **상시 스트림**이다.

```
node (STT·RTMP·마이크 업링크) → NATS → python (문장분리·DeepL·허브) → WS 청취자
```

- 두 스택: `services/node` (TypeScript, vitest), `services/python` (pytest)
- 셸의 검증 명령 (전부 통과해야 green — CI `ci.yml` 과 같은 검사):
  - python `cd services/python && .venv/bin/python -m pytest -q`
  - node `cd services/node && npm test` (vitest)
  - node 타입 검사 `cd services/node && npx tsc --noEmit`
- **시스템 python 으로 돌리면 `prometheus_client`·`nats`·`deepl` 미설치로 수집 단계에서
  에러가 난다** (2026-10-01 실측 17건) — 이걸 코드 결함으로 오독하지 말 것
- 메트릭은 python `src/monitoring/metrics.py`, node `prom-client` 기본 레지스트리.
  Prometheus·Grafana 는 dev 와 prod 둘 다 있다 (`docker-compose.prod.yml` 의 `prometheus`·`grafana`
  서비스 — 포트를 열지 않고 Grafana 는 nginx auth_basic 뒤)
- 공개 인터페이스 절에는 함수 시그니처·API 스펙·스키마와 함께 **설정 파일 구조**도 적는다

### 근거 — 이 저장소에서 특히

- **"X 가 없다" 는 부정 주장은 `services/node` 와 `services/python` 을 둘 다 본 뒤에만** 쓴다.
  한쪽만 grep 하고 단정한 것이 이 저장소에서 실제로 여러 번 나온 오류다
- [사람 확인 필요] 의 전형: 실제 STT·DeepL 호출, 실예배 타이밍, 청취자 화면 육안 확인

### 파일 목록 관례

- 짝 테스트 파일: python 은 `services/python/tests/test_<이름>.py`, node 는 `services/node/test/<이름>.test.ts`
- 사람 승인 사항인 의존성 파일: `services/node/package.json`·`services/python/pyproject.toml` (락파일 포함)
- 러너·린터·타입 검사 설정: `services/node/tsconfig.json`·`services/node/.eslintrc.cjs`·`services/python/tests/conftest.py`
