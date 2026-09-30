#!/usr/bin/env bash
# 실행 역할 터미널 (터미널 1)
#
# 역할 분리:
#   이 스크립트  = 오케스트레이터. 진행 결정권을 독점한다.
#   런처 세션    = 실행·전달·y 중계만 한다. 진행 결정권 없음 (launcher-protocol.md).
#   사람         = 유일하게 게이트 버튼을 누르는 주체.
#
# 사용법:
#   ./orchestrate.sh <feature-name>
#   AUTO=1 ./orchestrate.sh <feature-name>            # 사람 게이트 건너뜀 (무인)
#   MAX_RETRY=3 ./orchestrate.sh <feature-name>
#   RESUME_FROM=impl ./orchestrate.sh <feature-name>    # verify(테스트) 재사용, impl 부터
#   PREFLIGHT_CMD="npm run build" ./orchestrate.sh <f>  # 에이전트 전 환경 기준선 검사

set -euo pipefail

# ─────────────────────────────────────────── 설정
ROOT="$(git rev-parse --show-toplevel)"
FEATURE="${1:?사용법: ./orchestrate.sh <feature-name>}"
WORK="$ROOT/.pipeline/$FEATURE"
PROMPTS="$ROOT/prompts"

MAX_RETRY="${MAX_RETRY:-2}"
AUTO="${AUTO:-0}"
# 이미 STATUS: DONE 인 DESIGN.md 가 있으면 설계 단계를 건너뛰고 재사용한다.
# (중단 후 재실행에서 비싼 설계를 다시 만들지 않기 위한 것 — 설계 게이트는 그대로 거친다)
# 설계를 새로 뽑고 싶으면 FRESH_DESIGN=1
FRESH_DESIGN="${FRESH_DESIGN:-0}"

# ── RESUME_FROM=impl ────────────────────────────────
# verify(테스트 선작성)를 건너뛰고 이전 주행의 테스트로 impl 부터 시작한다.
# 계기: 테스트를 다 쓴 뒤 impl 이 계정 한도(api_error)로 죽으면 재실행이 verify 부터
# 다시 돈다 (design·judge 에만 재사용 로직이 있다). 예전 RESUME_FROM=verify 의 자리다 —
# 2026-09-30 red→green 으로 verify 가 impl 앞으로 옮겨 갔다.
#
# **자동 판정을 넣지 않는 이유**: 이전 주행의 impl 이 소스를 반쯤 고쳤을 수 있어 red 를 다시
# 확인할 수 없다. 테스트를 그대로 믿어도 되는지는 사람이 정한다. 그래서 명시할 때만 건너뛴다.
RESUME_FROM="${RESUME_FROM:-}"

# ── 검증 명령 ────────────────────────────────────────
# 프로젝트마다 다르므로 **기본값을 두지 않는다.** 예전에는 npm test 가 기본값이었는데,
# 그 저장소에서 성공 불가인 채로 재시도 루프가 예산만 태웠다 (2026-08-28 실측, ~$39).
# 그렇다고 "기본값 그대로면 경고"로는 못 잡는다 — 진짜 npm 저장소에서는 npm test 가
# 정답이라, 각색을 안 한 것과 각색이 맞은 것이 문자열로 구분되지 않는다.
# 그래서 탐지하지 않고 **선언을 강제한다**: 비어 있으면 에이전트를 띄우기 전에 죽는다($0).
# 테스트 파일이 0개일 때 실패하는 러너여야 한다 — 검증 단계가 테스트를 안 쓰고 넘어간
# 것을 게이트가 통과시키면 안 된다.
# neemba: ci.yml 이 도는 검사 전부 (node 테스트·타입 검사, python 테스트).
# 둘 다 **미설정**일 때만 채운다. TEST_CMDS 가 TEST_CMD 보다 우선하므로 무조건 채우면
# `TEST_CMD=... ./orchestrate.sh` 덮어쓰기가 조용히 무시되고, 빈 값으로 명시하면
# 아래 선언 강제 게이트로 죽어야 한다.
if [ -z "${TEST_CMD+x}" ] && [ -z "${TEST_CMDS+x}" ]; then
  TEST_CMDS="cd services/python && .venv/bin/python -m pytest -q
cd services/node && npm test
cd services/node && npx tsc --noEmit"
fi
TEST_CMD="${TEST_CMD:-}"

# ── 검증 명령이 여럿일 때 ────────────────────────────
# 줄바꿈으로 나열한다. `&&` 로 잇지 마라 — run_verify 가 체인 전체를 명령 하나로 보므로
# 어느 검사가 깨졌는지 FAIL_LOG 에 안 남고, 다음 구현 시도가 그걸 읽는다.
# CI 가 도는 검사 전부를 넣는다. 테스트만 넣고 린트·타입 검사를 빼면 게이트가 초록이어도
# "넣은 명령이 통과했다"는 뜻일 뿐이다.
#   TEST_CMDS="pytest -q
#   npm run lint
#   npm run typecheck"
# 비어 있으면 $TEST_CMD 하나를 쓴다 (하위호환). 앞뒤 공백과 빈 줄은 무시한다.
TEST_CMDS="${TEST_CMDS:-}"

# ── 프리플라이트 (선택) ──────────────────────────────
# 에이전트를 **띄우기 전에** 환경 기준선을 판정하는 명령. 여기서 죽으면 비용이 $0 이다.
# 계기(2026-08 실측): .env 가 없는 체크아웃에서 npm run build 가 원래 안 되는데, 셸이
# 그걸 impl 이 만든 실패로 오인해 impl+verify 사이클을 3회 태웠다. 그리고 다른 주행에서는
# phase:DONE 이 떴는데 타입 검사가 한 번도 안 돌았다 — 기준선이 녹색이어야 "이후 실패는
# 에이전트가 만든 것"이라고 말할 수 있다.
# 비어 있으면 건너뛴다. 예: PREFLIGHT_CMD="npm run build" / ".venv/bin/python -m mypy src"
PREFLIGHT_CMD="${PREFLIGHT_CMD:-}"

# ── 검증 명령 시간 상한 ──────────────────────────────
# 게이트 명령 하나가 영원히 안 돌아오면 파이프라인이 조용히 매달린다. 죽는 것보다
# 나쁘다 — 죽으면 FAIL_LOG 라도 남는데, 매달리면 사람이 알아채기 전까지 아무것도 없다.
# 2026-08-27 실측: impl 이 스텁을 진짜 감시 루프로 바꾸자 이전 테스트 3개가 Ctrl+C 를
# 영원히 기다리게 됐고 pytest 전체가 멈췄다. 초 단위. 0 이면 상한 없음.
VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-300}"

# ── 보호 파일 ────────────────────────────────────────
# 어느 단계도 건드리면 안 되는 파일. 프롬프트에도 적히지만 프롬프트는 게이트가 아니다.
# 의존성 파일이 들어가는 이유: 패키지 추가/제거는 사람 승인 사항이다 (CLAUDE.md 작업 게이트).
# 테스트 러너·린터 설정이 들어가는 이유: 검증 단계가 통과시키려고 설정을 느슨하게 만드는
# 것이 가장 값싼 부정행위 경로다. 존재하지 않는 파일도 목록에 둔다 — 지문이 "(없음)" 으로
# 찍히므로 에이전트가 새로 만들어 게이트를 무르게 하는 경로도 잡힌다.
# 파이프라인 산출물(DESIGN.md·JUDGE.md)은 설계 게이트 통과 후 자동으로 추가된다.
# 저장소 루트 기준 상대 경로, 공백 구분. 프로젝트에 맞게 덮어써라.
PROTECTED_FILES="${PROTECTED_FILES:-package.json package-lock.json pnpm-lock.yaml yarn.lock pyproject.toml requirements.txt requirements-dev.txt setup.cfg pytest.ini mypy.ini ruff.toml .ruff.toml conftest.py tests/conftest.py vitest.config.ts vitest.config.mts jest.config.js jest.config.ts tsconfig.json pubspec.yaml pubspec.lock .gitignore CLAUDE.md AGENTS.md .env .env.example services/node/package.json services/node/package-lock.json services/node/tsconfig.json services/node/.eslintrc.cjs services/python/pyproject.toml services/python/uv.lock services/python/tests/conftest.py infra/nginx/html/monitor/package.json infra/nginx/html/monitor/package-lock.json infra/nginx/html/monitor/tsconfig.json}"

# ── 에이전트가 스스로 돌려도 되는 명령 ────────────────
# `-p` 는 비대화형이라 승인할 사람이 없다. acceptEdits 는 Write/Edit 만 자동 승인하고
# Bash 는 승인을 요구한다. 그래서 에이전트가 테스트·린트를 **한 번도 실행하지 못한 채**
# "통과할 것"이라고 추측만 하고 단계를 끝낸다 (2026-08-26~27 실측: 세 세션 연속 실행 요청
# 전량 거부, impl 이 린트 한 줄을 두 주행 내내 못 고침 — 자기가 뭘 어겼는지 볼 수 없었다).
#
# 읽기 전용 검사만 넣는다. 합격 판정은 여전히 셸이 $TEST_CMD 와 범위 게이트로 직접 한다 —
# 여기서 여는 것은 "판정권"이 아니라 "제출 전에 스스로 확인할 권한"이다.
# 기본값은 git 읽기 4개. 각색 시 이 저장소의 테스트 러너를 **정확한 형태로** 추가한다
# (예: "Bash(.venv/bin/python -m pytest:*)", "Bash(cd frontend && pnpm test:*)").
# 쉼표 구분 한 줄. 넓게 열지 마라 — 범위 게이트가 백스톱이지만 임의 실행은 그 밖이다.
AGENT_TOOLS="${AGENT_TOOLS:-Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(git ls-files:*),Bash(cd services/python && .venv/bin/python -m pytest:*),Bash(cd services/node && npm test:*),Bash(cd services/node && npx tsc --noEmit:*)}"

# ── 정본 문서 주입 (선택) ────────────────────────────
# 설계 프롬프트 끝에 여기 적힌 파일의 본문을 `cat -n` 으로 이어 붙인다. "읽어라"는 부탁이고
# 이건 강제다 — 모델에게 Read 를 부를지 말지의 선택지가 없어진다.
# 계기(2026-09-05 실측): Opus 설계는 파일을 4~9개 읽었고(Fable 5.1 은 18~21개) judge 반박이
# 주행당 2.3건(Fable 0.5건)이었다. 비용은 문서 크기만큼의 캐시 쓰기 한 번 — 92KB PRD 면
# Opus 에서 약 $0.3, 이후 턴의 캐시 읽기까지 합쳐 최대 $0.6. 줄번호를 붙이는 이유는 근거
# 등급의 `파일:줄` 좌표를 그대로 쓰게 하기 위해서다.
# 설계 단계에만 붙는다. 저장소 루트 기준 상대 경로, 공백 구분. 없는 파일이면 시작 전에 죽는다.
# 이 파일들은 PROTECTED_FILES 에도 넣어라 — 기준선을 에이전트가 고칠 수 있으면 대조가 무의미하다.
REQUIRED_DOCS="${REQUIRED_DOCS:-}"

# ── 추가 읽기 디렉터리 (선택) ────────────────────────
# 에이전트는 작업 디렉터리 밖을 읽지 못한다. 프레임워크 SDK 소스처럼 계약을 확인해야 하는
# 읽기 전용 참조물이 밖에 있으면 여기 적는다 (2026-08-25 Flutter 설계가 SDK 를 못 읽어 BLOCKED).
# 확인을 막으면 에이전트가 추측으로 메꾸고, 그 추측을 judge 가 잡는 왕복이 더 비싸다.
# 공백 구분 절대 경로. 예: ADD_DIRS="$HOME/flutter"
ADD_DIRS="${ADD_DIRS:-}"

# ── 모델 티어링 ──────────────────────────────────────
# 별칭 대신 풀 ID를 박는다. 별칭은 어느 날 조용히 다른 모델을 가리킨다.
#
#   설계  : 상위(Opus 5.5).
#   판단검증: codex(gpt-6.1-sol → gpt-6-astra). 설계와 **다른 회사 모델**이 감사한다
#           (design-notes §6). claude 로 돌릴 때의 기준 모델은 Opus 5.5 — 아래 "엔진 접두사".
#   구현  : 중간(Sonnet 5.5). 설계가 확정돼 있으면 난이도가 내려간다.
#   검증  : 상위(Opus 5.5). **Opus 검증 품질은 미측정**(과거 8건 전부 Fable).
#
# 2026-09-30 세대 교체: opus-5 → opus-5-5, sonnet-5 → sonnet-5-5 (사용자 결정). 비용 실적은
# 5 세대 기준(2026-09-05 재가격, 전체 주행 $13.1 → $9.9)이라 5.5 세대로 다시 재야 한다.
# effort 는 이 변경과 분리해서 잰다 — 두 변수를 같이 바꾸면 차이의 출처를 못 가른다.
#
# FALLBACK_* 은 가용성 폴백(529 과부하 등) **그리고** 레이트리밋 순환 체인이다.
# --fallback-model 은 과부하·부재만 받고 창 소진 거부는 셸이 감지해 다음 항목으로
# 갈아탄다 (rate_limited 참조). 안전 분류기에 의한 모델 교체는 둘 다로 막을 수 없다 —
# MODEL_LOG.md 로 감시한다.
MODEL_DESIGN="${MODEL_DESIGN:-claude-opus-5-5}"
MODEL_JUDGE="${MODEL_JUDGE:-codex:gpt-6.1-sol}"
MODEL_IMPL="${MODEL_IMPL:-claude-sonnet-5-5}"
MODEL_VERIFY="${MODEL_VERIFY:-claude-opus-5-5}"

# 폴백은 먼저 **위**로 간다. 과부하 때 하위 티어로 떨어뜨리면 산출물 품질이 조용히 무너진다.
# 체인 끝의 sonnet 은 두 풀이 다 소진됐을 때 죽는 대신 돌리는 최후 수단이다 — FAIL_LOG 집계
# (2026-09-05) 에서 Fable 리밋 5건·Opus 리밋 5건, 양쪽 풀이 다 막힌다. 주 모델이 Opus 인
# 단계의 첫 폴백이 Fable 인 이유: 리밋으로 갈아탄 주행은 Fable 값을 내지만, 버려진 부분
# 주행보다 싸다.
FALLBACK_DESIGN="${FALLBACK_DESIGN:-claude-fable-5-1,claude-sonnet-5-5}"
FALLBACK_JUDGE="${FALLBACK_JUDGE:-codex:gpt-6-astra}"
FALLBACK_IMPL="${FALLBACK_IMPL:-claude-opus-5-5}"
FALLBACK_VERIFY="${FALLBACK_VERIFY:-claude-fable-5-1,claude-sonnet-5-5}"

# ── 엔진 접두사 (judge 전용 codex 엔진) ─────────────────
# 체인 항목은 `엔진:모델` 이다. 접두사가 없으면 claude 로 본다 (기존 설정 그대로 동작).
#   codex:<모델>  — `codex exec` 로 돈다. **judge 에만** 쓸 수 있다: read-only 샌드박스라
#                   파일을 못 쓰고, 셸이 최종 응답(-o)을 산출물로 저장한다. 감사 단계에
#                   "고치지 않는다"를 프롬프트가 아니라 샌드박스로 강제하는 구조다.
#
# judge 기본값은 codex(GPT) 다 (2026-09-30 사용자 결정 — A 방식). 설계와 다른 회사 모델이
# 감사해야 맹점을 덜 공유한다 (design-notes §6 의 교차 감사 근거를 모델 계열 단위로 넓힌 것).
# 체인에 claude 항목이 없으므로 codex 가 전부 막히면 **자동으로 claude 로 넘어가지 않고**
# exit 5 로 멈춰 사람에게 전환 여부를 묻는다. 자동 전환을 원하면 체인 끝에 claude 를 붙인다:
#   FALLBACK_JUDGE="codex:gpt-6-astra,claude-opus-5-5"
# codex 없이 돌리려면:
#   MODEL_JUDGE=claude-opus-5-5 FALLBACK_JUDGE=claude-fable-5-1,claude-sonnet-5-5
#
# 다른 PC 호환: 사용자 codex 설정(~/.codex/config.toml)을 읽지 않는다(--ignore-user-config).
# PC 마다 기본 effort·플러그인·notify 가 달라 judge 동작이 갈라지기 때문이다. 필요한 값은
# 전부 명령줄로 준다. Windows 는 샌드박스 설정도 명령줄로 줘야 한다 — 사용자 설정의
# `[windows] sandbox = "elevated"` 가 빠지면 읽기 명령까지 전부 거부된다 (2026-09-30 실측).
CODEX_BIN="${CODEX_BIN:-codex}"
CODEX_EFFORT="${CODEX_EFFORT:-high}"      # 사용자 설정 기본값(low)을 따르지 않는다
CODEX_TIMEOUT="${CODEX_TIMEOUT:-2400}"    # 초. codex exec 에는 턴 상한이 없어 시간으로 막는다

# ── 단계별 상한 ──────────────────────────────────────
# 턴 상한은 무한루프 탈출용이다. 실적보다 넉넉히 둔다 — 2026-08-31 실측: 40턴 시절
# judge·verify 가 한 주행에서 41턴에 나란히 죽었고, verify 는 테스트를 다 쓰고 마지막
# 확인에서 죽어 게이트 3종이 녹색인데 VERIFY.md 만 없는 상태를 남겼다 ($10.24 버려짐).
# 티어를 내리면 시행착오로 메꿔 턴을 더 먹는다 — 모델만 내리고 상한을 두면 더 확실히 죽는다.
TURNS_DESIGN="${TURNS_DESIGN:-60}"    # 실적 11~37
TURNS_JUDGE="${TURNS_JUDGE:-80}"      # 실적 22~70, 41 에서 사망
TURNS_IMPL="${TURNS_IMPL:-80}"        # 실적 17~60 (41 에서 사망한 적 있음)
TURNS_VERIFY="${TURNS_VERIFY:-80}"    # 실적 18~37, 41 에서 사망

# 예산 상한은 **기본값 없음** (2026-08-31 결정, 2026-09-04 원본 반영).
# 근거는 실적이다. 4 feature / 40 주행을 전수 조사한 결과:
#   - 폭주(무한루프·무한지출) 사례 0건. 어떤 주행도 70턴을 안 넘겼다
#   - 상한이 발동한 4번은 전부 정상 작업을 죽였다 (예산 2건 $10.02 + 턴 2건 $10.24)
# 상한은 돈을 아끼지 않는다. 이미 쓴 돈을 살릴지 버릴지만 정한다 — 쓸모 있는 작업과
# 없는 작업을 구별하지 못하기 때문이다. 실적 최댓값의 1.1~1.4배에 두면 차단기가 아니라
# 목줄이 된다. 폭주가 걱정되면 값을 주면 그대로 동작한다: BUDGET_IMPL=20 ./orchestrate.sh <f>
BUDGET_DESIGN="${BUDGET_DESIGN:-}"
BUDGET_JUDGE="${BUDGET_JUDGE:-}"
BUDGET_IMPL="${BUDGET_IMPL:-}"
BUDGET_VERIFY="${BUDGET_VERIFY:-}"

# ── worktree 격리 강제 ───────────────────────────────
# 각 단계는 --permission-mode acceptEdits 로 돈다. 메인 체크아웃에서 돌리면
# 사람이 작업 중인 파일을 에이전트가 그대로 덮어쓴다. 규칙으로 부탁하지 않고 막는다.
#
# 판별: worktree 안에서는 git-dir 이 <main>/.git/worktrees/<name>,
#       git-common-dir 은 <main>/.git 을 가리킨다. 메인 체크아웃에서는 둘이 같다.
#
# die() 를 안 쓴다 — 아직 아무 단계도 안 돌았는데 STATE.md 에 DIED 를 남기면
# 런처가 "돌다가 죽었다"로 읽는다. 시작 자체를 거부한 것과는 다른 사건이다.
REQUIRE_WORKTREE="${REQUIRE_WORKTREE:-1}"
if [ "$REQUIRE_WORKTREE" = "1" ] \
   && [ "$(git -C "$ROOT" rev-parse --git-dir)" = "$(git -C "$ROOT" rev-parse --git-common-dir)" ]; then
  {
    printf '\033[1;31m[FAIL]\033[0m 메인 체크아웃에서는 돌리지 않는다\n'
    printf '  acceptEdits 로 도는 에이전트가 작업 중인 파일을 덮어쓴다.\n\n'
    printf '  worktree 만들기:      ./pipeline-worktree.sh %s\n' "$FEATURE"
    printf '  정말 여기서 돌리려면: REQUIRE_WORKTREE=0 ./orchestrate.sh %s\n' "$FEATURE"
  } >&2
  exit 2
fi

mkdir -p "$WORK"
FAIL_LOG="$WORK/FAIL_LOG.md"     # append-only
STATE="$WORK/STATE.md"           # 런처가 읽는 유일한 실시간 창구
MODEL_LOG="$WORK/MODEL_LOG.md"   # 요청 모델 vs 실제 실행 모델
touch "$FAIL_LOG" "$MODEL_LOG"

# TEST_CMD 도 내보낸다. prompts/verify.md 가 본문에서 참조하는데, export 하지 않으면
# envsubst 가 빈 문자열로 치환해서 "셸이 `` 를 실행해서 판정한다" 라는 깨진 문장이
# 에이전트에게 전달된다 (run-tests 의 "프롬프트 치환" 케이스가 이걸 잡는다).
export FEATURE WORK ROOT TEST_CMD

# STATE.md 의 검증 게이트 블록이 첫 호출부터 참조한다 (set -u).
PREFLIGHT_STATE="건너뜀 (PREFLIGHT_CMD 비어 있음)"
VERIFY_LAST=""
BASELINE_STATE=""
RED_STATE=""
VERIFY_PASSED=""
VERIFY_FAILED=""
ARTIFACT_GUARD=""   # 설계 게이트 통과 후 DESIGN.md·JUDGE.md 가 들어온다

log() { printf '\033[1;36m[orch]\033[0m %s\n' "$*" >&2; }
die() {
  # 죽으면 봉인을 푼다 — 여기서부터는 사람이 worktree 를 만지는 것이 정상이다 (되돌리기 등).
  # 봉인 대조 자체로 죽을 때만 남긴다: 풀어 버리면 재실행이 같은 수정을 조용히 통과시킨다.
  [ "${KEEP_SEAL:-0}" = 1 ] || rm -f "$WORK/.seal" "$WORK/.seal.at"
  state "DIED" "$*" "실패했다. $FAIL_LOG 와 위 note 를 읽고 원인을 사람에게 보고해라. 재실행 여부는 사람이 정한다 — 런처가 임의로 재실행하지 마라."
  printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 2
}

# 실패를 append-only 로 남기는 유일한 창구.
# prompts/impl.md 는 FAIL_LOG 를 "이전 시도가 왜 실패했나"의 유일한 입력으로 쓰는데,
# 예전엔 검증 실패 경로 한 곳에서만 여기에 썼다. 단계가 죽는 경로에는 아무것도 안 남아
# 사람이 매번 *.stream.jsonl 을 jq 로 파야 했다 (2026-08-24 하루에 세 번).
fail_log() {   # fail_log <제목> ; 본문은 stdin
  { echo "## $1 — $(date -Iseconds)"; cat; echo; } >> "$FAIL_LOG"
}

# 파일 내용 해시 — 승인 마커가 "무엇을 승인했는가"를 내용 단위로 기억하는 키이자
# 보호 파일 지문·산출물 신선도 판정의 공용 함수다.
# approve.sh 의 file_hash 와 결과가 같아야 한다 (run-tests 가 교차 검증한다).
file_hash() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1"
  else sha256sum "$1"; fi | awk '{print $1}'
}

# ─────────────────────────────────────────── 런처용 상태 브로드캐스트
# 셸은 대화를 못 한다. 대신 상태를 파일로 흘려서 런처 세션이 읽게 한다.
# 3번째 인자가 "## 다음 행동" 블록이 된다 — 런처 계약은 문서(SKILL.md)가 아니라
# 런처가 실제로 읽는 이 파일에 박는다. 문서에만 적힌 계약은 안 지켜졌다(2026-08-24:
# 런처 세션이 스크립트 stderr 의 터미널 안내를 그대로 전달하고, 정지 후 갈 길을 잃었다).
state() {
  local phase=$1 note=${2:-} next=${3:-}
  cat > "$STATE" <<EOF
# 파이프라인 상태 (셸이 자동 생성 — 사람이 편집하지 말 것)

- feature: $FEATURE
- phase: $phase
- attempt: ${ATTEMPT:-0} / $((MAX_RETRY + 1))
- pid: $$
- updated: $(date -Iseconds)
- note: $note

## 다음 행동 (런처 세션은 이 블록만 따르면 된다)
${next:-진행 중 — 개입 불필요. 이 파일을 다시 읽으면 최신 상태가 보인다.}

## 검증 게이트

셸이 실제로 무엇을 돌렸는지. "DONE" 이 무엇을 뜻하는지는 여기를 봐야 안다.

- 프리플라이트: $PREFLIGHT_STATE
- 검증 명령: $(verify_commands | paste -sd, - | sed 's/,/, /g')$([ -f "$WORK/smoke.sh" ] && printf ', bash %s' "$WORK/smoke.sh")
- 명령별 시간 상한: ${VERIFY_TIMEOUT}초
- 기준선(테스트 작성 전): ${BASELINE_STATE:-(아직 실행 안 함)}
- red(구현 전 새 테스트 실패): ${RED_STATE:-(아직 실행 안 함)}
- 마지막 결과(green): ${VERIFY_LAST:-(아직 실행 안 함)}

## 지금까지 생성된 산출물
$(ls -1 "$WORK"/*.md 2>/dev/null | sed 's|.*/|- |' || echo "- (없음)")

## 마지막 테스트 출력 (tail 20)
\`\`\`
$(tail -20 "$WORK/test_out.txt" 2>/dev/null || echo "(아직 없음)")
\`\`\`
EOF
}

# ─────────────────────────────────────────── BLOCKED 종료
# 정상 종료든 크래시든 산출물이 BLOCKED 면 사람은 **막힌 이유**를 받아야 한다.
# 크래시 경로가 이걸 안 부르면 "그냥 죽었다"만 보이고 BLOCKED_NEEDS 가 묻힌다
# (예산 상한 직전에 BLOCKED 를 쓰고 죽는 것은 흔한 조합이다).
emit_blocked() {   # emit_blocked <이름> <산출물> [덧붙일 사인]
  local name=$1 artifact=$2 extra=${3:-}
  state "BLOCKED:$name" "사람 판단 필요${extra:+ ($extra)}" \
    "$artifact 의 BLOCKED_REASON·BLOCKED_NEEDS 를 사람에게 보고하고 결정을 받아라. 결정 전에는 재실행하지 마라 — 같은 곳에서 또 막힌다."
  log "  ⛔ $name BLOCKED${extra:+ — $extra}"
  sed -n '/^BLOCKED_REASON:/,$p' "$artifact" >&2
  rm -f "$WORK/.seal" "$WORK/.seal.at"   # 사람 결정이 필요하다 — die 와 같은 이유로 봉인을 푼다
  exit 3
}

# ─────────────────────────────────────────── 모델 교체 감시
# 안전 분류기가 걸리면 --model 로 지정한 모델이 아닌 다른 모델이 돈다.
# --fallback-model 로 막을 수 없으므로, 막는 대신 기록해서 눈에 띄게 한다.
# ※ 필드명은 버전마다 다르다. CLI 2.1.226 실측: .modelUsage(모델명이 키)는 있고
#   .model 은 없다. 첫 실행 후 `jq 'keys' result.json` 으로 확인할 것.
#
# allow_gate=0 이면 기록만 한다 — 크래시 경로가 그렇다. 바로 뒤 부검 게이트가
# 같은 판단("이 산출물을 신뢰할까")을 묻는데 게이트를 두 번 띄울 이유가 없다.
check_model_swap() {   # check_model_swap <이름> <result.json> <요청모델> <allow_gate>
  local name=$1 out=$2 model=$3 allow_gate=$4 actual
  actual=$(jq -r '(.modelUsage // {} | keys | join(",")) // empty' "$out" 2>/dev/null || true)
  [ -z "$actual" ] && actual=$(jq -r '.model // empty' "$out" 2>/dev/null || true)

  if [ -n "$actual" ] && [[ "$actual" != *"$model"* ]]; then
    log "  ⚠ 모델 교체 감지: 요청=$model 실제=$actual"
    echo "- $(date -Iseconds) | $name | 요청 $model → 실제 $actual" >> "$MODEL_LOG"
    if [ "$allow_gate" = "1" ] && [ "$AUTO" != "1" ]; then
      # 게이트는 MODEL_LOG 가 아니라 단계별 스냅숏에 건다 — MODEL_LOG 는 모든 단계가
      # append 하는 저널이라 승인 마커(내용 해시)가 다음 단계에서 반드시 낡는다.
      # 스냅숏은 타임스탬프 없이 결정적이라 같은 교체가 반복돼도 마커가 살아 있다.
      local swap_note="$WORK/$name.model-swap"
      printf '%s | 요청 %s → 실제 %s\n' "$name" "$model" "$actual" > "$swap_note"
      gate_human "요청한 모델이 안 돌았다. 결과를 신뢰할지 판단해라" "$swap_note"
    fi
  elif [ -z "$actual" ]; then
    echo "- $(date -Iseconds) | $name | 실제 모델 확인 불가 (필드명 점검 필요)" >> "$MODEL_LOG"
  fi
}

# ─────────────────────────────────────────── 죽은 단계 부검
# claude 가 0 이 아닌 코드로 죽어도 스트림 마지막 result 이벤트에는 이유가 들어 있다
# (2026-08-24 실측: subtype=error_max_budget_usd / $5.08 / 34턴). 예전엔 exit code
# 숫자 하나만 보고 die 해서 그 파일을 손에 쥐고도 버렸다.
#
# 반환 0 = 사람이 산출물을 신뢰하기로 했다(호출자는 그대로 진행).
# 그 밖의 경로는 이 함수 안에서 끝난다 — die(2), BLOCKED(3), 승인 대기(4).
stage_postmortem() {
  local name=$1 out=$2 stream=$3 code=$4 artifact=$5 art_before=$6
  local reason

  if [ "$(jq -r 'type' "$out" 2>/dev/null)" = "object" ]; then
    local subtype errors turns cost term
    subtype="$(jq -r '.subtype // "?"' "$out")"
    errors="$(jq -r '(.errors // []) | join("; ")' "$out")"
    turns="$(jq -r '.num_turns // "?"' "$out")"
    cost="$(jq -r '.total_cost_usd // "?"' "$out")"
    term="$(jq -r '.terminal_reason // "?"' "$out")"
    reason="$subtype${errors:+ — $errors} (턴 $turns, \$$cost, terminal_reason=$term)"
  else
    # 침묵하지 않는다. "확인 불가"도 정보다 — 스트림이 중간에 끊겼다는 뜻이고,
    # 그건 예산·턴 초과와 다른 사건이다 (프로세스 강제 종료·디스크·파이프 파손).
    reason="사인 확인 불가 — 스트림에 result 이벤트가 없다 (프로세스가 중간에 끊김)"
  fi

  # 산출물이 **이번 주행 것인지**를 실행 전 지문과 비교해 판정한다.
  # 파일 존재만 보면 이전 주행이 남긴 것을 이번 것으로 오인한다 — 실측: 1차 정상 주행
  # 뒤 FRESH_DESIGN=1 로 설계를 버리라고 명시하고 2차가 산출물 없이 죽었는데,
  # 셸이 **1차의 DESIGN.md** 를 "온전해 보인다"며 되살리라고 사람에게 내밀었다.
  local fresh=0 artifact_state="없음"
  if [ -f "$artifact" ]; then
    if [ "$(file_hash "$artifact")" != "$art_before" ]; then
      fresh=1; artifact_state="이번 주행이 씀"
    else
      artifact_state="있으나 이전 주행 것 (이번 주행은 건드리지 않음)"
    fi
  fi

  log "  ✖ $name: 프로세스 사망 (exit $code) — $reason"
  fail_log "$name 단계 프로세스 사망 (exit $code)" <<EOF
사인: $reason
스트림: $stream
산출물: $artifact ($artifact_state)
EOF

  local verdict=""
  if [ "$fresh" = "1" ]; then
    verdict="$(grep -m1 '^STATUS:' "$artifact" | awk '{print $2}' || true)"
  fi

  # 이번 주행이 BLOCKED 를 쓰고 죽었으면 "그냥 죽었다"가 아니라 막힌 이유를 넘긴다.
  if [ "$verdict" = "BLOCKED" ]; then
    emit_blocked "$name" "$artifact" "$reason"
  fi

  if [ "$verdict" != "DONE" ]; then
    die "$name: 프로세스 사망 (exit $code) — $reason → $stream"
  fi

  # 여기부터: 프로세스는 죽었는데 산출물은 온전해 보인다 (2026-08-24 judge 가 그랬다 —
  # JUDGE.md 는 완성본이었는데 셸이 버려서 $4.72 를 다시 냈다).
  #
  # 그래도 자동 통과는 안 된다. 에이전트가 파일을 쓴 **뒤** 더 검증하려다 죽었다면
  # 내용이 의도보다 덜 검증된 상태다. 그래서 gate_human 의 force=1 — AUTO=1 에서도 사람이 본다.
  #
  # 게이트를 띄우기 **전에** 파킹한다. 제자리에 둔 채 게이트만 띄우면, 사람이 n 을
  # 누르든 tty 없이 exit 4 로 멈추든, 다음 실행의 재사용 로직이 이 미승인 산출물을
  # **게이트 없이** 되살린다 (2026-08-24 document-detail 의 JUDGE.md 가 그 상태였다).
  # 파킹본도 번호를 매긴다 — 스트림은 attempt 번호로 보존하면서 정작 가장 비싼
  # 산출물만 덮어쓰는 것은 앞뒤가 안 맞는다.
  local parked="$artifact.crashed"
  if [ -e "$parked" ]; then
    local m=2
    while [ -e "$artifact.crashed$m" ]; do m=$((m + 1)); done
    parked="$artifact.crashed$m"
  fi
  mv "$artifact" "$parked"
  log "  ⚠ 프로세스는 죽었으나 산출물은 STATUS: DONE — $parked 로 파킹"

  # 파킹본의 승인 명령은 마커가 아니라 mv 다. 마커로 되살리면 "승인했다"와 "제자리에
  # 있다"가 분리돼 다음 실행의 재사용 로직이 마커 없는 파일을 집는 경로가 생긴다.
  # design·judge 는 재사용 로직이 집고, impl·verify 는 단계가 다시 돈다.
  gate_human \
    "죽은 이유: $reason. 산출물이 온전해 보이는데 신뢰할까? (y = 제자리로 되돌리고 진행)" \
    "$parked" 1 \
    "mv '$parked' '$artifact'"

  # 여기 도달 = 사람이 y 를 눌렀거나 유효한 승인 마커가 있었다.
  mv "$parked" "$artifact"
  log "  ✔ 사람이 산출물을 신뢰하기로 했다 — 제자리로 되돌리고 진행"
  return 0
}

# ─────────────────────────────────────────── 레이트 리밋 판정
# rate_limited <stream>
#
# --fallback-model 은 "과부하·부재"만 받는다 (CLI --help 원문: "when the default
# model is overloaded or not available"). 주간·5시간 창이 소진돼 거부되면 그 플래그는
# 아무것도 하지 않고 단계가 그냥 죽는다 — 2026-08-26 실측: FALLBACK_VERIFY 에
# opus-5·sonnet-5 가 있었는데도 fable-5 에서 죽었고 폴백을 한 번도 시도하지 않았다.
# 그래서 리밋 거부는 셸이 직접 감지한다. 신호 두 개 중 하나면 참으로 본다:
#   ① rate_limit_event.rate_limit_info.status == "rejected"
#   ② assistant 메시지의 error == "rate_limit" (합성 안내 메시지)
rate_limited() {
  local stream=$1
  jq -Rn '[inputs | fromjson?
           | select((.type? == "rate_limit_event"
                     and (.rate_limit_info?.status? == "rejected"))
                 or (.error? == "rate_limit"))] | length' "$stream" 2>/dev/null \
    | grep -qvx '0'
}

# ─────────────────────────────────────────── 사람 조치 대기 (exit 5)
# 설치·로그인·엔진 전환처럼 **사람이 해야 풀리는** 정지. die(2) 와 가르는 이유: 2 는 "무엇이
# 어긋났는가"를 FAIL_LOG 에서 찾으라는 신호인데, 이건 어긋난 게 아니라 할 일이 정해져 있다.
# 할 일은 STATE.md 의 "다음 행동" 블록에 적는다 — 런처가 읽는 유일한 창구다.
need_human() {   # need_human <phase> <note> <다음 행동>
  state "$1" "$2" "$3"
  {
    printf '\033[1;33m[사람 조치 필요]\033[0m %s (exit 5)\n' "$2"
    printf '  자세한 안내는 %s 의 "다음 행동" 블록에 있다\n' "$STATE"
  } >&2
  exit 5
}

# ─────────────────────────────────────────── 엔진 접두사
chain_engine() {   # chain_engine <항목> → claude | codex
  case "$1" in codex:*) echo codex ;; *) echo claude ;; esac
}
chain_model() {    # chain_model <항목> → 접두사를 뗀 모델 ID
  local m=${1#codex:}; echo "${m#claude:}"
}
# claude 의 --fallback-model 에는 claude 항목만 넘긴다 (codex 모델 ID 를 넘기면 거부된다).
claude_only() {    # claude_only <체인> → 쉼표 구분 claude 모델 ID
  printf '%s' "$1" | tr ',' '\n' | grep -v '^codex:' | sed 's/^claude://' | paste -sd, - || true
}

CODEX_SWITCH_HINT="MODEL_JUDGE=claude-opus-5-5 FALLBACK_JUDGE=claude-fable-5-1,claude-sonnet-5-5"

# 시작 전 점검 — 에이전트를 띄우기 전이라 여기서 멈추면 비용이 $0 이다.
# 설계까지 다 돌린 뒤 judge 에서 "codex 없음"으로 죽으면 설계 비용이 버려진다.
check_engines() {
  local stage chain_var fb_var entry
  for stage in DESIGN IMPL VERIFY; do
    chain_var="MODEL_$stage"; fb_var="FALLBACK_$stage"
    for entry in $(printf '%s,%s' "${!chain_var}" "${!fb_var}" | tr ',' ' '); do
      [ "$(chain_engine "$entry")" = codex ] \
        && die "codex 엔진은 judge 전용이다 ($chain_var/$fb_var 에 $entry) — read-only 샌드박스라 산출물·코드를 쓸 수 없다"
    done
  done
  case ",$MODEL_JUDGE,$FALLBACK_JUDGE," in *,codex:*) ;; *) return 0 ;; esac

  if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
    need_human "SETUP_NEEDED" "judge 체인에 codex 가 있는데 codex CLI 가 없다 ($CODEX_BIN)" \
      "1) 사람에게 \"codex CLI 를 설치할까요? (y/n)\" 를 물어라 — 패키지 설치는 사람이 정한다. 2) 정확히 y 면 \`npm install -g @openai/codex\` 를 실행한다 (npm 이 없으면 Node.js 설치가 먼저라고 보고하고 멈춘다). 3) 설치 후 로그인은 사람이 직접 한다: 사람에게 프롬프트에 \`! codex login\` 을 입력하라고 안내한다 (브라우저 인증). 4) 로그인까지 끝나면 같은 명령으로 재실행한다. codex 없이 진행하려면 사람이 원할 때만: $CODEX_SWITCH_HINT 를 앞에 붙여 재실행."
  fi
  if ! "$CODEX_BIN" login status >/dev/null 2>&1; then
    need_human "LOGIN_NEEDED" "codex CLI 가 로그인돼 있지 않다" \
      "1) 사람에게 프롬프트에 \`! codex login\` 을 입력하라고 안내한다 — 브라우저 인증이라 런처가 대신할 수 없다. 2) 사람이 로그인했다고 하면 \`$CODEX_BIN login status\` 로 확인한 뒤 같은 명령으로 재실행한다. codex 없이 진행하려면 사람이 원할 때만: $CODEX_SWITCH_HINT 를 앞에 붙여 재실행."
  fi
  log "  ✔ codex 엔진 준비됨 ($("$CODEX_BIN" --version 2>/dev/null | head -1))"
}

# codex_unavailable <stream> <stderr> — 리밋·사용 불가로 죽었는가.
# codex 는 claude 의 rate_limit_event 같은 구조화 신호가 없어 오류 메시지 문구로 판정한다.
# stderr 에는 정상 주행에도 ERROR 줄(모델 목록 갱신 시간 초과 등)이 섞이므로, 호출자는
# 단계가 실패했을 때만 이 함수를 부른다. grep -q 를 쓰지 않는다 — 일치하는 순간 grep 이 먼저 끝나
# 앞의 cat 이 SIGPIPE 로 죽고, pipefail 이 그걸 "불일치"로 뒤집는다.
codex_unavailable() {
  { jq -Rr 'fromjson? // empty | select(.type? == "error" or .type? == "turn.failed")
            | (.message // .error.message // "")' "$1" 2>/dev/null
    cat "$2" 2>/dev/null; } \
    | grep -iE 'usage limit|rate.?limit|too many requests|429|quota|model.{0,80}(not found|does not exist|not supported|not available|unavailable)|unsupported model' >/dev/null
}

# codex_exec <이름> <모델> <산출물> <stream> <out> — 종료 코드를 돌려준다.
# claude 경로와 같은 모양의 result.json 을 $out 에 만든다 — 아래 부검·로그가 그걸 읽는다.
codex_exec() {
  local name=$1 model=$2 artifact=$3 stream=$4 out=$5
  local last="$WORK/$name.codex-last.md" err="$WORK/$name.codex.stderr"
  local cprompt="$WORK/$name.codex-prompt.md" rc=0
  local -a win=()
  local win_note=""
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      win=(-c 'windows.sandbox="elevated"')
      # codex 는 Windows 에서 명령을 Windows PowerShell 5.1 로 돌린다. Get-Content 는 BOM 없는
      # UTF-8 을 코드페이지로 읽고, -Encoding UTF8 을 줘도 출력이 콘솔 코드페이지로 다시
      # 인코딩돼 한글이 깨진다. 외부 프로그램 출력은 변환 없이 넘어간다 (2026-09-30 실측:
      # Get-Content 는 깨짐, `cmd /c type` 은 한 번에 정상). 제한 언어 모드라 인코딩 설정용
      # 메서드 호출도 거부된다 — 그래서 명령 선택을 지시한다.
      win_note="- **Windows 읽기·검색 규칙:** 파일 읽기는 \`cmd /c type <경로>\`, 검색은 \`git grep -n <패턴>\` 을 쓴다. PowerShell 의 \`Get-Content\`·\`Select-String\` 은 쓰지 않는다 — 한글이 깨진다. 인코딩을 바꾸려는 메서드 호출은 제한 언어 모드에서 거부된다." ;;
  esac

  # codex 에는 --append-system-prompt 가 없다. 종료 계약·거부 계약을 프롬프트 앞에 붙이고,
  # "파일을 쓴다"는 지시를 "최종 응답이 곧 파일"로 바꾸는 블록을 뒤에 붙인다.
  {
    cat "$PROMPTS/_contract.md"
    [ -f "$PROMPTS/_denial.md" ] && { echo; cat "$PROMPTS/_denial.md"; }
    echo; cat "$WORK/$name.prompt.md"
    cat <<EOF

## 실행 환경 (codex 엔진 — 셸이 붙였다. 위 지시와 충돌하면 이 블록이 우선한다)
이 세션은 **읽기 전용 샌드박스**에서 돈다. 파일을 쓰거나 고칠 수 없다 — 시도하면 거부된다.
- 위 지시의 "산출물 파일을 쓴다"·"먼저 한 번 써라(중간 저장)"는 이 환경에서 **최종 응답**으로 대체한다.
- 최종 응답 = \`$artifact\` 의 본문 전체다. 셸이 최종 응답을 그대로 그 파일에 저장한다.
- 최종 응답의 첫 줄은 \`STATUS: DONE\` 또는 \`STATUS: BLOCKED\` 이고, 판단 검증이면 바로 다음 줄에 \`UNVERIFIED: <n> REFUTED: <n>\` 을 둔다.
- 코드 블록(\`\`\`)으로 감싸지 말고 본문만 출력한다.
- 읽기 명령(파일 열람·검색)은 쓸 수 있다. 쓰기가 필요한 확인(테스트 실행 등)이 거부되면 그 주장은 \`미확인\` 으로 센다.
$win_note
EOF
    # 감사 대상 본문은 셸이 주입한다. 프롬프트는 stdin UTF-8 이라 OS·셸 인코딩과 무관하게 온전히
    # 전달된다 — 실측(2026-09-30)에서 Windows judge 가 DESIGN.md 의 한글을 깨진 채 읽고 BLOCKED 됐다.
    if [ "$name" = judge ] && [ -f "$WORK/DESIGN.md" ]; then
      printf '\n## 감사 대상 본문 (셸이 주입했다 — %s 전체. 줄번호는 `파일:줄` 좌표로 쓴다)\n\n' "$WORK/DESIGN.md"
      cat -n "$WORK/DESIGN.md"
    fi
  } > "$cprompt"

  rm -f "$last"
  local cmd
  cmd="$(printf '%q ' "$CODEX_BIN" exec --ignore-user-config --ignore-rules --ephemeral \
          -C "$ROOT" -m "$model" -s read-only -c "model_reasoning_effort=\"$CODEX_EFFORT\"" \
          ${win[@]+"${win[@]}"} --json -o "$last" -)"
  cmd="$cmd < $(printf '%q' "$cprompt") 2> $(printf '%q' "$err")"

  run_with_timeout "$CODEX_TIMEOUT" "$cmd" \
    | tee "$stream" \
    | jq --unbuffered -Rr 'fromjson? // empty | select(.type? == "item.completed") | .item |
        if .type == "command_execution" then
          "  ⚙ \((.command // "") | tostring | .[0:90]) (exit \(.exit_code // "?"))"
        elif .type == "agent_message" and ((.text // "") | length) > 0 then
          "  💬 \(.text | gsub("\\s+"; " ") | .[0:160])"
        else empty end' >&2
  rc=${PIPESTATUS[0]}

  local failed turns errors
  failed=$(jq -Rn '[inputs | fromjson? | select(.type? == "turn.failed" or .type? == "error")] | length' "$stream" 2>/dev/null || echo 0)
  turns=$(jq -Rn '[inputs | fromjson? | select(.type? == "item.completed" and .item.type? == "command_execution")] | length' "$stream" 2>/dev/null || echo 0)
  errors=$(jq -Rrn '[inputs | fromjson? | select(.type? == "turn.failed" or .type? == "error") | (.message // .error.message // "?")] | join("; ")' "$stream" 2>/dev/null || echo "")
  [ "$rc" -eq 124 ] && errors="${CODEX_TIMEOUT}초 시간 상한 초과${errors:+; $errors}"
  # 턴 실패를 알렸는데 exit 0 이면 실패로 고친다 — 산출물 판정 전에 사인이 남아야 한다.
  [ "$rc" -eq 0 ] && [ "${failed:-0}" -gt 0 ] && rc=1

  if [ "$rc" -eq 0 ]; then
    jq -n --arg m "$model" --argjson t "${turns:-0}" \
      '{type:"result", subtype:"success", is_error:false, result:"codex exec 완료",
        num_turns:$t, total_cost_usd:"n/a(codex)", engine:"codex", requested_model:$m}' > "$out"
    # 최종 응답 → 산출물. 모델이 지시를 어기고 코드 블록으로 감쌌으면 바깥 울타리만 벗긴다.
    if [ -s "$last" ]; then
      awk 'NR==1 && /^```/ {fence=1; next} {lines[++n]=$0}
           END { if (fence && n > 0 && lines[n] ~ /^```[[:space:]]*$/) n--; for (i=1;i<=n;i++) print lines[i] }' \
        "$last" > "$artifact"
    fi
  else
    jq -n --arg m "$model" --argjson t "${turns:-0}" --arg e "${errors:-codex exit $rc (stderr: $err)}" --arg r "codex_exit_$rc" \
      '{type:"result", subtype:"error", is_error:true, errors:[$e], result:$e,
        num_turns:$t, total_cost_usd:"n/a(codex)", terminal_reason:$r, engine:"codex", requested_model:$m}' > "$out"
  fi
  return "$rc"
}

# ─────────────────────────────────────────── 프롬프트 조립
# build_prompt <이름> <프롬프트파일> — envsubst 한 골격 뒤에, 설계 단계면 REQUIRED_DOCS 본문을 붙인다.
# 문서 본문은 envsubst 를 **거치지 않는다** — 문서 안의 `$VAR` 문자열이 빈 문자열로 바뀌면 안 된다.
# "읽어라"가 아니라 "여기 있다"로 바꾸는 것이 이 함수의 전부다 (계기: 상단 REQUIRED_DOCS 주석).
build_prompt() {
  local name=$1 prompt_file=$2 d
  envsubst < "$prompt_file"
  [ "$name" = "design" ] && [ -n "$REQUIRED_DOCS" ] || return 0
  printf '\n\n## 정본 문서 (셸이 주입했다 — 아래 본문이 곧 파일 내용이다. Read 로 다시 열 필요 없다. 줄번호는 `파일:줄` 좌표로 쓴다)\n'
  for d in $REQUIRED_DOCS; do
    printf '\n--- %s ---\n' "$d"
    cat -n "$ROOT/$d"
  done
}

# ─────────────────────────────────────────── 단계 실행기
# run_stage <이름> <모델> <폴백체인> <프롬프트파일> <산출물경로>
#
# stream-json 으로 받아 진행 상황(도구 호출·중간 텍스트)을 실시간으로 흘리고,
# 스트림 마지막의 result 이벤트만 뽑아 게이트 판정에 쓴다.
# (--output-format json 은 완료까지 무출력이라 UX가 나빠서 교체함)
run_stage() {
  local name=$1 model=$2 fallback=$3 prompt_file=$4 artifact=$5
  local out="$WORK/$name.result.json" stream="$WORK/$name.stream.jsonl" code=0

  # 상한은 단계 이름으로 끌어온다: name=impl → TURNS_IMPL / BUDGET_IMPL
  # 인자로 더 받지 않는 이유 — 이미 5개다. 7개짜리 위치 인자는 호출부에서 순서를 틀린다.
  local upper turns_var budget_var turns budget budget_desc
  upper="$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')"
  turns_var="TURNS_$upper"; budget_var="BUDGET_$upper"
  turns="${!turns_var:-40}"; budget="${!budget_var:-}"
  if [ -n "$budget" ]; then budget_desc="예산≤\$$budget"; else budget_desc="예산 상한 없음"; fi

  # 재시도가 이전 시도의 증거를 덮어쓰지 않게 한다. 고정 이름은 유지하고(사람·테스트·
  # 도구가 그 경로를 안다) 덮어쓰기 직전에 이전 것을 번호로 밀어둔다.
  # 번호를 ATTEMPT 로 매기지 않는 이유: ATTEMPT 는 검증 루프 안에서만 올라가고
  # design·judge 는 루프 밖이라 늘 0 이다 — 두 경우가 다 남아야 한다.
  # (2026-08-24: impl 1차 실패의 증거가 2차 성공 주행에 덮여 사라졌다)
  if [ -f "$stream" ] || [ -f "$out" ]; then
    local n=1
    while [ -e "$WORK/$name.attempt$n.stream.jsonl" ] || [ -e "$WORK/$name.attempt$n.result.json" ]; do
      n=$((n + 1))
    done
    if [ -f "$stream" ]; then mv "$stream" "$WORK/$name.attempt$n.stream.jsonl"; fi
    if [ -f "$out" ];    then mv "$out"    "$WORK/$name.attempt$n.result.json"; fi
    log "  ↩ 이전 $name 증거 보관 → $name.attempt$n.*"
  fi

  # 산출물의 "실행 전 지문". 부검이 "이 파일이 이번 주행 것인가"를 이걸로 판정한다.
  # mtime 비교(-nt)를 안 쓰는 이유: macOS 기본 bash 3.2 는 mtime 을 **초 단위로만**
  # 비교해서, 같은 초 안에 끝난 단계의 산출물이 전부 이전 것으로 오판된다 (실측).
  local art_before="NONE"
  if [ -f "$artifact" ]; then art_before="$(file_hash "$artifact")"; fi

  # 종료 계약(_contract.md)에 거부 처리 계약(_denial.md)을 이어 붙인다.
  # _denial.md 가 없어도 돌아가야 한다 — 그래야 그 파일만 단독으로 되돌릴 수 있다.
  local sys_append; sys_append="$(cat "$PROMPTS/_contract.md")"
  if [ -f "$PROMPTS/_denial.md" ]; then
    sys_append="$sys_append"$'\n\n'"$(cat "$PROMPTS/_denial.md")"
  fi

  # --add-dir 은 디렉터리마다 하나씩 붙는다. 배열이 비면 아무것도 안 붙는다
  # (bash 3.2 의 set -u 에서 빈 배열 확장은 ${arr[@]+"${arr[@]}"} 꼴이어야 한다).
  local add_dirs=() d
  for d in $ADD_DIRS; do add_dirs+=(--add-dir "$d"); done

  # 모델 체인 순환. 첫 항목으로 돌리고, 레이트 리밋 거부로 죽으면 다음 항목으로
  # 갈아탄다. 리밋이 아닌 실패(예산·턴 초과, 에이전트 에러)는 갈아타지 않는다 —
  # 그건 모델을 바꾼다고 나아지는 실패가 아니고, 조용히 다른 모델로 재주행하면
  # MODEL_LOG 가 감시하려던 "다른 모델이 돌았다"를 셸이 스스로 만들어내는 꼴이 된다.
  local chain full_chain try_model rest swap=0 engine=claude entry claude_rest limited
  chain="$model${fallback:+,$fallback}"; full_chain="$chain"

  while :; do
    entry="${chain%%,*}"
    rest="${chain#*,}"; [ "$rest" = "$chain" ] && rest=""
    engine="$(chain_engine "$entry")"; try_model="$(chain_model "$entry")"
    claude_rest="$(claude_only "$rest")"

    state "RUNNING:$name" "engine=$engine, model=$try_model, 턴≤$turns, $budget_desc"
    log "▶ $name ($engine: model=$try_model, fallback=${rest:-없음}, 턴≤$turns, $budget_desc)"

    set +e
    # 프롬프트는 파일로 남긴다 — 무엇이 주입됐는지가 증거로 남아야 "안 읽었다"와 "안 줬다"를 가른다.
    build_prompt "$name" "$prompt_file" > "$WORK/$name.prompt.md"
    if [ "$engine" = codex ]; then
      # 턴·예산 상한은 codex exec 에 없다 — CODEX_TIMEOUT 이 그 자리를 맡는다.
      codex_exec "$name" "$try_model" "$artifact" "$stream" "$out"
      code=$?
    else
    # PIPELINE_STAGE: 런처 읽기 전용 훅이 "파이프라인이 띄운 단계 에이전트"를 알아보는 표시.
    # 봉인이 걸린 동안 그 훅은 worktree 쓰기를 막는데, 단계 에이전트는 쓰는 게 일이다.
    PIPELINE_STAGE="$name" \
    claude -p \
      --model "$try_model" \
      ${claude_rest:+--fallback-model "$claude_rest"} \
      --output-format stream-json \
      --verbose \
      --max-turns "$turns" \
      ${budget:+--max-budget-usd "$budget"} \
      --permission-mode acceptEdits \
      --allowedTools "$AGENT_TOOLS" \
      ${add_dirs[@]+"${add_dirs[@]}"} \
      --append-system-prompt "$sys_append" \
      < "$WORK/$name.prompt.md" \
      | tee "$stream" \
      | jq --unbuffered -Rr 'fromjson? // empty |
          select(.type? == "assistant") | .message.content[]? |
          if .type == "tool_use" then
            "  ⚙ \(.name)  \((.input.file_path // .input.command // .input.pattern // .input.description // "") | tostring | .[0:90])"
          elif .type == "text" and ((.text // "") | length) > 0 then
            "  💬 \(.text | gsub("\\s+"; " ") | .[0:160])"
          else empty end' >&2
    code=${PIPESTATUS[0]}   # [0]=claude [1]=tee [2]=jq — 판정 기준은 claude
    fi
    set -e

    # 사인을 먼저 확보한다 — exit code 검사보다 **앞**이다. claude 가 0 이 아닌 코드로
    # 죽어도 스트림 마지막 result 이벤트에는 이유가 들어 있다. 예전엔 순서가 반대라
    # 그 파일을 손에 쥐고도 exit code 숫자 하나만 보고 버렸다.
    #
    # stream-json 은 NDJSON 인데 claude 가 JSON 이 아닌 줄을 stdout 으로 흘릴 때가 있다
    # (2026-08-26 실측: MCP 서버 경고가 6번째 줄에 섞였다). jq 기본 파서는 그 한 줄에 죽고
    # tee 와 claude 가 SIGPIPE 로 연달아 죽는다 — 멀쩡히 일하던 $5 짜리 verify 가 그렇게
    # 날아갔다. fromjson? 으로 관용 파싱하되 버린 줄은 세어서 보고한다.
    # codex 경로는 codex_exec 가 이미 $out 을 만들었다.
    [ "$engine" = codex ] \
      || jq -Rn '[inputs | fromjson? | select(.type? == "result")] | last' "$stream" > "$out" 2>/dev/null || true

    # 리밋(codex 는 모델 사용 불가 포함)으로 죽었는가. codex 는 실패했을 때만 본다 —
    # 정상 주행의 stderr 에도 ERROR 줄이 섞인다 (codex_unavailable 주석).
    limited=0
    if [ "$engine" = codex ]; then
      if [ "$code" -ne 0 ] && codex_unavailable "$stream" "$WORK/$name.codex.stderr"; then limited=1; fi
    elif rate_limited "$stream"; then
      limited=1
    fi

    # codex 체인이 바닥났으면 claude 로 **자동으로 넘기지 않는다** — 사람이 정한다 (A 방식).
    # 체인 끝에 claude 항목을 넣어 둔 경우는 순환이 거기로 넘어가므로 여기 오지 않는다.
    if [ "$limited" = 1 ] && [ -z "$rest" ] && [ "$engine" = codex ]; then
      fail_log "$name: codex 체인 전부 리밋·사용 불가 ($full_chain)" <<EOF
마지막 시도: $try_model
증거: $stream / $WORK/$name.codex.stderr
EOF
      need_human "ENGINE_EXHAUSTED:$name" "codex 체인($full_chain)이 전부 리밋·사용 불가" \
        "1) 사람에게 이 한 가지만 물어라: \"$name 을 claude opus 5.5 로 돌릴까요? (y/n)\" 2) 정확히 y 면 원래 실행 명령 앞에 $CODEX_SWITCH_HINT 를 붙여 재실행한다 — 기존 DESIGN.md 는 재사용되므로 FRESH_DESIGN 을 주지 마라. 3) n 이면 codex 리밋이 풀린 뒤 같은 명령으로 재실행하면 된다고 보고하고 기다린다. 사인은 $FAIL_LOG 마지막 항목."
    fi

    # 아직 안 써본 모델이 남아 있고 리밋으로 죽었을 때만 갈아탄다.
    if [ -z "$rest" ] || [ "$limited" != 1 ]; then break; fi

    swap=$((swap + 1))
    mv "$stream" "$WORK/$name.ratelimit$swap.stream.jsonl" 2>/dev/null || true
    mv "$out"    "$WORK/$name.ratelimit$swap.result.json"  2>/dev/null || true
    log "  ⚠ $try_model 리밋·사용 불가 — ${rest%%,*} 로 갈아탄다 (증거: $name.ratelimit$swap.*)"
    fail_log "$name: $try_model 레이트 리밋 거부 — ${rest%%,*} 로 전환" <<EOF
--fallback-model 은 과부하·부재만 받는다. 창 소진 거부(codex 는 모델 사용 불가 포함)는 셸이 감지해 갈아탄다.
증거: $WORK/$name.ratelimit$swap.stream.jsonl
EOF
    chain="$rest"
  done

  local junk
  junk=$(grep -cv '^{' "$stream" 2>/dev/null || true)
  [ "${junk:-0}" -gt 0 ] \
    && log "  ⚠ 스트림에 JSON 아닌 줄 ${junk}개 — 무시하고 진행 (원문: $stream)"

  # 모델 교체 감시는 **죽은 경로에서도** 돈다. 다른 모델이 돌다 상한에 닿은 것이라면,
  # 사람이 "이 산출물을 신뢰할까"를 판단할 때 그 사실을 알아야 한다.
  if [ "$engine" = codex ]; then
    # codex exec --json 은 실제로 돈 모델을 보고하지 않는다 (2026-09-30 실측: 이벤트에 model 필드 없음).
    # 추측하지 않고 "보고 없음"을 남긴다 — claude 쪽 "필드명 점검 필요" 경고와 섞지 않는다.
    echo "- $(date -Iseconds) | $name | codex $try_model — 실제 모델은 codex 가 보고하지 않음" >> "$MODEL_LOG"
  elif [ "$(jq -r 'type' "$out" 2>/dev/null)" = "object" ]; then
    if [ "$code" -eq 0 ]; then check_model_swap "$name" "$out" "$try_model" 1
    else                       check_model_swap "$name" "$out" "$try_model" 0
    fi
  fi

  if [ "$code" -ne 0 ]; then
    # 돌아왔다 = 사람이 산출물을 신뢰하기로 했다. 곧장 반환한다 — 죽은 주행의 result 는
    # is_error=true 라 아래 검사에 걸려서, 계속 내려가면 사람의 승인이 무효가 된다.
    stage_postmortem "$name" "$out" "$stream" "$code" "$artifact" "$art_before"
    return 0
  fi

  [ "$(jq -r 'type' "$out" 2>/dev/null)" = "object" ] \
    || die "$name: 스트림에 result 이벤트가 없음 → $stream 확인"

  [ "$(jq -r '.is_error' "$out")" = "false" ] \
    || die "$name: 에이전트 에러 — $(jq -r '.result' "$out" | head -c 300)"

  log "  \$$(jq -r '.total_cost_usd' "$out") / 턴 $(jq -r '.num_turns' "$out")"

  # 게이트 1: 산출물 물리적 존재
  [ -f "$artifact" ] || die "$name: 산출물 없음 → $artifact"

  # 게이트 2: 종료 형식
  local verdict
  verdict="$(grep -m1 '^STATUS:' "$artifact" | awk '{print $2}' || true)"
  case "${verdict:-MISSING}" in
    DONE)
      log "  ✔ $name DONE" ;;
    BLOCKED)
      emit_blocked "$name" "$artifact" ;;
    *)
      die "$name: STATUS 라인 없음 또는 형식 위반 (DONE|BLOCKED 필수)" ;;
  esac
}

# ─────────────────────────────────────────── 사람 게이트
# 런처는 여기서 판단하지 않는다 (y 중계만). 판단은 사람만 한다.
# gate_human <메시지> <검토파일> [force] [승인명령]
#
# 4번째 인자는 tty 없는 경로(exit 4)에서 "사람이 y 라고 답하면 실행할 명령"이다.
# 기본은 approve.sh 마커(--relayed)지만, 파킹된 산출물은 mv 가 승인이다.
#
# 런처 모드의 승인 계약 (2026-09-04 사용자 결정): 메인 세션은 **판단 금지**다.
# 파일을 보여주고 "승인? (y/n)" 하나만 물은 뒤, 사람이 정확히 y 라고 답했을 때만
# 아래 승인명령을 실행한다. 요약·추천·"괜찮아 보인다"는 계약 위반이다 — 사람이
# 요약만 읽고 y 를 누르는 순간 이 게이트는 텍스트 규칙이 된다 (design-notes §7).
#
# force=1 이면 AUTO=1 이어도 멈춘다. 검증되지 않은 주장을 무인으로 통과시키면
# 이 파이프라인이 막으려는 것(근거 없는 판단이 구현까지 흘러가는 것)이 그대로
# 일어난다 — 무인 모드는 "게이트를 없앤다"가 아니라 "판정 가능한 것만 자동으로
# 넘긴다"는 뜻이다.
gate_human() {
  local msg=$1 file=$2 force=${3:-0} approve_cmd=${4:-}
  [ -n "$approve_cmd" ] \
    || approve_cmd="$ROOT/approve.sh $FEATURE $(basename "$file") --relayed y"

  # 승인 마커: 사람이 approve.sh 로 "이 내용을 검토했다"를 남긴 것.
  # 해시로 내용에 묶여 있어 승인 후 파일이 바뀌면 무효가 된다.
  # AUTO 보다 먼저 본다 — 명시적 승인은 force 게이트까지 통과시키는 유일한
  # 무인 경로다 (AUTO 는 force 를 못 넘는다).
  local marker="$file.approved"
  if [ -f "$marker" ]; then
    if [ "$(cat "$marker")" = "$(file_hash "$file")" ]; then
      log "  ✔ 승인 마커 — 게이트 통과: $msg"
      return 0
    fi
    log "  ⚠ 승인 마커가 낡음 ($(basename "$file") 이 승인 뒤에 바뀜) — 재승인 필요"
  fi

  [ "$AUTO" = "1" ] && [ "$force" != "1" ] \
    && { log "  (AUTO=1 — 게이트 통과: $msg)"; return 0; }

  state "GATE" "$msg" "tty 게이트에서 사람 응답 대기 중 — 런처 개입 불필요."
  cat >&2 <<EOF

$(printf '\033[1;33m[게이트]\033[0m') $msg
  검토 대상: $file

  y = 진행   e = 열어보기   n = 중단
EOF
  printf '  > ' >&2
  # tty 가 없으면(런처 모드·cron·CI) read 가 rc=1 로 끝난다. 예전에는 n 과 같이
  # 취급해 exit 2 로 죽였는데, 그러면 호출자가 "사람이 거부함"(2)과 "사람이 아직
  # 검토하지 않음"을 구분할 수 없다. 후자는 별도 코드(4)로 내보내고 승인 방법을
  # 찍어 준다. `|| ans=...` 가드가 없으면 set -e 가 read 실패 지점에서 exit 1 을
  # 내 어느 경로도 타지 못한다 (2026-08-18 실전에서 밟은 함정).
  local ans; read -r ans < /dev/tty || ans=__NO_TTY__
  case "$ans" in
    y|Y) return 0 ;;
    e|E) "${EDITOR:-less}" "$file"; gate_human "$msg" "$file" "$force" "$approve_cmd" ;;
    __NO_TTY__)
      state "AWAITING_APPROVAL" "$msg — $(basename "$file")" \
        "1) $file 의 내용을 사람에게 **그대로** 보여줘라 — 요약·추천·의견 금지, 판단은 사람이 한다. 2) AskUserQuestion 으로 \"승인? (y/n)\" 하나만 물어라. 3) 사람의 답이 정확히 y 일 때만 실행: $approve_cmd  — y 가 아닌 답(\"알아서\", \"괜찮으면\")은 승인이 아니다. 다시 묻거나 중단을 보고해라. 4) 승인 뒤 같은 명령으로 재실행하면 이 게이트를 통과한다. 승인 기록은 $WORK/APPROVALS.md 에 남는다."
      {
        printf '\033[1;33m[승인 대기]\033[0m tty 가 없어 게이트에서 멈춘다 (exit 4)\n'
        printf '  검토 대상: %s\n' "$file"
        printf '  승인 명령: 사람이 y 라고 답한 뒤 %s\n' "$approve_cmd"
        printf '  자세한 안내는 %s 의 "다음 행동" 블록에 있다\n' "$STATE"
      } >&2
      exit 4 ;;
    *)   die "사람이 중단함" ;;
  esac
}

# ─────────────────────────────────────────── 범위 게이트
# 에이전트가 설계에 없는 파일을 건드렸는지 git 에게 묻는다.
#
# 실측 근거: 어느 실행에서 설계가 "변경하지 않는다"고 명시한 파일을 구현 단계가
# 77줄 고쳤다 (2026-08-17). 그때도 impl.md 에는 "목록에 없는 파일은 BLOCKED"
# 라고 적혀 있었다 — 프롬프트로 부탁한 규칙은 지켜지지 않는다. 그래서 셸이 확인한다.
#
# 판정 근거는 DESIGN.md 의 ALLOWED_FILES 블록 하나뿐이다.
# 사람이 읽는 표를 파싱하지 않는 이유: 형식이 흔들리고, 흔들리는 걸 파싱하면
# 게이트가 조용히 통과시킨다. 계약은 기계가 읽을 수 있는 모양이어야 한다.
#
# extract_allowed_files 는 gate_scope 와 "승인 직후 훅 연동" 두 곳에서 쓴다. 로직을
# 두 벌로 두면 한쪽만 고쳐졌을 때 게이트가 판정하는 목록과 훅이 허용하는 목록이
# 갈라진다 — 그때 증상은 "설계에 있는 파일인데 거부됨"이라 원인을 찾기 어렵다.
extract_block() {   # extract_block <블록 헤더> <출력파일>
  # grep 은 매치가 0건이면 exit 1 이다. set -e 아래에서 그건 "계약이 비었다"가 아니라
  # "스크립트 사망"으로 나타난다 — 판정하기 전에 죽으므로 반드시 감싼다.
  set +e
  sed -n "/^$1:/,/^[[:space:]]*$/p" "$WORK/DESIGN.md" \
    | grep '^- ' | sed -e 's/^- *//' -e 's|^\./||' | sort -u > "$2"
  set -e
}
extract_allowed_files() { extract_block ALLOWED_FILES "$1"; }
extract_test_files()    { extract_block TEST_FILES    "$1"; }

# ─────────────────────────────────────────── 계약 형식 게이트
# 설계 직후, judge 를 띄우기 **전에** 두 블록의 형식을 본다 — 여기서 죽으면 judge 비용이 안 든다.
# ALLOWED_FILES 는 범위 게이트가, TEST_FILES 는 단계별 쓰기 게이트가 읽는다. 둘 다 사람용
# 표가 아니라 기계용 블록이고, 없으면 게이트가 "이탈 0개"로 조용히 통과시키므로 없음=위반이다.
# TEST_FILES 는 비어 있어도 된다(테스트 없는 설계) — 헤더 자체는 있어야 한다.
gate_contract() {
  local allowed="$WORK/.contract_allowed" tests="$WORK/.contract_tests" stray
  extract_allowed_files "$allowed"
  [ -s "$allowed" ] \
    || die "design: DESIGN.md 에 ALLOWED_FILES 블록이 없다 — 범위를 계약으로 만들 수 없다 (prompts/design.md 참조)"
  grep -q '^TEST_FILES:' "$WORK/DESIGN.md" \
    || die "design: DESIGN.md 에 TEST_FILES 블록이 없다 — 어느 단계가 어느 파일을 고쳐도 되는지 가를 수 없다 (prompts/design.md 참조)"
  extract_test_files "$tests"
  stray="$(comm -13 "$allowed" "$tests")"
  [ -z "$stray" ] \
    || die "design: TEST_FILES 에 ALLOWED_FILES 밖의 파일이 있다: $(printf '%s' "$stray" | tr '\n' ' ')— 두 블록은 부분집합 관계여야 한다"
  log "  ✔ 계약 형식 (허용 $(wc -l < "$allowed" | tr -d ' ')개 · 테스트 $(wc -l < "$tests" | tr -d ' ')개)"
  rm -f "$allowed" "$tests"
}

gate_scope() {
  local stage=$1
  local allowed="$WORK/allowed_files.txt" changed="$WORK/changed_files.txt"

  extract_allowed_files "$allowed"

  # -z + core.quotePath=false. 앞서 쓰던 `status --porcelain | cut -c4-` 가 두 가지를
  # 조용히 틀렸다 — 둘 다 게이트가 "정상 구현을 죽이는" 방향이라 더 나쁘다:
  #   ① rename 은 한 줄에 'R  old -> new' 로 온다. 4번째 글자부터 자르면
  #      'old -> new' 라는 존재하지 않는 경로 하나가 되어 계약에 없는 파일로 읽힌다.
  #      파일을 옮기는 리팩터링마다 규칙을 지킨 구현이 exit 2 로 죽었다.
  #   ② 한글 등 non-ASCII 경로는 C-quote 된다("\354\203\210\355\217\264\353\215\224/…").
  #      잘라내도 이스케이프가 남아 ALLOWED_FILES 의 원문과 영영 매치되지 않는다.
  # -z 는 NUL 구분이라 인용을 하지 않고, rename/copy 는 'XY <new>\0<old>\0' 로 두
  # 항목이 되어 새 경로와 원래 경로가 **둘 다** 계약과 대조된다 — 파일을 옮기려면
  # 양쪽이 다 설계에 적혀 있어야 한다는 뜻이고, 그게 이 게이트가 원하는 바다.
  # 파싱은 순수 bash 다. awk 의 RS="\0" 은 구현마다 갈리고 python 은 의존성이 는다.
  # -uall : 새 디렉터리 안의 파일을 디렉터리 하나로 뭉치지 않고 파일 단위로 본다.
  # .pipeline/ 은 산출물이라 항상 제외한다 (.gitignore 가 지워져도 게이트는 살아 있어야 한다)
  local rec
  set +e
  : > "$changed.raw"
  while IFS= read -r -d '' rec; do
    printf '%s\n' "${rec:3}" >> "$changed.raw"
    # rename/copy 의 원본 경로는 다음 레코드로 따로 오고 상태코드가 붙지 않는다
    case "${rec:0:1}" in
      R|C) IFS= read -r -d '' rec && printf '%s\n' "$rec" >> "$changed.raw" ;;
    esac
  done < <(git -C "$ROOT" -c core.quotePath=false status --porcelain -z -uall)
  sed 's|^\./||' "$changed.raw" | grep -v '^\.pipeline/' | sort -u > "$changed"
  rm -f "$changed.raw"
  set -e

  [ -s "$allowed" ] \
    || die "$stage: DESIGN.md 에 ALLOWED_FILES 블록이 없다 — 범위를 계약으로 만들 수 없다 (prompts/design.md 참조)"

  local strays count
  strays="$(comm -13 "$allowed" "$changed")"
  if [ -n "$strays" ]; then
    count=$(printf '%s\n' "$strays" | wc -l | tr -d ' ')
    log "⚠ $stage: 설계에 없는 파일이 변경됐다 (${count}개)"
    printf '%s\n' "$strays" | sed 's/^/     /' >&2
    printf '%s\n' "$strays" | fail_log "scope creep ($stage)"
    die "$stage: 범위 이탈 ${count}개 → $FAIL_LOG"
  fi

  log "  ✔ $stage 범위 준수 (계약 $(wc -l < "$allowed" | tr -d ' ')개 파일)"
}

# ─────────────────────────────────────────── 보호 파일 게이트
# 어느 단계도 건드리면 안 되는 파일(PROTECTED_FILES)과, 설계 게이트를 통과한 뒤의
# 파이프라인 산출물(DESIGN.md·JUDGE.md)을 지문으로 감시한다.
#
# git diff 대신 지문 비교를 쓰는 이유: 파이프라인 시작 시점에 이미 더러운 워킹 트리에서
# 돌리면 diff 기반 검사가 첫 시도부터 헛발질한다. 검사할 것은 "지금 더러운가"가 아니라
# "이번 실행이 바꿨는가"다. 같은 이유로 .pipeline/ 안의 산출물(범위 게이트가 제외한다)도
# 이 방식으로만 잡힌다 — impl 이 DESIGN.md 를 덮어쓴 실패 모드(design-notes §6)가 그것이다.
#
# **기준선을 찍는 위치가 곧 검사 범위다.** 기준선 이전의 변경은 흡수돼 영원히 안 잡힌다.
# 그래서 첫 run_stage 보다 앞에서 찍고, 설계 게이트 통과 직후 산출물을 목록에 넣으며 한 번
# 다시 찍는다 (그 사이 변경은 design·judge 뒤의 check_protected 가 이미 봤다).
# ─────────────────────────────────────────── 설계 읽기 게이트
# 설계가 ALLOWED_FILES 에 "고치겠다"고 적은 **기존 소스 파일**을 Read 로 열어봤는가를
# design.stream.jsonl 에서 확인한다. 열어보지도 않은 파일을 고치겠다는 설계는 그 자체가 누락
# 신호다 — 2026-09-05 소급: Opus 설계가 2025-12 부터 있던 dart 파일을 Read·Grep·Glob 어느
# 것으로도 안 열고 변경 목록에 넣었고, judge 반박으로 이어졌다.
#
# 소스만 본다(TEST_FILES 제외 — 테스트는 verify 가 쓴다). 신규 파일은 읽을 수 없으니 제외한다.
# 정본 문서(REQUIRED_DOCS)는 여기서 안 본다 — 주입돼 있어 Read 가 필요 없다.
# 부분 읽기(offset/limit)도 "읽음"으로 친다 — 범위 합산은 측정이 쌓이면 넣는다.
# judge 전에 둔다: 여기서 죽으면 버리는 건 설계 1회($2.6~3.4)이고 judge 비용은 안 붙는다.
# 설계를 재사용한 실행은 건너뛴다 — 스트림이 이번 주행 것이 아니다.
# 경로 대조는 접미사 일치다: Read 의 file_path 는 절대 경로이고 macOS 에서 /tmp 와 /private/tmp
# 처럼 같은 파일이 다른 접두사로 보일 수 있다.
check_design_reads() {
  local stream="$WORK/design.stream.jsonl"
  local allowed="$WORK/.reads_allowed" tests="$WORK/.reads_tests" seen="$WORK/.reads_seen"
  extract_allowed_files "$allowed"; extract_test_files "$tests"
  jq -Rr 'fromjson? // empty | select(.type=="assistant") | .message.content[]?
          | select(.type=="tool_use" and .name=="Read") | .input.file_path // empty' \
    "$stream" 2>/dev/null | sort -u > "$seen"

  local f missing="" count=0 checked=0
  while read -r f; do
    [ -n "$f" ] || continue
    [ -f "$ROOT/$f" ] || continue
    checked=$((checked + 1))
    if ! awk -v f="$f" '{ if ($0 == f || substr($0, length($0) - length(f)) == "/" f) ok = 1 } END { exit !ok }' "$seen"; then
      missing="$missing$f"$'\n'; count=$((count + 1))
    fi
  done < <(comm -23 "$allowed" "$tests")
  rm -f "$allowed" "$tests" "$seen"

  if [ "$count" -gt 0 ]; then
    printf '%s' "$missing" | fail_log "design: 고치겠다는 파일을 읽지 않음 (${count}개)"
    die "design: ALLOWED_FILES 의 기존 소스 ${count}개를 Read 로 열어보지 않았다: $(printf '%s' "$missing" | tr '\n' ' ')— 설계 재실행 시 이 파일들을 먼저 읽어라 (FAIL_LOG 에 기록됨)"
  fi
  log "  ✔ design 읽기 범위 준수 (기존 소스 ${checked}개 전부 Read)"
}

# fingerprint_paths <경로...> — 저장소 루트 기준 상대 경로마다 "경로 해시" 한 줄. 없는 파일은 "(없음)".
# 보호 파일 게이트와 단계별 쓰기 게이트가 같은 함수를 쓴다 — 지문 형식이 두 벌이면 한쪽만 고쳐진다.
fingerprint_paths() {
  local f
  for f in "$@"; do
    if [ -f "$ROOT/$f" ]; then
      printf '%s %s\n' "$f" "$(file_hash "$ROOT/$f")"
    else
      printf '%s (없음)\n' "$f"
    fi
  done
}
protected_fingerprint() { fingerprint_paths $PROTECTED_FILES $ARTIFACT_GUARD; }

# changed_paths <기준선> <현재> — 두 지문 사이에서 달라진 경로(생성·삭제 포함)를 한 줄에 하나씩.
changed_paths() {
  diff <(printf '%s\n' "$1") <(printf '%s\n' "$2") \
    | grep '^[<>]' | awk '{print $2}' | sort -u || true
}

PROTECTED_BASELINE=""

# check_protected <단계이름>
# 매 단계 직후에 부른다. 늦게 볼수록 그 위에 코드와 테스트가 쌓여 되돌리는 비용이 올라간다.
check_protected() {
  local stage=$1 changed
  changed="$(changed_paths "$PROTECTED_BASELINE" "$(protected_fingerprint)" | tr '\n' ' ')"
  [ -z "$changed" ] \
    || die "$stage 단계가 보호 파일을 수정함: $changed — git checkout 으로 되돌린 뒤 설계부터 다시 볼 것 (의도한 변경이면 PROTECTED_FILES 에서 빼고 재실행)"
}

# ─────────────────────────────────────────── 단계별 쓰기 권한 게이트
# 범위 게이트(ALLOWED_FILES)는 "어느 파일이 바뀌어도 되는가"만 본다. "**누가** 바꿔도 되는가"는
# 모른다 — 그래서 검증이 소스를 땜질해 테스트를 통과시키는 것과 구현이 테스트를 깎아 통과시키는
# 것이 둘 다 목록 안에서 일어나며 범위 게이트를 그대로 지난다. 프롬프트가 둘 다 금지하지만
# 프롬프트는 게이트가 아니다 (2026-09-04 분석 — verify 스트림 13개에서 사건 0건이었으나
# 게이트가 잡은 0 이 아니라 아직 안 일어난 0 이었다).
#
# 판정: DESIGN.md 의 TEST_FILES 블록이 테스트 파일이고, 나머지 ALLOWED_FILES 가 소스다.
#   impl   → 테스트 파일을 바꾸면 위반
#   verify → 소스 파일을 바꾸면 위반
# git status 가 아니라 지문인 이유: 재시도 2차의 impl 은 1차 verify 가 남긴 테스트 파일을
# 워킹트리에서 본다. "지금 더러운가"가 아니라 "**이 단계가** 바꿨는가"를 봐야 하므로 기준선을
# 단계 직전에 찍는다. 이름 패턴(test_*.py 등)으로 가르지 않는 이유는 저장소마다 규칙이 달라
# 패턴이 자라고, 안 걸리는 픽스처 하나에서 오탐이 나기 때문이다 — 설계가 명시한다.
#
# stage_baseline  — ALLOWED_FILES 전체의 지문. 단계 직전에 찍는다.
# check_stage_writes <단계> <기준선> <금지목록파일> <설명> — 바뀐 파일 ∩ 금지목록이 비어야 통과.
stage_baseline() { fingerprint_paths $(cat "$WORK/allowed_files.txt"); }

check_stage_writes() {
  local stage=$1 baseline=$2 forbidden=$3 what=$4 changed bad count
  changed="$(changed_paths "$baseline" "$(stage_baseline)")"
  bad="$(comm -12 <(printf '%s\n' "$changed" | sort -u) <(sort -u "$forbidden"))"
  [ -n "$bad" ] || { log "  ✔ $stage 쓰기 권한 준수 ($what 미변경)"; return 0; }
  count=$(printf '%s\n' "$bad" | wc -l | tr -d ' ')
  printf '%s\n' "$bad" | fail_log "$stage 단계가 $what 를 수정함 (${count}개)"
  die "$stage 단계가 $what 를 수정함: $(printf '%s' "$bad" | tr '\n' ' ')— 이 단계의 권한 밖이다. impl 이면 테스트를 고쳐 통과시킨 것이고 verify 면 소스를 고쳐 통과시킨 것이다 → $FAIL_LOG"
}

# ─────────────────────────────────────────── 단계 사이 봉인
# 게이트는 전부 "이 단계가 무엇을 바꿨는가"를 단계 **직전** 기준선으로 본다. 그래서 단계와
# 단계 **사이**의 수정 — 사람 승인을 기다리는 동안, exit 4 로 멈췄다 재실행하기 전 — 은
# 다음 단계의 기준선에 흡수돼 어느 게이트에도 안 잡힌다. 그 틈에 깨어 있는 것은 런처 세션과
# 사람이다 (2026-09-30: 상담역 제거로 런처가 쓰기 도구를 가진 채 대화하게 됐다).
#
# 봉인 = worktree 에서 HEAD 대비 바뀐 파일(.pipeline/ 제외)의 지문. 시작과 각 단계 게이트
# 통과 직후에 찍고, 다음 단계 직전과 다음 실행 시작 때 대조한다. 누가 고쳤는지는 묻지 않는다
# — 에디터든 다른 세션이든 사람이든 "어느 단계도 쓰지 않은 수정"이면 게이트가 판정할 수 없다.
#
# 봉인은 exit 4(승인 대기)·5(사람 조치)에서 **유지**되고 DONE·die·BLOCKED 에서 풀린다.
# 런처 읽기 전용 훅(hooks/pipeline-launcher-guard.sh)도 이 파일로 "실행 중"을 판정한다.
SEAL="$WORK/.seal"

worktree_fingerprint() {
  local rec f
  {
    while IFS= read -r -d '' rec; do
      printf '%s\n' "${rec:3}"
      case "${rec:0:1}" in R|C) IFS= read -r -d '' rec && printf '%s\n' "$rec" ;; esac
    done < <(git -C "$ROOT" -c core.quotePath=false status --porcelain -z -uall)
  } | sed 's|^\./||' | { grep -v '^\.pipeline/' || true; } | sort -u \
    | while IFS= read -r f; do
        if [ -f "$ROOT/$f" ]; then printf '%s %s\n' "$f" "$(file_hash "$ROOT/$f")"
        else printf '%s (없음)\n' "$f"; fi
      done
}

seal_worktree() {   # seal_worktree <어느 시점인지>
  worktree_fingerprint > "$SEAL"
  printf '%s\n' "$1" > "$SEAL.at"
}

check_seal() {      # check_seal <지금 시작하려는 것>
  [ -f "$SEAL" ] || return 0
  local changed at
  changed="$(changed_paths "$(cat "$SEAL")" "$(worktree_fingerprint)")"
  [ -n "$changed" ] || return 0
  at="$(cat "$SEAL.at" 2>/dev/null || echo '?')"
  printf '%s\n' "$changed" | fail_log "단계 사이 수정 감지 ($at 이후, $1 전)"
  KEEP_SEAL=1 die "단계 사이에 worktree 가 바뀌었다 ($at 이후, $1 전): $(printf '%s' "$changed" | tr '\n' ' ')— 어느 단계도 쓰지 않은 수정이라 게이트가 판정할 수 없다. 런처는 되돌리거나 봉인을 풀지 말고 사람에게 보고해라. 사람이 의도한 수정이라고 확인하면 rm '$SEAL' 뒤 재실행, 아니면 사람이 되돌린 뒤 재실행"
}

# ─────────────────────────────────────────── 검증 실행
# run_with_timeout <초> <명령> — 상한 초과면 124 를 돌려준다 (timeout(1) 과 같은 약속).
# coreutils timeout 이 없는 macOS 에서도 상한이 걸려야 하므로 bash 워치독으로 대체한다.
run_with_timeout() {
  local secs=$1 cmd=$2
  if [ "$secs" -le 0 ]; then bash -c "$cmd"; return $?; fi
  if command -v timeout >/dev/null 2>&1;  then timeout  -k 5 "$secs" bash -c "$cmd"; return $?; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout -k 5 "$secs" bash -c "$cmd"; return $?; fi

  local flag="$WORK/.timeout_fired" pid wd rc=0
  rm -f "$flag"
  bash -c "$cmd" & pid=$!
  ( sleep "$secs"
    if kill -0 "$pid" 2>/dev/null; then
      touch "$flag"; kill -TERM "$pid" 2>/dev/null; sleep 2; kill -KILL "$pid" 2>/dev/null
    fi ) >/dev/null 2>&1 & wd=$!  # the orphaned sleep must not hold the caller's pipe open
  wait "$pid" || rc=$?
  kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null || true
  if [ -f "$flag" ]; then rm -f "$flag"; return 124; fi
  return "$rc"
}

# 검증 목록 = $TEST_CMD + (있으면) 기능 폴더의 smoke.sh. 순서대로 돌리고 첫 실패에서 멈춘다.
# smoke.sh 를 여기 붙이는 이유: 오케스트레이터는 기능 중립이어야 하므로 라우트나 포트를
# 하드코딩하지 않고, 기능별 스모크는 파일이 있을 때만 마지막에 돈다.
verify_commands() {
  local line
  if [ -n "$TEST_CMDS" ]; then
    while IFS= read -r line; do
      line="${line#"${line%%[![:space:]]*}"}"
      line="${line%"${line##*[![:space:]]}"}"
      [ -n "$line" ] && printf '%s\n' "$line"
    done <<< "$TEST_CMDS"
  else
    printf '%s\n' "$TEST_CMD"
  fi
}

run_verify() {
  local cmd rc
  local -a cmds=()
  while IFS= read -r cmd; do cmds+=("$cmd"); done < <(verify_commands)
  # 빈 목록을 통과로 취급하면 게이트가 사라진 걸 아무도 모른다 — 실패보다 나쁘다.
  [ "${#cmds[@]}" -gt 0 ] || die 2 "검증 명령이 하나도 없다 (TEST_CMDS 가 공백뿐이다)"
  [ -f "$WORK/smoke.sh" ] && cmds+=("bash '$WORK/smoke.sh'")

  VERIFY_PASSED=""
  VERIFY_FAILED=""
  : > "$WORK/test_out.txt"

  for cmd in "${cmds[@]}"; do
    log "  ▸ $cmd"
    echo "### \$ $cmd" >> "$WORK/test_out.txt"
    rc=0
    (cd "$ROOT" && run_with_timeout "$VERIFY_TIMEOUT" "$cmd") >> "$WORK/test_out.txt" 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "→ 통과" >> "$WORK/test_out.txt"; echo >> "$WORK/test_out.txt"
      VERIFY_PASSED="$VERIFY_PASSED${VERIFY_PASSED:+, }$cmd"
    elif [ "$rc" -eq 124 ]; then
      # 상한 초과를 그냥 "실패"로 뭉개면 다음 시도의 impl 이 FAIL_LOG 를 읽고 "테스트가
      # 틀렸구나"로 오해한다 — 실제로는 안 돌아온 것이다.
      VERIFY_FAILED="$cmd (${VERIFY_TIMEOUT}초 시간 초과 — 명령이 돌아오지 않았다)"
      # ${VAR}초 — bash 3.2 는 $VAR 바로 뒤의 한글 바이트를 변수명으로 읽는다 (unbound variable)
      echo "→ 시간 초과 (${VERIFY_TIMEOUT}초). 무한 대기하는 테스트를 의심하라." >> "$WORK/test_out.txt"
      return 1
    else
      VERIFY_FAILED="$cmd"
      echo "→ 실패 (exit $rc)" >> "$WORK/test_out.txt"
      return 1
    fi
  done
  return 0
}

# ─────────────────────────────────────────── 프리플라이트
# 에이전트를 띄우기 전에 환경 기준선을 판정한다. 여기서 죽으면 비용이 $0 이다.
# 실패 원인을 환경으로 단정하지 않는다 — 이미 워킹트리에 있던 미완성 코드일 수도 있다.
# 확실한 것 하나만 말한다: **에이전트가 만든 것은 아니다**.
preflight() {
  [ -n "$PREFLIGHT_CMD" ] || return 0
  log "▶ 프리플라이트: $PREFLIGHT_CMD (기준선)"
  local rc=0
  (cd "$ROOT" && run_with_timeout "$VERIFY_TIMEOUT" "$PREFLIGHT_CMD") > "$WORK/preflight_out.txt" 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then
    PREFLIGHT_STATE="통과: $PREFLIGHT_CMD"
    log "  ✔ 기준선 녹색 — 이후 같은 명령의 실패는 에이전트가 만든 것이다"
  else
    PREFLIGHT_STATE="실패: $PREFLIGHT_CMD (exit $rc)"
    tail -30 "$WORK/preflight_out.txt" >&2
    die "프리플라이트 실패 (exit $rc) — 에이전트는 아직 한 번도 안 띄웠으므로(비용 \$0) **에이전트가 만든 문제가 아니다**. 환경이거나 이미 워킹트리에 있던 코드다. 고친 뒤 다시 실행해라 → $WORK/preflight_out.txt"
  fi
}

# ─────────────────────────────────────────── 파이프라인
ATTEMPT=0
state "START"
log "=== $FEATURE 시작 ==="
log "상태는 $STATE 에 실시간으로 쓴다 — 런처 세션은 이 파일만 읽으면 된다"

# 검증 명령이 선언되지 않았으면 여기서 죽는다. 이 게이트가 없으면 "각색을 안 한 저장소"가
# 조용히 남의 기본값으로 돌고, 초록이어도 무엇을 통과한 건지 아무도 모른다.
if [ -z "$TEST_CMD" ] && [ -z "$TEST_CMDS" ]; then
  die 2 "검증 명령이 선언되지 않았다 — orchestrate.sh 상단의 TEST_CMD 또는 TEST_CMDS 를 이 저장소에 맞게 채워라 (CI 가 도는 검사 전부)"
fi

# 이전 실행이 봉인을 남기고 멈췄다면(exit 4·5) 그 사이의 수정부터 본다 — 아무것도 하기 전에.
check_seal "이번 실행 시작"

# 엔진 준비(codex 설치·로그인)는 에이전트를 띄우기 전에 본다 — 설계 비용을 버리지 않기 위해서다.
check_engines

preflight

# 주입할 정본 문서는 시작 전에 확인한다 — 오타 하나로 설계가 문서 없이 도는 것보다 여기서 죽는 게 싸다($0).
for _d in $REQUIRED_DOCS; do
  [ -f "$ROOT/$_d" ] || die "REQUIRED_DOCS 에 없는 파일: $_d (저장소 루트 기준 상대 경로)"
done

# 보호 파일 기준선 — 어떤 run_stage 보다 위에 있어야 한다 (위 check_protected 주석).
PROTECTED_BASELINE="$(protected_fingerprint)"
seal_worktree "실행 시작"

# RESUME_FROM 은 건너뛰기다. 근거가 없으면 조용히 넘어가는 대신 여기서 죽는다 —
# 오타(RESUME_FROM=verfiy)가 "그냥 impl 이 또 돌았다"로 나타나면 알아채지 못한다.
if [ -n "$RESUME_FROM" ]; then
  [ "$RESUME_FROM" != "verify" ] \
    || die "RESUME_FROM=verify 는 없어졌다 — verify 는 이제 impl 앞에서 돈다 (2026-09-30 red→green). 테스트를 재사용하고 impl 부터 돌리려면 RESUME_FROM=impl"
  [ "$RESUME_FROM" = "impl" ] \
    || die "RESUME_FROM 은 impl 만 지원한다 (받은 값: $RESUME_FROM)"
  [ -f "$WORK/VERIFY.md" ] \
    && [ "$(grep -m1 '^STATUS:' "$WORK/VERIFY.md" | awk '{print $2}')" = "DONE" ] \
    || die "RESUME_FROM=impl 인데 $WORK/VERIFY.md 가 없거나 STATUS: DONE 이 아니다 — 건너뛸 근거가 없다"
fi

if [ "$FRESH_DESIGN" != "1" ] && [ -f "$WORK/DESIGN.md" ] \
   && [ "$(grep -m1 '^STATUS:' "$WORK/DESIGN.md" | awk '{print $2}')" = "DONE" ]; then
  log "↺ 기존 DESIGN.md 재사용 ($(date -r "$WORK/DESIGN.md" '+%m-%d %H:%M') 생성) — 새로 뽑으려면 FRESH_DESIGN=1"
  state "REUSED:design" "기존 산출물 재사용"
  DESIGN_RAN=0
else
  run_stage design "$MODEL_DESIGN" "$FALLBACK_DESIGN" "$PROMPTS/design.md" "$WORK/DESIGN.md"
  DESIGN_RAN=1
fi
# design.md 가 "읽기만, 절대 수정 금지"로 못박지만 프롬프트는 게이트가 아니다.
check_protected design
# 계약 블록의 형식은 judge 를 띄우기 전에 본다 — 형식 위반에 judge 비용을 얹을 이유가 없다.
gate_contract
# 이번 주행이 설계를 돌렸을 때만 — 재사용한 설계의 스트림은 이번 것이 아니다.
[ "$DESIGN_RAN" = "1" ] && check_design_reads
seal_worktree "design 게이트 통과"

# ─────────────────────────────────────────── 판단 검증
# 설계의 '주장'을 별 프로세스가 감사한다. 구현물에는 테스트·게이트가 있는데
# 판단물(원인 판정·우선순위·"X 가 없다")은 아무 검사 없이 구현으로 흘러갔다.
# DESIGN.md 보다 새로우면 재사용한다 — 설계가 새로 돌면 판정도 다시 받아야 한다.
if [ -f "$WORK/JUDGE.md" ] && [ "$WORK/JUDGE.md" -nt "$WORK/DESIGN.md" ] \
   && [ "$(grep -m1 '^STATUS:' "$WORK/JUDGE.md" | awk '{print $2}')" = "DONE" ]; then
  log "↺ 기존 JUDGE.md 재사용 (DESIGN.md 보다 최신)"
  state "REUSED:judge" "기존 산출물 재사용"
else
  check_seal "judge"
  run_stage judge "$MODEL_JUDGE" "$FALLBACK_JUDGE" "$PROMPTS/judge.md" "$WORK/JUDGE.md"
fi

# 판단 검증은 읽기 전용 단계다 — 확인용 임시 파일을 만들었다면 지웠어야 한다.
# 카운트 게이트보다 먼저 본다: judge 가 파일을 건드렸다면 사람이 y 를 누르기 전에 드러나야 한다.
gate_scope judge
check_protected judge
# 이 봉인이 가장 긴 틈을 덮는다 — 아래 사람 게이트(설계 검토·반박)는 exit 4 로 멈췄다가
# 런처가 승인을 중계하고 재실행하는 경로다.
seal_worktree "judge 게이트 통과"

# ★ 판정권은 셸에 있다. 에이전트가 쓴 '판정' 문장을 읽지 않고, 자기가 신고한
#   카운트 한 줄만 파싱한다. 형식이 없으면 그것도 게이트 위반이다.
JUDGE_COUNTS="$(grep -m1 -E '^UNVERIFIED: *[0-9]+ +REFUTED: *[0-9]+' "$WORK/JUDGE.md" || true)"
if [ -z "$JUDGE_COUNTS" ]; then
  die "JUDGE.md 에 'UNVERIFIED: <n> REFUTED: <n>' 라인이 없다 → $WORK/JUDGE.md"
fi
UNVERIFIED="$(sed -E 's/^UNVERIFIED: *([0-9]+).*/\1/' <<<"$JUDGE_COUNTS")"
REFUTED="$(sed -E 's/.*REFUTED: *([0-9]+).*/\1/' <<<"$JUDGE_COUNTS")"
log "판단 검증: 미확인 $UNVERIFIED / 반박 $REFUTED"

if [ "$UNVERIFIED" -gt 0 ] || [ "$REFUTED" -gt 0 ]; then
  state "JUDGE_FLAGGED" "미확인 $UNVERIFIED / 반박 $REFUTED" \
    "$WORK/JUDGE.md 의 반박·미확인 항목을 사람에게 보여주고 판단을 받아라. 승인 없이 구현으로 넘기지 마라."
  gate_human \
    "설계의 주장 중 반박 $REFUTED 건·미확인 $UNVERIFIED 건 — 이대로 구현하면 그 위에 코드가 쌓인다" \
    "$WORK/JUDGE.md" 1
fi

gate_human "설계 검토 — 여기서 틀리면 뒤가 전부 낭비다" "$WORK/DESIGN.md"

# ── 승인된 설계·판정은 여기서부터 읽기 전용이다 ─────────
# impl·verify 가 DESIGN.md 를 덮어쓴 실패 모드(design-notes §6)를 지문으로 막는다.
# 범위 게이트는 .pipeline/ 을 제외하므로 이 경로는 여기서만 잡힌다.
ARTIFACT_GUARD=".pipeline/$FEATURE/DESIGN.md .pipeline/$FEATURE/JUDGE.md"
PROTECTED_BASELINE="$(protected_fingerprint)"

# ── 승인된 범위를 하위 프로세스(그리고 그 훅)에 알린다 ────────────────
# 사용자 전역 PreToolUse 훅(~/.claude/hooks/sensitive-path-guard.py)은 auth·마이그레이션
# 같은 민감 경로의 Edit/Write 에 "ask" 를 건다. `-p` 는 비대화형이라 답할 사람이 없어
# 단계가 산출물 없이 죽는다 — 2026-08-24 impl 이 이렇게 죽었다(마이그레이션 Write 거부).
# 그 훅은 PIPELINE_APPROVED_SCOPE 가 가리키는 파일에 적힌 경로를 통과시킨다. 여기서
# 그 목록을 **사람이 방금 승인한 DESIGN.md 의 ALLOWED_FILES 에서** 뽑는다 — 승인의
# 출처가 설계 문서이므로 방어선이 느슨해지는 것이 아니라 사람 승인에 묶인다.
# 반드시 DESIGN 게이트 **뒤**에 둔다. 앞에 두면 승인 안 된 설계가 범위를 정한다.
# 훅이 없는 환경에서는 변수가 그냥 무시된다 (fail-safe).
extract_allowed_files "$WORK/allowed_files.txt"
export PIPELINE_APPROVED_SCOPE="$WORK/allowed_files.txt"
log "승인 범위 $(wc -l < "$WORK/allowed_files.txt" | tr -d ' ')개 파일을 훅에 전달 (PIPELINE_APPROVED_SCOPE)"

# 단계별 쓰기 권한의 근거도 같은 승인본에서 뽑는다. 소스 = 허용 − 테스트.
extract_test_files "$WORK/test_files.txt"
comm -23 "$WORK/allowed_files.txt" "$WORK/test_files.txt" > "$WORK/source_files.txt"

# ─────────────────────────────────────────── 테스트 선작성 (verify → red)
# verify 는 **구현 전에** 돈다 (2026-09-30). 예전 순서(impl → verify)에서는 테스트가 구현을 보고
# 쓰였고, 셸은 그 테스트가 무엇을 검사하는지 가를 방법이 없었다 — 항상 참인 단언이나 구현을
# 그대로 베낀 기대값도 초록이면 통과였다. 판정권은 셸에 있었지만 판정 **재료**는 에이전트가
# 만들었다. 이제 셸이 재료도 검사한다:
#   기준선  : 테스트를 쓰기 전 검증 명령이 초록이어야 한다 — 그래야 이후 빨강이 새 테스트 탓이다
#   red     : 테스트를 쓴 뒤, 구현 전 코드에서 검증 명령이 **실패해야** 한다
#   green   : 구현 뒤 같은 명령이 통과해야 한다 (아래 재시도 루프)
# 구현 전에도 통과하는 테스트는 바뀔 동작을 검사하지 않거나, 러너가 새 테스트를 안 도는 것이다.
# 설계가 TEST_FILES 를 비워 뒀으면(테스트 없는 변경) 기준선·red 는 해당 없음으로 남긴다.
#
# 한계: 셸은 "왜 실패했는가"를 모른다. 테스트 파일의 문법 오류도 red 다 — 그런 테스트는 impl 이
# 고칠 수 없으므로(TEST_FILES 쓰기 금지) green 에서 막히고 BLOCKED·재시도 소진으로 드러난다.
# 그래서 red 출력(red_out.txt)을 STATE.md 에 남겨 사람이 실패 이유를 볼 수 있게 한다.
red_gate() {
  if [ ! -s "$WORK/test_files.txt" ]; then
    RED_STATE="해당 없음 (설계가 TEST_FILES 를 비워 둠)"
    log "  · red→green 해당 없음 — 설계가 테스트 파일을 선언하지 않았다"
    return 0
  fi
  state "TESTING_RED" "구현 전 코드에서 새 테스트가 실패하는지 확인"
  local rc=0
  run_verify || rc=$?
  cp "$WORK/test_out.txt" "$WORK/red_out.txt"
  if [ "$rc" -eq 0 ]; then
    RED_STATE="실패: 구현 전인데 전부 통과 ($VERIFY_PASSED)"
    fail_log "red 게이트: 구현 전인데 검증 명령이 전부 통과" <<EOF
새 테스트 파일: $(tr '\n' ' ' < "$WORK/test_files.txt")
통과한 명령: $VERIFY_PASSED
EOF
    die "red 게이트: 테스트를 썼는데 구현 전 코드에서 검증 명령이 전부 통과했다 — 새 테스트가 바뀔 동작을 검사하지 않거나 러너가 새 테스트를 돌지 않는다 → $WORK/red_out.txt"
  fi
  case "$VERIFY_FAILED" in
    *"시간 초과"*)
      RED_STATE="확인 불가: $VERIFY_FAILED"
      die "red 게이트: 검증 명령이 시간 상한 안에 돌아오지 않았다 — 실패가 아니라 대기라서 red 로 치지 않는다 ($VERIFY_FAILED) → $WORK/red_out.txt" ;;
  esac
  RED_STATE="확인: 구현 전 $VERIFY_FAILED 실패 (출력: red_out.txt)"
  log "  ✔ red — 구현 전 코드에서 $VERIFY_FAILED 가 실패한다"
}

if [ "$RESUME_FROM" = "impl" ]; then
  # 보호 파일 검사의 사각을 git 으로 메운다. check_protected 는 "이번 실행이 바꿨는가"를
  # 보는데, verify 를 이번에 안 돌리면 이전 실행의 결과가 이미 기준선에 들어가 있다.
  RESUMED_DIRTY="$(cd "$ROOT" && git status --porcelain -- $PROTECTED_FILES 2>/dev/null | awk '{print $2}' | tr '\n' ' ')"
  [ -z "$RESUMED_DIRTY" ] \
    || die "RESUME_FROM=impl — 보호 파일이 커밋 기준으로 변경돼 있다: $RESUMED_DIRTY. 이전 주행이 건드렸는지 확인하고 git checkout 으로 되돌린 뒤 다시 실행해라 (의도한 변경이면 커밋한 뒤 실행)"
  BASELINE_STATE="건너뜀 (RESUME_FROM=impl)"
  RED_STATE="건너뜀 (RESUME_FROM=impl — 이전 주행의 테스트 재사용)"
  log "↺ RESUME_FROM=impl — verify 건너뜀 (기존 VERIFY.md·테스트 재사용, 보호 파일 git 대조 통과)"
  state "REUSED:verify" "RESUME_FROM=impl — 기존 VERIFY.md 재사용"
else
  if [ -s "$WORK/test_files.txt" ]; then
    state "TESTING_BASELINE" "테스트를 쓰기 전 검증 명령 기준선"
    BASELINE_RC=0; run_verify || BASELINE_RC=$?
    cp "$WORK/test_out.txt" "$WORK/baseline_out.txt"
    if [ "$BASELINE_RC" -ne 0 ]; then
      BASELINE_STATE="실패: $VERIFY_FAILED"
      die "기준선이 이미 빨갛다 ($VERIFY_FAILED) — 테스트를 쓰기 전부터 실패하면 red→green 을 새 테스트 탓으로 판정할 수 없다. 환경이거나 워킹트리에 이미 있던 코드다. 고친 뒤 재실행하면 DESIGN.md·JUDGE.md 는 재사용된다 → $WORK/baseline_out.txt"
    fi
    BASELINE_STATE="통과: $VERIFY_PASSED"
    log "  ✔ 기준선 녹색 — 이후 빨강은 새 테스트가 만든 것이다"
    seal_worktree "기준선 실행"
  else
    BASELINE_STATE="해당 없음 (설계가 TEST_FILES 를 비워 둠)"
  fi

  check_seal "verify"
  VERIFY_BASELINE="$(stage_baseline)"
  run_stage verify "$MODEL_VERIFY" "$FALLBACK_VERIFY" "$PROMPTS/verify.md" "$WORK/VERIFY.md"
  gate_scope verify
  # 통과시키려고 러너 설정을 손대는 것이 가장 값싼 부정행위 경로다.
  check_protected verify
  # 테스트 파일 밖은 verify 의 권한이 아니다 — 구현을 미리 써 두면 red 가 사라진다.
  check_stage_writes verify "$VERIFY_BASELINE" "$WORK/source_files.txt" "소스 파일"
  red_gate
  seal_worktree "verify·red 게이트 통과"
fi

# ─────────────────────────────────────────── 구현 → green (재시도 루프)
# 재시도는 impl 만 다시 돈다. 테스트는 red 로 검증된 채 고정된다 — impl 은 TEST_FILES 를 못 쓴다.
# 테스트 자체가 틀렸다고 판단되면 impl 이 BLOCKED 로 올리고 사람이 정한다.
while :; do
  ATTEMPT=$((ATTEMPT + 1))
  log "── 시도 $ATTEMPT/$((MAX_RETRY + 1))"

  check_seal "impl (시도 $ATTEMPT)"
  # 기준선은 impl **직전**에 찍는다 — 재시도 2차는 1차 impl 과 검증 명령이 남긴 것을 안고 시작한다.
  IMPL_BASELINE="$(stage_baseline)"
  run_stage impl   "$MODEL_IMPL"   "$FALLBACK_IMPL"   "$PROMPTS/impl.md"   "$WORK/IMPL.md"
  gate_scope impl
  check_protected impl
  check_stage_writes impl "$IMPL_BASELINE" "$WORK/test_files.txt" "테스트 파일"

  # ★ 최종 판정은 셸이 한다. 에이전트에게 안 맡긴다.
  state "TESTING" "$TEST_CMD"
  VERIFY_RC=0; run_verify || VERIFY_RC=$?
  # 검증 명령이 남긴 것(캐시·리포트)까지 봉인한다 — 안 그러면 재시도 직전 대조가 오탐한다.
  seal_worktree "impl·검증 명령 실행 (시도 $ATTEMPT)"
  if [ "$VERIFY_RC" -eq 0 ]; then
    VERIFY_LAST="통과: $VERIFY_PASSED"
    log "✅ green — 검증 통과 ($VERIFY_PASSED)"
    break
  fi

  VERIFY_LAST="실패: $VERIFY_FAILED (그 앞까지 통과: ${VERIFY_PASSED:-없음})"
  log "❌ 검증 실패 — $VERIFY_FAILED"
  tail -30 "$WORK/test_out.txt" >&2
  state "TEST_FAILED" "attempt $ATTEMPT — $VERIFY_FAILED 실패" \
    "$FAIL_LOG 의 마지막 항목을 읽고 무엇이 실패했는지 사람에게 보고해라. 재시도 여부는 아래 게이트에서 사람이 정한다."

  # 기록이 die 보다 먼저다. 예전엔 순서가 반대라 **마지막 시도의 실패가 FAIL_LOG 에
  # 영영 안 남았다** — 정작 가장 알고 싶은 실패가 그것이다.
  # 첫 줄에 어느 명령이 실패했는지 둔다. 다음 구현 시도가 이걸 읽는데, 출력만 있고
  # 명령 이름이 없으면 무엇을 고쳐야 하는지 추측하게 된다.
  {
    echo "실패한 명령: \`$VERIFY_FAILED\`"
    echo "그 앞까지 통과: ${VERIFY_PASSED:-없음}"
    echo '```'
    tail -60 "$WORK/test_out.txt"
    echo '```'
  } | fail_log "attempt $ATTEMPT"

  [ "$ATTEMPT" -gt "$MAX_RETRY" ] \
    && die "검증 ${MAX_RETRY}회 재시도 후에도 실패 (마지막: $VERIFY_FAILED) → $FAIL_LOG"

  gate_human "재시도 $((ATTEMPT + 1)) 진행? (실패 원인은 FAIL_LOG 마지막 항목)" "$FAIL_LOG"
done

rm -f "$SEAL" "$SEAL.at"   # 완주 — 런처 훅의 "실행 중" 판정도 여기서 풀린다
state "DONE" "통과: $VERIFY_PASSED" \
  "완주다. 산출물($WORK/{DESIGN,JUDGE,IMPL,VERIFY}.md)과 위 '검증 게이트' 블록이 말하는 통과 범위를 사람에게 보고해라."
log "=== $FEATURE 완료 ==="
log "검증 통과: $VERIFY_PASSED"
log "산출물: $WORK/{DESIGN,JUDGE,IMPL,VERIFY}.md"
