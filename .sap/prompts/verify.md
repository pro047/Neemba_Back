### 테스트 위치와 명령

- 셸의 검증 명령 (red·green 판정에 전부 쓰인다):
  - `cd services/python && .venv/bin/python -m pytest -q`
  - `cd services/node && npm test` (vitest)
  - `cd services/node && npx tsc --noEmit`
- python 테스트는 `services/python/tests/test_<이름>.py`, node 테스트는 `services/node/test/<이름>.test.ts`
- **시스템 python 으로 돌리면 미설치 의존성 때문에 수집 단계에서 에러가 난다** — `.venv/bin/python` 만 쓴다

### 작성 규칙

- **AAA 패턴**(Arrange-Act-Assert)으로 쓴다
- 테스트명은 조건 → 기대 형식. 이 저장소는 **영어 함수명**을 쓴다
  (`test_unparseable_message_increments_counter` 처럼)
- **DB 모킹 금지** — 테스트 DB(`services/python/tests/conftest.py` 의 `pg_pool` 픽스처)를 쓴다.
  `conftest.py` 는 보호 파일이라 **픽스처를 가져다 쓰기만 하고 고치지 않는다**. 외부 유료 API(DeepL·Google STT)는 모킹한다
- 테스트 간 의존성 금지. 각 테스트는 단독 실행 가능해야 한다.
  **prometheus 메트릭은 프로세스 전역 레지스트리라 값이 누적된다** — 절대값이 아니라
  `before` 를 읽고 델타를 검증할 것
- **좋은 테스트는 계측을 지웠을 때 실패한다.** 값이 존재하는지만 보는 테스트는
  구현이 사라져도 통과할 수 있다
