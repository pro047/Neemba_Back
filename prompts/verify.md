# 단계: 검증

기능: $FEATURE

## 출력 (이 파일 하나만 쓴다 — 테스트 파일 작성은 별개)

$WORK/VERIFY.md

## 입력 (읽기만, 절대 수정 금지)

- `$WORK/DESIGN.md` — "검증 기준" 섹션이 테스트의 근거다
- `$WORK/IMPL.md` — 실제 구현 내용

## 프로젝트 테스트 규칙 (neemba)

- python 테스트는 `services/python/tests/`, 실행은 **`.venv/bin/python -m pytest -q`**.
  시스템 python 으로 돌리면 미설치 의존성 때문에 수집 단계에서 에러가 난다
- node 테스트는 vitest. `services/node` 에서 `npm test`
- **AAA 패턴**(Arrange-Act-Assert)으로 쓴다
- 테스트명은 조건 → 기대 형식. 이 저장소는 **영어 함수명**을 쓴다
  (`test_unparseable_message_increments_counter` 처럼)
- **DB 모킹 금지** — 테스트 DB(`conftest.py` 의 `pg_pool` 픽스처)를 쓴다.
  단 외부 유료 API(DeepL·Google STT)는 모킹한다
- 테스트 간 의존성 금지. 각 테스트는 단독 실행 가능해야 한다.
  **prometheus 메트릭은 프로세스 전역 레지스트리라 값이 누적된다** — 절대값이 아니라
  `before` 를 읽고 델타를 검증할 것

## 할 일

1. DESIGN.md 의 검증 기준을 실행 가능한 테스트로 옮긴다.
2. 테스트를 작성/수정한다. **ALLOWED_FILES 에 있는 파일만 건드린다** — 셸이 대조한다.
3. `$WORK/VERIFY.md` 에 기록한다.

## VERIFY.md 필수 섹션

- STATUS 라인 (첫 줄)
- 작성한 테스트 파일과 케이스 목록
- 검증 기준 ↔ 테스트 케이스 대응표 (빠진 기준이 있으면 명시)
- 테스트로 덮지 못한 부분
- **이 테스트가 무엇을 못 잡는지** — 통과해도 남는 위험

## 중요

테스트 통과 여부는 이 세션이 판정하지 않는다. 파이프라인이 별도로 실행한다.
"통과할 것으로 보인다" 같은 문장을 쓰지 마라.
구현이 명백히 틀렸다고 판단되면 테스트를 느슨하게 만들지 말고 STATUS: BLOCKED.

**좋은 테스트는 계측을 지웠을 때 실패한다.** 값이 존재하는지만 보는 테스트는
구현이 사라져도 통과할 수 있다 — 그런 테스트는 쓰지 마라.

## 금지

- **구현 코드를 고치지 않는다.** 테스트가 실패하면 그건 다음 재시도의 입력이다.
- `$WORK/DESIGN.md`·`$WORK/IMPL.md` 를 수정하지 않는다.
