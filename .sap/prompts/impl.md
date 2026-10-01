### 구조와 검사 명령

- python: `services/python`. 테스트는 `cd services/python && .venv/bin/python -m pytest -q` (시스템 python 금지)
- node: `services/node`. 테스트는 `cd services/node && npm test` (vitest), 타입 검사는
  `cd services/node && npx tsc --noEmit` — 셸은 이 셋을 전부 돌려 green 을 판정한다

### 코딩 규칙

- 코드 주석은 주변 코드와 같은 밀도로, 영어로 쓴다. 이 저장소는 **"왜 이렇게 했나"** 를 남기는
  주석을 쓴다 — 함정·경계조건·과거에 겪은 버그. 자명한 것은 안 쓴다
- 파일을 새로 만들기 전에 **기존 모듈에 같은 역할이 있는지 먼저 본다**
