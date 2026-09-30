#!/usr/bin/env bash
# orchestrate.sh 게이트 검증 스위트
#
# 검증하는 것: 게이트가 "통과시키는가"가 아니라 "막는가"
# API 호출 0회. fake-claude 를 PATH 앞에 끼워넣는다.
#
# 사용법: ./test/run-tests.sh

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"

PASS=0; FAIL=0
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }

# tty 없는 실행(런처·cron·CI)을 흉내내려면 controlling terminal 을 떼야 한다.
# macOS 에 setsid 가 없어 python3 의 os.setsid 를 쓰는데, Windows 의 python3 에는
# 그 함수 자체가 없다. command -v 만 보면 "있다"고 판단해서 해당 케이스가 통째로
# 거짓 실패한다 — 호출 가능한지까지 확인한다.
have_detach() {
  command -v python3 >/dev/null 2>&1 && python3 -c 'import os; os.setsid' >/dev/null 2>&1
}
detach() { python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"; }

# ── 매 테스트마다 깨끗한 샌드박스 repo 를 만든다
setup() {
  SANDBOX="$(mktemp -d)"
  cd "$SANDBOX"
  git init -q .
  git config user.email t@t; git config user.name t
  mkdir -p prompts test
  cp "$SRC/orchestrate.sh" "$SRC/approve.sh" .
  cp "$SRC/prompts/"*.md prompts/
  cp "$HERE/fake-claude" test/claude       # ← 이름이 'claude' 여야 가로챈다
  cp "$HERE/fake-codex"  test/codex        # ← 진짜 codex 가 PATH 뒤에 있어도 이게 먼저 잡힌다
  chmod +x orchestrate.sh approve.sh test/claude test/codex
  printf '.pipeline/\n' > .gitignore
  echo x > x.txt; git add -A; git commit -qm init
  export PATH="$SANDBOX/test:$PATH"
  # 샌드박스는 worktree 가 아닌 평범한 저장소다. 격리 가드를 켠 채로 두면
  # 나머지 케이스가 전부 "가드에 막혀 exit 2" 로 통과해버린다 —
  # 통과하지만 아무것도 검증하지 않는 상태가 되므로 여기서 끈다.
  export REQUIRE_WORKTREE=0
  # judge 기본값은 codex 엔진이다. 기존 케이스의 judge 시나리오(FAKE_SCENARIO_JUDGE=…)는
  # fake-claude 가 흉내내므로 여기서 claude 체인으로 고정한다. codex 기본값은
  # "codex judge 엔진" 절이 env -u 로 이 고정을 풀고 따로 검증한다.
  export MODEL_JUDGE=claude-opus-5-5 FALLBACK_JUDGE=claude-fable-5-1,claude-sonnet-5-5
  unset FAKE_CODEX_SCENARIO FAKE_CODEX_LOGGED_OUT FAKE_CODEX_RATELIMIT_MODELS CODEX_BIN
}

teardown() { cd /; rm -rf "$SANDBOX"; }

# 테스트 쪽 가짜 검증 명령 (red→green 을 흉내낸다):
#   t.test.txt 에 검증이 쓴 테스트가 없으면 통과 — 기준선·테스트 없는 설계
#   있는데 IMPL.md 가 없으면 실패 — 구현 전 red
#   둘 다 있으면 통과 — 구현 뒤 green
# 설계가 TEST_FILES 를 비워 두면 true 와 같다.
RG='! grep -qs verify t.test.txt || [ -f .pipeline/feat/IMPL.md ]'

# expect <설명> <기대exit코드> -- <env할당들...>
expect() {
  local desc=$1 want=$2; shift 3
  setup
  local got=0
  env "$@" AUTO=1 TEST_CMD="$RG" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then
    green "  PASS  $desc (exit $got)"; PASS=$((PASS+1))
  else
    red   "  FAIL  $desc — 기대 exit $want, 실제 $got"; FAIL=$((FAIL+1))
  fi
  teardown
}

# 설계·판정이 이미 있는 상태를 만든다 (설계 게이트·재사용 케이스용)
seed_design_judge() {   # seed_design_judge [설계 본문]
  mkdir -p .pipeline/feat
  printf 'STATUS: DONE\n\n(%s)\n\nALLOWED_FILES:\n- x.txt\n\nTEST_FILES:\n\n' "${1:-사람이 이미 검토한 설계}" > .pipeline/feat/DESIGN.md
  sleep 1
  printf 'STATUS: DONE\nUNVERIFIED: 0 REFUTED: 0\n' > .pipeline/feat/JUDGE.md
}

echo "=== 정상 경로 ==="
expect "전부 정상이면 0으로 끝난다" 0 -- FAKE_SCENARIO=ok

echo
echo "=== 게이트가 막아야 하는 것들 ==="
expect "STATUS 라인 없으면 죽는다 (설계)"        2 -- FAKE_SCENARIO_DESIGN=no_status
expect "STATUS 라인 없으면 죽는다 (구현)"        2 -- FAKE_SCENARIO_IMPL=no_status
expect "STATUS 라인 없으면 죽는다 (검증)"        2 -- FAKE_SCENARIO_VERIFY=no_status
expect "산출물 파일 없으면 죽는다"               2 -- FAKE_SCENARIO_DESIGN=no_file
expect "에이전트 에러면 죽는다"                  2 -- FAKE_SCENARIO_DESIGN=agent_error
expect "프로세스가 죽으면 죽는다"                2 -- FAKE_SCENARIO_DESIGN=crash
expect "BLOCKED 는 exit 3 (사람 호출)"           3 -- FAKE_SCENARIO_DESIGN=blocked
expect "구현 BLOCKED 도 exit 3"                  3 -- FAKE_SCENARIO_IMPL=blocked
expect "STATUS 라인 없으면 죽는다 (판단검증)"    2 -- FAKE_SCENARIO_JUDGE=no_status
expect "판단검증 카운트 라인 없으면 죽는다"      2 -- FAKE_SCENARIO_JUDGE=judge_nocount
expect "판단검증 BLOCKED 도 exit 3"              3 -- FAKE_SCENARIO_JUDGE=blocked

echo
echo "=== 판단 검증 게이트 ==="
# 이 게이트의 존재 이유: 반박·미확인이 있는 설계가 AUTO=1 로 조용히 구현까지
# 흘러가면 안 된다. 즉 검사할 것은 "통과하는가"가 아니라 "무인이어도 멈추는가".
#
# 게이트는 /dev/tty 에서 읽으므로, 터미널이 있으면 대기하고 없으면 즉시 중단한다.
# 둘 중 어느 쪽이 되든 불변식은 하나다 — **IMPL.md 가 만들어지지 않는다.**
setup
env FAKE_SCENARIO_JUDGE=judge_flagged AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 &
pid=$!
for _ in $(seq 1 60); do
  [ -f .pipeline/feat/JUDGE.md ] && ! kill -0 $pid 2>/dev/null && break
  grep -q 'phase: GATE' .pipeline/feat/STATE.md 2>/dev/null && break
  sleep 0.1
done
kill $pid 2>/dev/null; wait $pid 2>/dev/null
if grep -q '^UNVERIFIED: 2 REFUTED: 1' .pipeline/feat/JUDGE.md 2>/dev/null \
   && [ ! -f .pipeline/feat/IMPL.md ]; then
  green "  PASS  반박이 있으면 AUTO=1 이어도 구현으로 넘어가지 않는다"; PASS=$((PASS+1))
else
  red   "  FAIL  판단 검증 게이트가 무인 모드를 막지 못함 (IMPL.md 생성됨)"; FAIL=$((FAIL+1))
fi
teardown

# tty 없는 환경(런처 모드·cron·CI)에서 게이트가 **의도한 경로로** 멈추는지.
# 마커 없이 게이트에 걸리면 "사람이 아직 검토하지 않음" = exit 4 (승인 대기).
# "사람이 거부함"(exit 2)과 구분되어야 런처가 산출물을 보여주고 재실행할 수 있다.
if have_detach; then
  setup
  got=0
  detach env FAKE_SCENARIO_JUDGE=judge_flagged AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 4 ] && [ ! -f .pipeline/feat/IMPL.md ] \
     && grep -q 'phase: AWAITING_APPROVAL' .pipeline/feat/STATE.md 2>/dev/null; then
    green "  PASS  tty 없는 게이트는 exit 4 승인 대기로 멈춘다"; PASS=$((PASS+1))
  else
    red   "  FAIL  tty 부재 시 종료 경로 — exit=$got (기대 4), phase=$(grep -m1 'phase:' .pipeline/feat/STATE.md 2>/dev/null)"
    FAIL=$((FAIL+1))
  fi
  teardown
else
  red "  SKIP  tty 부재 케이스 — setsid 사용 불가"
fi

echo
echo "=== 승인 마커 (런처 모드) ==="
# 런처 모드의 계약 세 가지를 검사한다:
#   1) 사람이 남긴 마커는 tty 없는 게이트를 통과시킨다
#   2) 승인 후 내용이 바뀐 마커(낡은 마커)는 통과시키지 않는다
#   3) approve.sh 자체가 tty 없이는 마커를 만들지 못한다 (런처 대리 승인 차단)
# 1의 마커는 approve.sh --hash 로 만든다 — orchestrate.sh 의 file_hash 와
# 구현이 어긋나면 이 케이스가 잡는다 (교차 검증).
if have_detach; then
  setup
  seed_design_judge "검토된 설계"
  ./approve.sh --hash .pipeline/feat/DESIGN.md > .pipeline/feat/DESIGN.md.approved
  got=0
  detach env FAKE_SCENARIO=ok AUTO=0 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 0 ] && [ -f .pipeline/feat/IMPL.md ]; then
    green "  PASS  유효한 승인 마커는 tty 없는 게이트를 통과시킨다"; PASS=$((PASS+1))
  else
    red   "  FAIL  마커 통과 실패 — exit=$got (기대 0)"; FAIL=$((FAIL+1))
  fi
  teardown

  setup
  seed_design_judge "검토된 설계"
  echo "stale-hash-of-previously-approved-content" > .pipeline/feat/DESIGN.md.approved
  got=0
  detach env FAKE_SCENARIO=ok AUTO=0 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 4 ] && [ ! -f .pipeline/feat/IMPL.md ] \
     && grep -q 'approve.sh feat DESIGN.md' .pipeline/feat/STATE.md 2>/dev/null; then
    green "  PASS  낡은 마커는 통과시키지 않고 STATE 에 승인 안내를 남긴다"; PASS=$((PASS+1))
  else
    red   "  FAIL  낡은 마커 — exit=$got (기대 4)$([ -f .pipeline/feat/IMPL.md ] && echo ', IMPL.md 생성됨')"; FAIL=$((FAIL+1))
  fi
  teardown

  setup
  mkdir -p .pipeline/feat
  printf 'STATUS: DONE\n' > .pipeline/feat/DESIGN.md
  got=0
  detach ./approve.sh feat DESIGN.md >/dev/null 2>&1 || got=$?
  if [ "$got" -ne 0 ] && [ ! -f .pipeline/feat/DESIGN.md.approved ]; then
    green "  PASS  approve.sh 는 tty 없이(중계 없이) 마커를 만들지 않는다"; PASS=$((PASS+1))
  else
    red   "  FAIL  tty 없는 approve — exit=$got$([ -f .pipeline/feat/DESIGN.md.approved ] && echo ', 마커 생성됨')"; FAIL=$((FAIL+1))
  fi
  teardown

  # ── y 중계 (2026-09-04 사용자 결정): 런처는 사람의 답을 그대로 넘긴다. y 만 승인이다.
  setup
  mkdir -p .pipeline/feat
  printf 'STATUS: DONE\n' > .pipeline/feat/DESIGN.md
  got=0
  detach ./approve.sh feat DESIGN.md --relayed y >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 0 ] \
     && [ "$(cat .pipeline/feat/DESIGN.md.approved)" = "$(./approve.sh --hash .pipeline/feat/DESIGN.md)" ] \
     && grep -q '런처 중계' .pipeline/feat/APPROVALS.md 2>/dev/null; then
    green "  PASS  --relayed y 는 tty 없이 마커를 만들고 감사 기록을 남긴다"; PASS=$((PASS+1))
  else
    red   "  FAIL  y 중계 — exit=$got, 마커=$([ -f .pipeline/feat/DESIGN.md.approved ] && echo O || echo X)"; FAIL=$((FAIL+1))
  fi
  teardown

  setup
  mkdir -p .pipeline/feat
  printf 'STATUS: DONE\n' > .pipeline/feat/DESIGN.md
  bad=0
  for ans in n "" "괜찮으면 해" yes; do
    got=0
    detach ./approve.sh feat DESIGN.md --relayed "$ans" >/dev/null 2>&1 || got=$?
    { [ "$got" -eq 0 ] || [ -f .pipeline/feat/DESIGN.md.approved ]; } && { bad=1; echo "         통과시킨 답: '$ans' (exit $got)"; }
  done
  if [ "$bad" -eq 0 ]; then
    green "  PASS  y 가 아닌 중계 답(n·빈 값·문장·yes)은 마커를 만들지 않는다"; PASS=$((PASS+1))
  else
    red   "  FAIL  중계가 y 이외의 답을 승인으로 받았다"; FAIL=$((FAIL+1))
  fi
  teardown

  # 중계 마커가 실제 게이트를 통과시키고, STATE 의 다음 행동이 중계 계약을 담는지.
  setup
  seed_design_judge "중계 승인 대상 설계"
  got=0
  detach env FAKE_SCENARIO=ok AUTO=0 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  first=$got
  has_protocol=0
  grep -q -- '--relayed y' .pipeline/feat/STATE.md 2>/dev/null \
    && grep -q 'AskUserQuestion' .pipeline/feat/STATE.md 2>/dev/null \
    && grep -q '요약·추천·의견 금지' .pipeline/feat/STATE.md 2>/dev/null && has_protocol=1
  detach ./approve.sh feat DESIGN.md --relayed y >/dev/null 2>&1 || true
  got=0
  detach env FAKE_SCENARIO=ok AUTO=0 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$first" -eq 4 ] && [ "$has_protocol" -eq 1 ] && [ "$got" -eq 0 ] && [ -f .pipeline/feat/IMPL.md ]; then
    green "  PASS  exit 4 의 다음 행동이 y 중계 계약이고, 중계 마커로 게이트를 통과한다"; PASS=$((PASS+1))
  else
    red   "  FAIL  y 중계 게이트 — 1차=$first (기대 4), 계약=$has_protocol, 2차=$got (기대 0)"; FAIL=$((FAIL+1))
  fi
  teardown
else
  red "  SKIP  승인 마커 케이스 — setsid 사용 불가"
fi

# 대조군 — 게이트가 '항상 막는' 게 아니라는 것. 이게 없으면 위 PASS 는 무의미하다.
setup
got=0
env FAKE_SCENARIO_JUDGE=judge_clean AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && [ -f .pipeline/feat/IMPL.md ]; then
  green "  PASS  미확인·반박 0 이면 그대로 진행한다 (대조군)"; PASS=$((PASS+1))
else
  red   "  FAIL  깨끗한 판정인데 진행이 막혔다 — exit=$got"; FAIL=$((FAIL+1))
fi
teardown

# 설계가 새로 돌면 판정도 다시 받아야 한다 (JUDGE.md 가 DESIGN.md 보다 오래됐으면 재실행)
setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\nUNVERIFIED: 0 REFUTED: 0\n\n(지난 판정)\n' > .pipeline/feat/JUDGE.md
sleep 1
printf 'STATUS: DONE\n\n(새 설계)\n\nALLOWED_FILES:\n- x.txt\n\nTEST_FILES:\n\n' > .pipeline/feat/DESIGN.md
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ -f .pipeline/feat/judge.result.json ]; then
  green "  PASS  설계가 판정보다 새로우면 판단 검증을 다시 돌린다"; PASS=$((PASS+1))
else
  red   "  FAIL  낡은 JUDGE.md 를 그대로 재사용했다"; FAIL=$((FAIL+1))
fi
teardown

setup
seed_design_judge
printf 'STATUS: DONE\nUNVERIFIED: 0 REFUTED: 0\n\n(이미 받은 판정)\n' > .pipeline/feat/JUDGE.md
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ ! -f .pipeline/feat/judge.result.json ] \
   && grep -q '이미 받은 판정' .pipeline/feat/JUDGE.md; then
  green "  PASS  판정이 설계보다 새로우면 재사용한다"; PASS=$((PASS+1))
else
  red   "  FAIL  판단 검증 재사용 실패"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 프롬프트 치환 ==="
# orchestrate.sh 가 export 하지 않은 변수를 프롬프트가 참조하면 envsubst 가 빈
# 문자열로 치환한다. 에이전트는 "셸이 `` 를 실행한다" 같은 깨진 문장을 받는데,
# 파이프라인은 정상 동작하므로 아무도 모른다. 실제로 $TEST_CMD 가 그랬다.
setup
rendered_ok=1
for f in prompts/design.md prompts/judge.md prompts/impl.md prompts/verify.md; do
  out=$(FEATURE=feat WORK=/w ROOT=/r TEST_CMD="npm test" envsubst < "$f")
  # 정확히 백틱 2개(앞뒤가 백틱이 아닌) = 빈 인라인 코드. ``` 펜스는 제외된다.
  printf '%s' "$out" | grep -qE '(^|[^`])``([^`]|$)' && { rendered_ok=0; echo "         빈 치환: $f"; }
  # 살아남은 $VAR = export 목록에 없는 변수
  printf '%s' "$out" | grep -qE '\$[A-Z_][A-Z_0-9]*' && { rendered_ok=0; echo "         미치환: $f"; }
done
if [ "$rendered_ok" -eq 1 ]; then
  green "  PASS  모든 프롬프트가 빈 치환 없이 렌더된다"; PASS=$((PASS+1))
else
  red   "  FAIL  프롬프트 치환이 깨짐 (orchestrate.sh 의 export 목록 확인)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 재시도 루프 ==="
# 테스트가 항상 실패하면 MAX_RETRY 만큼 돌고 죽어야 한다.
# 기록은 시도 횟수와 같다 — 마지막 시도도 die 전에 FAIL_LOG 에 남는다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 MAX_RETRY=2 TEST_CMD="false" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
attempts=$(grep -c '^## attempt' .pipeline/feat/FAIL_LOG.md 2>/dev/null | head -1)
attempts=${attempts:-0}
if [ "$got" -eq 2 ] && [ "$attempts" -eq 3 ]; then
  green "  PASS  테스트 계속 실패 → 3회 전부 기록 후 포기 (exit 2)"; PASS=$((PASS+1))
else
  red   "  FAIL  재시도 루프 — exit=$got, FAIL_LOG 기록=$attempts (기대: 2, 3)"; FAIL=$((FAIL+1))
fi
teardown

# 어느 명령이 실패했는지가 FAIL_LOG 첫 줄에 보여야 한다.
# 출력만 있고 명령 이름이 없으면 다음 구현 시도가 무엇을 고칠지 추측하게 된다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 MAX_RETRY=0 TEST_CMD="false" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && grep -q '실패한 명령: `false`' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  실패한 명령 이름이 FAIL_LOG 에 남는다"; PASS=$((PASS+1))
else
  red   "  FAIL  FAIL_LOG 에 실패 명령이 없음 — exit=$got"
  sed -n '1,6p' .pipeline/feat/FAIL_LOG.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 검증 명령 선언 강제 ==="
# 기본값 npm test 를 두면 각색을 안 한 저장소가 조용히 그걸로 돈다 (2026-08-28 실측 ~$39).
# "기본값 그대로면 경고"로는 못 잡는다 — 진짜 npm 저장소에서는 그게 정답이라
# 각색 누락과 각색 성공이 구분되지 않는다. 그래서 선언을 강제한다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="" TEST_CMDS="" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/DESIGN.md ] \
   && grep -q '검증 명령이 선언되지 않았다' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  TEST_CMD·TEST_CMDS 가 둘 다 비면 에이전트를 띄우기 전에 죽는다 (비용 \$0)"; PASS=$((PASS+1))
else
  red   "  FAIL  검증 명령 미선언이 통과됨 — exit=$got (기대 2)"
  grep -n 'DIED\|검증 명령' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 검증 명령 목록 (TEST_CMDS) ==="
# 여러 검사를 && 로 이으면 run_verify 가 명령 하나로 보고, 어느 검사가 깨졌는지
# FAIL_LOG 에 안 남는다. 줄 단위 목록이 그걸 푼다.

# 대조군: 목록이 전부 통과하면 완주하고, STATE.md 가 무엇을 돌렸는지 전부 보여준다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="이건-쓰이면-안-된다" \
  TEST_CMDS="true
  true
" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && grep -q '검증 명령: true, true' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  목록의 명령을 전부 돌리고 STATE.md 에 전부 적는다 (대조군) (exit 0)"; PASS=$((PASS+1))
else
  red   "  FAIL  TEST_CMDS 대조군 — exit=$got"
  grep '검증 명령' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

# 핵심 이득: 중간 명령이 깨지면 그 명령 **하나**의 이름이 FAIL_LOG 에 남는다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 MAX_RETRY=0 TEST_CMD="true" \
  TEST_CMDS="true
false
true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && grep -q '실패한 명령: `false`' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  목록 중 깨진 명령의 이름만 FAIL_LOG 에 남는다"; PASS=$((PASS+1))
else
  red   "  FAIL  TEST_CMDS 실패 지목 — exit=$got"
  sed -n '1,6p' .pipeline/feat/FAIL_LOG.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

# 하위호환: TEST_CMDS 가 비면 예전처럼 TEST_CMD 하나를 쓴다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMDS="" TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && grep -q '검증 명령: true' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  TEST_CMDS 가 비면 TEST_CMD 로 돌아간다 (exit 0)"; PASS=$((PASS+1))
else
  red   "  FAIL  하위호환 — exit=$got"; FAIL=$((FAIL+1))
fi
teardown

# 빈 목록을 통과로 취급하면 게이트가 사라진 걸 아무도 모른다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 MAX_RETRY=0 TEST_CMD="true" \
  TEST_CMDS="

   " ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ]; then
  green "  PASS  검증 명령이 공백뿐이면 통과가 아니라 죽는다 (exit 2)"; PASS=$((PASS+1))
else
  red   "  FAIL  빈 검증 목록이 통과로 취급됨 — exit=$got (기대 2)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 검증 명령 시간 상한 ==="
# 안 돌아오는 테스트가 파이프라인을 조용히 매달아 두면 안 된다 (2026-08-27 실측).
# 상한 초과는 "실패"와 다른 사건이므로 FAIL_LOG 에 그렇게 적혀야 한다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 MAX_RETRY=0 VERIFY_TIMEOUT=1 TEST_CMD="sleep 20" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && grep -q '시간 초과' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  검증 명령이 상한을 넘기면 시간 초과로 기록하고 실패 처리한다"; PASS=$((PASS+1))
else
  red   "  FAIL  시간 상한 — exit=$got (기대 2)"; sed -n '1,4p' .pipeline/feat/FAIL_LOG.md 2>/dev/null | sed 's/^/         /'; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== smoke.sh 훅 ==="
setup
mkdir -p .pipeline/feat
printf '#!/usr/bin/env bash\nexit 0\n' > .pipeline/feat/smoke.sh
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if grep -q 'smoke.sh' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q '마지막 결과(green): 통과: true, bash' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  기능 폴더의 smoke.sh 가 검증 목록에 붙고 STATE 에 통과 범위가 남는다"; PASS=$((PASS+1))
else
  red   "  FAIL  smoke.sh 훅이 안 붙음"; sed -n '/검증 게이트/,/산출물/p' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'; FAIL=$((FAIL+1))
fi
teardown

setup
mkdir -p .pipeline/feat
printf '#!/usr/bin/env bash\nexit 1\n' > .pipeline/feat/smoke.sh
got=0
env FAKE_SCENARIO=ok AUTO=1 MAX_RETRY=0 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && grep -q '실패한 명령: `bash' .pipeline/feat/FAIL_LOG.md 2>/dev/null \
   && grep -q '그 앞까지 통과: true' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  smoke.sh 실패는 검증 실패이고 그 앞의 통과가 기록된다"; PASS=$((PASS+1))
else
  red   "  FAIL  smoke.sh 실패 처리 — exit=$got"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 프리플라이트 ==="
# 환경 기준선이 깨져 있으면 에이전트를 한 번도 안 띄우고 죽는다 (비용 $0).
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" PREFLIGHT_CMD="false" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/DESIGN.md ] \
   && grep -q '프리플라이트 실패' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  프리플라이트가 깨지면 에이전트를 띄우기 전에 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  프리플라이트 — exit=$got (기대 2), DESIGN.md=$([ -f .pipeline/feat/DESIGN.md ] && echo 생성됨 || echo 없음)"; FAIL=$((FAIL+1))
fi
teardown

setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" PREFLIGHT_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && grep -q '프리플라이트: 통과: true' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  프리플라이트 통과는 STATE 에 남고 정상 진행한다 (대조군)"; PASS=$((PASS+1))
else
  red   "  FAIL  프리플라이트 대조군 — exit=$got"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 설계 재사용 ==="
# 이미 DONE 인 DESIGN.md 가 있으면 설계 단계를 아예 호출하지 않아야 한다
setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\n\n(사람이 이미 검토한 설계)\n\nALLOWED_FILES:\n- x.txt\n\nTEST_FILES:\n\n' > .pipeline/feat/DESIGN.md
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ ! -f .pipeline/feat/design.result.json ] \
   && grep -q '사람이 이미 검토한 설계' .pipeline/feat/DESIGN.md \
   && [ -f .pipeline/feat/IMPL.md ]; then
  green "  PASS  기존 DESIGN.md 는 재사용되고 덮어쓰이지 않는다"; PASS=$((PASS+1))
else
  red   "  FAIL  설계 재사용 실패 — design 단계가 다시 돌았거나 산출물이 덮어써짐"; FAIL=$((FAIL+1))
fi
teardown

setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\n\n(사람이 이미 검토한 설계)\n\nALLOWED_FILES:\n- x.txt\n\nTEST_FILES:\n\n' > .pipeline/feat/DESIGN.md
env FAKE_SCENARIO=ok AUTO=1 FRESH_DESIGN=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ -f .pipeline/feat/design.result.json ]; then
  green "  PASS  FRESH_DESIGN=1 이면 설계를 다시 뽑는다"; PASS=$((PASS+1))
else
  red   "  FAIL  FRESH_DESIGN=1 인데 설계 단계가 안 돌았다"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== RESUME_FROM=impl ==="
# 근거(STATUS: DONE 인 VERIFY.md)가 없으면 건너뛰지 않고 죽는다. 옛 값·오타도 죽는다.
expect "RESUME_FROM=impl 인데 VERIFY.md 가 없으면 죽는다" 2 -- FAKE_SCENARIO=ok RESUME_FROM=impl
expect "옛 RESUME_FROM=verify 는 impl 로 안내하고 죽는다"  2 -- FAKE_SCENARIO=ok RESUME_FROM=verify
expect "RESUME_FROM 오타는 죽는다"                          2 -- FAKE_SCENARIO=ok RESUME_FROM=impll

setup
seed_design_judge
printf 'STATUS: DONE\n\n(이전 주행의 테스트 요약)\n' > .pipeline/feat/VERIFY.md
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="$RG" RESUME_FROM=impl ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && [ ! -f .pipeline/feat/verify.result.json ] \
   && [ -f .pipeline/feat/impl.result.json ] \
   && grep -q '이전 주행의 테스트 요약' .pipeline/feat/VERIFY.md \
   && grep -q 'RESUME_FROM=impl' .pipeline/feat/STATE.md; then
  green "  PASS  RESUME_FROM=impl 은 verify 를 건너뛰고 impl 부터 돈다"; PASS=$((PASS+1))
else
  red   "  FAIL  RESUME_FROM — exit=$got, verify.result=$([ -f .pipeline/feat/verify.result.json ] && echo 있음 || echo 없음)"; FAIL=$((FAIL+1))
fi
teardown

# 건너뛴 verify 가 보호 파일을 건드려 놓았으면 지문 기준선에 흡수돼 안 보인다 — git 으로 메운다.
setup
seed_design_judge
printf 'STATUS: DONE\n' > .pipeline/feat/VERIFY.md
echo "dist/" >> .gitignore
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="$RG" RESUME_FROM=impl ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/impl.result.json ]; then
  green "  PASS  RESUME 시 보호 파일이 커밋 기준으로 더럽혀져 있으면 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  RESUME 보호 파일 대조 — exit=$got (기대 2)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 진행 스트림 ==="
# tee 가 원본 스트림을 보존해야 result 추출이 가능하다.
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if grep -q '"type":"assistant"' .pipeline/feat/design.stream.jsonl 2>/dev/null \
   && [ "$(jq -r '.is_error' .pipeline/feat/design.result.json 2>/dev/null)" = "false" ]; then
  green "  PASS  스트림이 보존되고 마지막 result 만 추출된다"; PASS=$((PASS+1))
else
  red   "  FAIL  스트림 보존/추출 실패"; FAIL=$((FAIL+1))
fi
teardown

# NDJSON 사이에 JSON 아닌 줄이 섞여도 죽지 않는다 (2026-08-26 MCP 경고 실측).
expect "스트림에 JSON 아닌 줄이 섞여도 완주한다" 0 -- FAKE_SCENARIO_JUDGE=junk_stream

echo
echo "=== 모델 교체 감시 ==="
setup
env FAKE_SCENARIO=model_swap AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1
if grep -q '요청 claude-opus-5-5 → 실제 claude-opus-4-8' .pipeline/feat/MODEL_LOG.md 2>/dev/null; then
  green "  PASS  다른 모델이 돌면 MODEL_LOG 에 기록된다"; PASS=$((PASS+1))
else
  red   "  FAIL  모델 교체가 기록되지 않음"
  echo "         MODEL_LOG 내용:"; sed 's/^/         /' .pipeline/feat/MODEL_LOG.md 2>/dev/null
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 레이트 리밋 순환 ==="
# --fallback-model 은 창 소진 거부를 안 받는다 (2026-08-26 실측). 셸이 감지해 갈아탄다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" FAKE_RATELIMIT_MODELS="claude-opus-5-5" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && [ -f .pipeline/feat/design.ratelimit1.stream.jsonl ] \
   && grep -q 'model=claude-fable-5-1' .pipeline/feat/DESIGN.args 2>/dev/null \
   && grep -q '레이트 리밋 거부' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  리밋 거부면 다음 모델로 갈아타고 증거를 남긴다"; PASS=$((PASS+1))
else
  red   "  FAIL  레이트 리밋 순환 — exit=$got (기대 0)"; ls -1 .pipeline/feat 2>/dev/null | sed 's/^/         /'; FAIL=$((FAIL+1))
fi
teardown

setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" \
  FAKE_RATELIMIT_MODELS="claude-fable-5-1 claude-opus-5-5 claude-sonnet-5-5" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ -f .pipeline/feat/design.ratelimit2.stream.jsonl ] \
   && grep -q 'design 단계 프로세스 사망' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  체인을 다 소진하면 사인을 남기고 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  체인 소진 — exit=$got (기대 2)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 런처 상태 창구 ==="
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if grep -q 'phase: DONE' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q '## 다음 행동' .pipeline/feat/STATE.md \
   && grep -q '완주' .pipeline/feat/STATE.md \
   && grep -q '마지막 결과(green): 통과: true' .pipeline/feat/STATE.md; then
  green "  PASS  STATE.md 가 최종 상태·다음 행동·검증 증거를 반영한다"; PASS=$((PASS+1))
else
  red   "  FAIL  STATE.md 미갱신 또는 다음 행동/검증 게이트 블록 없음"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== worktree 격리 강제 ==="
# 확인할 것은 두 방향이다 — 메인에서 막는가, 그리고 worktree 에서는 통과시키는가.
expect "메인 체크아웃이면 시작 자체를 거부한다" 2 -- FAKE_SCENARIO=ok REQUIRE_WORKTREE=1

setup
WT="$SANDBOX-wt"
git worktree add -b pipeline/feat "$WT" HEAD >/dev/null 2>&1
got=0
(cd "$WT" && env FAKE_SCENARIO=ok AUTO=1 REQUIRE_WORKTREE=1 TEST_CMD="true" \
  ./orchestrate.sh feat) >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && [ -f "$WT/.pipeline/feat/IMPL.md" ]; then
  green "  PASS  worktree 안에서는 가드를 켜도 완주한다"; PASS=$((PASS+1))
else
  red   "  FAIL  worktree 판별 실패 — exit=$got (기대 0)"; FAIL=$((FAIL+1))
fi
git worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"
teardown

echo
echo "=== 단계별 상한·CLI 전달 ==="
# 모델과 턴/예산이 단계마다 다르게 전달되는지. 여기가 어긋나면
# "구현만 중간 티어" 같은 티어링 결정이 조용히 무효가 된다. 예산 기본값은 없음이다.
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
d="$(head -1 .pipeline/feat/DESIGN.args 2>/dev/null)"
j="$(head -1 .pipeline/feat/JUDGE.args  2>/dev/null)"
i="$(head -1 .pipeline/feat/IMPL.args   2>/dev/null)"
v="$(head -1 .pipeline/feat/VERIFY.args 2>/dev/null)"
if [ "$d" = "model=claude-opus-5-5 turns=60 budget=없음" ] \
   && [ "$j" = "model=claude-opus-5-5 turns=80 budget=없음" ] \
   && [ "$i" = "model=claude-sonnet-5-5 turns=80 budget=없음" ] \
   && [ "$v" = "model=claude-opus-5-5 turns=80 budget=없음" ]; then
  green "  PASS  단계별 모델·턴이 각각 전달되고 예산 상한은 기본 없음이다"; PASS=$((PASS+1))
else
  red   "  FAIL  상한 전달 어긋남"
  printf '         design: %s\n         judge : %s\n         impl  : %s\n         verify: %s\n' "$d" "$j" "$i" "$v"
  FAIL=$((FAIL+1))
fi
teardown

setup
env FAKE_SCENARIO=ok AUTO=1 TURNS_IMPL=7 BUDGET_IMPL=2 ADD_DIRS="/tmp /opt" TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1
if grep -q 'turns=7 budget=2' .pipeline/feat/IMPL.args 2>/dev/null \
   && grep -q 'add_dir=/tmp /opt' .pipeline/feat/IMPL.args 2>/dev/null \
   && grep -q 'tools=Bash(git status:\*)' .pipeline/feat/IMPL.args 2>/dev/null; then
  green "  PASS  TURNS/BUDGET/ADD_DIRS/AGENT_TOOLS 가 CLI 로 전달된다"; PASS=$((PASS+1))
else
  red   "  FAIL  CLI 전달 어긋남 — $(cat .pipeline/feat/IMPL.args 2>/dev/null | tr '\n' ' ')"
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 범위 이탈 게이트 ==="
# 확인할 것은 두 방향이다 — 이탈을 잡는가, 그리고 계약 안이면 통과시키는가.
expect "구현이 계약에 없는 파일을 만들면 죽는다" 2 -- FAKE_SCENARIO=scope_creep
expect "계약 안에서만 움직이면 완주한다"         0 -- FAKE_SCENARIO=ok

# git 의 출력 형식 두 가지가 게이트를 거짓 양성으로 만들던 자리다. 둘 다
# "계약을 지킨 구현이 죽는다" 방향이라 이탈을 놓치는 것보다 눈에 늦게 띈다.
#   rename : 'R  old -> new' 한 줄 → 잘라내면 'old -> new' 라는 없는 경로
#   한글   : C-quote 되어 계약의 원문과 영영 매치되지 않음
# 둘 다 FAKE_ALLOWED 에 정답을 넣고 부르므로 기대값은 0(완주)이다.
#
# 단계를 FAKE_SCENARIO_IMPL 로 지정하는 게 중요하다. 전역 FAKE_SCENARIO 로 주면
# judge 단계까지 그 시나리오로 돌아 카운트 라인 없는 JUDGE.md 를 쓰고, 범위 게이트에
# 닿기도 전에 죽는다. exit 2 를 기대하는 케이스에서는 그래도 "통과"라 거짓 초록이 된다.
expect "파일을 옮겨도 양쪽이 계약에 있으면 완주한다" 0 -- \
  FAKE_SCENARIO_IMPL=impl_rename FAKE_ALLOWED="x.txt moved.txt"
expect "한글 경로도 계약과 대조된다"                 0 -- \
  FAKE_SCENARIO_IMPL=impl_hangul FAKE_ALLOWED="x.txt 새폴더/악보.txt"

# 대조군 — 위 둘이 "항상 통과"로 무력화되지 않았는지 본다.
# 옮긴 자리가 계약에 없으면 여전히 죽어야 한다.
expect "옮긴 파일이 계약에 없으면 죽는다"            2 -- \
  FAKE_SCENARIO_IMPL=impl_rename FAKE_ALLOWED="x.txt"

# 계약 블록 자체가 없는 옛 설계를 재사용하면 죽어야 한다.
# 파싱 결과가 비었을 때 "이탈 0개"로 읽고 통과시키는 게 이런 게이트의 단골 버그다.
setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\n\n(ALLOWED_FILES 블록이 없는 옛 설계)\n' > .pipeline/feat/DESIGN.md
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ]; then
  green "  PASS  계약 블록 없는 설계는 통과시키지 않는다"; PASS=$((PASS+1))
else
  red   "  FAIL  계약이 없는데 완주함 — exit=$got (기대 2)"; FAIL=$((FAIL+1))
fi
teardown

# 승인된 범위가 훅으로 전달된다 (PIPELINE_APPROVED_SCOPE → allowed_files.txt)
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" FAKE_ALLOWED="x.txt src/auth/login.ts" ./orchestrate.sh feat >/dev/null 2>&1
if grep -qx 'src/auth/login.ts' .pipeline/feat/allowed_files.txt 2>/dev/null; then
  green "  PASS  승인된 ALLOWED_FILES 가 훅용 목록 파일로 나온다"; PASS=$((PASS+1))
else
  red   "  FAIL  allowed_files.txt 없음 또는 내용 불일치"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 보호 파일 게이트 ==="
# 보호 파일은 설계가 허용해도 건드리면 죽는다 (의존성·러너 설정은 사람 승인 사항).
# 범위 게이트를 지나 보호 게이트에서 잡히도록 FAKE_ALLOWED 에 .gitignore 를 넣는다.
setup
got=0
env FAKE_SCENARIO_IMPL=impl_protected FAKE_ALLOWED="x.txt .gitignore" AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/test_out.txt ] \
   && grep -q '보호 파일을 수정함: .gitignore' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  구현이 보호 파일을 건드리면 검증 명령 실행 전에 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  보호 파일 게이트(impl) — exit=$got (기대 2), test_out=$([ -f .pipeline/feat/test_out.txt ] && echo 있음 || echo 없음)"
  grep -m1 'note:' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

setup
got=0
env FAKE_SCENARIO_VERIFY=verify_protected FAKE_ALLOWED="x.txt .gitignore" AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/test_out.txt ]; then
  green "  PASS  검증이 보호 파일을 고치면 테스트 실행 전에 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  보호 파일 게이트(verify) — exit=$got (기대 2)"; FAIL=$((FAIL+1))
fi
teardown

# 승인된 DESIGN.md 를 구현이 덮어쓰면 죽는다 — 범위 게이트는 .pipeline/ 을 제외하므로
# 지문으로만 잡힌다 (design-notes §6 의 실패 모드).
setup
got=0
env FAKE_SCENARIO_IMPL=design_overwrite AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/test_out.txt ] \
   && grep -q 'DESIGN.md' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q '보호 파일을 수정함' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  구현이 승인된 DESIGN.md 를 바꾸면 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  산출물 지문 게이트 — exit=$got (기대 2)"; grep -m1 'note:' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'; FAIL=$((FAIL+1))
fi
teardown

# 대조군 — 사람이 PROTECTED_FILES 에서 뺀 파일은 잡지 않는다 (의도한 변경의 탈출구).
setup
got=0
env FAKE_SCENARIO_IMPL=impl_protected FAKE_ALLOWED="x.txt .gitignore" PROTECTED_FILES="package.json" \
  AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ]; then
  green "  PASS  PROTECTED_FILES 에서 뺀 파일은 게이트가 잡지 않는다 (대조군)"; PASS=$((PASS+1))
else
  red   "  FAIL  보호 파일 오버라이드 — exit=$got (기대 0)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 죽은 단계 부검 ==="
# 2026-08-24 하루에 세 번 죽었는데 세 번 다 로그에 사인이 한 줄도 안 남았다.
# 사람이 매번 *.stream.jsonl 을 jq 로 파서 원인을 알아냈다. 그게 이 절이 막는 것이다.

# ① 사인이 없어도 "없다"를 정직하게 남긴다 (침묵 금지)
setup
got=0
env FAKE_SCENARIO_DESIGN=crash_no_result AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && grep -q '사인 확인 불가' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  result 이벤트가 없으면 그 사실을 FAIL_LOG 에 남기고 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  사인 부재가 기록되지 않음 — exit=$got"
  sed 's/^/         /' .pipeline/feat/FAIL_LOG.md 2>/dev/null | head -6
  FAIL=$((FAIL+1))
fi
teardown

if have_detach; then
  # ② 산출물이 온전해도 자동 통과는 없다. 사인은 FAIL_LOG 로, 산출물은 파킹으로.
  #    AUTO=1 인데도 멈추는 것이 핵심이다 (gate_human force=1).
  setup
  got=0
  detach env FAKE_SCENARIO_DESIGN=crash_with_artifact AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 4 ] \
     && grep -q 'error_max_turns' .pipeline/feat/FAIL_LOG.md 2>/dev/null \
     && grep -q 'turns_exhausted' .pipeline/feat/FAIL_LOG.md 2>/dev/null \
     && [ -f .pipeline/feat/DESIGN.md.crashed ] && [ ! -f .pipeline/feat/DESIGN.md ] \
     && [ ! -f .pipeline/feat/IMPL.md ]; then
    green "  PASS  죽었는데 산출물이 온전하면 AUTO=1 이어도 멈추고 사인·파킹을 남긴다"; PASS=$((PASS+1))
  else
    red   "  FAIL  부검 게이트 — exit=$got (기대 4), 파킹=$([ -f .pipeline/feat/DESIGN.md.crashed ] && echo O || echo X)"
    sed 's/^/         /' .pipeline/feat/FAIL_LOG.md 2>/dev/null | head -6
    FAIL=$((FAIL+1))
  fi
  # ③ 그 정지 지점에도 런처용 안내가 붙어야 한다
  if grep -q 'phase: AWAITING_APPROVAL' .pipeline/feat/STATE.md 2>/dev/null \
     && grep -q 'mv ' .pipeline/feat/STATE.md 2>/dev/null; then
    green "  PASS  exit 4 정지 지점에 살리는 방법(mv)이 STATE.md 에 찍힌다"; PASS=$((PASS+1))
  else
    red   "  FAIL  파킹 승인 안내가 STATE.md 에 없음"; FAIL=$((FAIL+1))
  fi

  # ④ 미승인 산출물이 재사용 로직에 걸리면 안 된다.
  env FAKE_SCENARIO=ok AUTO=1 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1
  if [ -f .pipeline/feat/design.result.json ] \
     && [ -f .pipeline/feat/DESIGN.md.crashed ] \
     && ! grep -q 'crash_with_artifact' .pipeline/feat/DESIGN.md 2>/dev/null; then
    green "  PASS  파킹된 산출물은 다음 실행의 재사용 로직에 안 걸린다"; PASS=$((PASS+1))
  else
    red   "  FAIL  미승인 산출물이 게이트 없이 되살아났다"; FAIL=$((FAIL+1))
  fi
  teardown

  # ⑤ 이전 주행이 남긴 산출물을 "이번 주행 것"으로 오인하면 안 된다.
  setup
  env FAKE_SCENARIO=ok AUTO=1 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1
  got=0
  detach env FRESH_DESIGN=1 FAKE_SCENARIO_DESIGN=crash_no_result AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 2 ] && [ ! -e .pipeline/feat/DESIGN.md.crashed ] \
     && grep -q '이전 주행 것' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
    green "  PASS  이번 주행이 안 쓴 산출물은 살리지 않는다 (die, 파킹 없음)"; PASS=$((PASS+1))
  else
    red   "  FAIL  이전 주행 산출물이 승격됨 — exit=$got (기대 2)"; FAIL=$((FAIL+1))
  fi
  teardown

  # ⑥ 파킹본도 덮어쓰지 않는다.
  setup
  detach env FAKE_SCENARIO_DESIGN=crash_with_artifact AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || true
  detach env FAKE_SCENARIO_DESIGN=crash_with_artifact AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || true
  if [ -f .pipeline/feat/DESIGN.md.crashed ] && [ -f .pipeline/feat/DESIGN.md.crashed2 ]; then
    green "  PASS  두 번째 파킹본이 첫 번째를 덮지 않는다"; PASS=$((PASS+1))
  else
    red   "  FAIL  파킹본이 덮어써짐"; ls -1 .pipeline/feat/ | sed 's/^/         /'; FAIL=$((FAIL+1))
  fi
  teardown

  # ⑦ 죽은 경로에서도 모델 교체가 기록돼야 한다.
  setup
  detach env FAKE_SCENARIO_DESIGN=crash_swapped AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || true
  if grep -q '요청 claude-opus-5-5 → 실제 claude-opus-4-8' .pipeline/feat/MODEL_LOG.md 2>/dev/null; then
    green "  PASS  크래시 경로에서도 모델 교체가 MODEL_LOG 에 남는다"; PASS=$((PASS+1))
  else
    red   "  FAIL  크래시 시 모델 교체 미기록"; sed 's/^/         /' .pipeline/feat/MODEL_LOG.md 2>/dev/null; FAIL=$((FAIL+1))
  fi
  teardown
else
  red "  SKIP  부검 게이트 케이스 — setsid 사용 불가"
fi

# ⑧ 크래시했는데 산출물이 BLOCKED 면 "그냥 죽었다"가 아니라 막힌 이유를 넘긴다.
setup
got=0
# stderr 를 잡는다. STATE.md 의 안내 문구에도 'BLOCKED_NEEDS' 라는 낱말이 들어 있어서
# 그걸로 단언하면 실제 사유가 안 나와도 통과하는 공허한 검사가 된다.
env FAKE_SCENARIO_DESIGN=crash_blocked AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>blocked_err.txt || got=$?
if [ "$got" -eq 3 ] && grep -q 'phase: BLOCKED:design' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q '예산 상한 직전에 막힘' blocked_err.txt 2>/dev/null \
   && grep -q '스키마를 바꿔도 되는지' blocked_err.txt 2>/dev/null; then
  green "  PASS  BLOCKED 를 쓰고 죽으면 exit 3 으로 막힌 이유가 간다"; PASS=$((PASS+1))
else
  red   "  FAIL  크래시+BLOCKED — exit=$got (기대 3), phase=$(grep -m1 'phase:' .pipeline/feat/STATE.md 2>/dev/null)"
  sed 's/^/         /' blocked_err.txt 2>/dev/null | tail -6
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 증거 보존 ==="
# 재시도 루프의 사인은 "1차가 왜 죽었나"인데 그 파일이 2차에 덮였다 (2026-08-24 실측).
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ -f .pipeline/feat/impl.attempt1.stream.jsonl ] \
   && [ -f .pipeline/feat/impl.attempt1.result.json ] \
   && [ -f .pipeline/feat/impl.stream.jsonl ]; then
  green "  PASS  다시 도는 단계는 이전 증거를 attempt 번호로 보관한다"; PASS=$((PASS+1))
else
  red   "  FAIL  이전 스트림이 덮어써짐"
  ls -1 .pipeline/feat/ 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 단계별 쓰기 권한 게이트 ==="
# ALLOWED_FILES 하나로는 "누가 어느 파일을 고쳐도 되는가"를 못 가른다. 검증이 소스를
# 땜질해 테스트를 통과시키는 경로와 구현이 테스트를 깎는 경로는 둘 다 목록 안에서
# 일어나므로 범위 게이트를 그대로 통과한다 (2026-09-04 분석: verify 스트림 13개에서
# 사건 0건이었으나 게이트가 잡은 0이 아니라 아직 안 일어난 0이었다).
# 확인할 것은 네 방향이다 — 두 이탈을 잡는가, 두 정상 경로를 통과시키는가.
expect "검증이 ALLOWED_FILES 안의 소스를 고치면 죽는다" 2 -- \
  FAKE_SCENARIO_VERIFY=verify_edits_source FAKE_ALLOWED="x.txt t.test.txt" FAKE_TEST_FILES="t.test.txt"
expect "검증이 TEST_FILES 의 테스트 파일을 쓰면 완주한다 (대조군)" 0 -- \
  FAKE_SCENARIO_VERIFY=verify_edits_test FAKE_ALLOWED="x.txt t.test.txt" FAKE_TEST_FILES="t.test.txt"
expect "구현이 TEST_FILES 의 테스트 파일을 고치면 죽는다" 2 -- \
  FAKE_SCENARIO_IMPL=impl_edits_test FAKE_ALLOWED="x.txt t.test.txt" FAKE_TEST_FILES="t.test.txt"
expect "구현이 소스를 고치면 완주한다 (대조군)" 0 -- \
  FAKE_SCENARIO_IMPL=impl_edits_source FAKE_ALLOWED="x.txt t.test.txt" FAKE_TEST_FILES="t.test.txt"

# 낡은 테스트 인계: 이미 커밋된 테스트 파일을 검증이 고치는 것은 정상 경로다.
# 새 파일 생성만 통과시키고 기존 파일 수정을 막으면 impl↔verify 인계가 교착된다.
setup
echo "old" > t.test.txt; git add -A; git commit -qm "old test"
got=0
env FAKE_SCENARIO_VERIFY=verify_edits_test FAKE_ALLOWED="x.txt t.test.txt" FAKE_TEST_FILES="t.test.txt" \
  AUTO=1 TEST_CMD="$RG" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ]; then
  green "  PASS  검증이 기존 테스트 파일을 고쳐도 완주한다 (낡은 테스트 인계)"; PASS=$((PASS+1))
else
  red   "  FAIL  기존 테스트 수정이 막힘 — exit=$got (기대 0)"
  grep -m1 'note:' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

# 재시도 루프: 재시도는 impl 만 다시 돈다 — 테스트는 red 로 검증된 채 고정된다.
# 2차 impl 의 기준선은 impl 직전에 다시 찍는다. 아니면 verify 의 테스트 변경이 impl 의 죄가 된다.
setup
got=0
# 검증 명령: 기준선 통과 → red(구현 전) 실패 → green 1차 실패 → green 2차 통과 (.once 마커).
env FAKE_SCENARIO_VERIFY=verify_edits_test \
  FAKE_ALLOWED="x.txt t.test.txt" FAKE_TEST_FILES="t.test.txt" \
  AUTO=1 MAX_RETRY=1 TEST_CMD="! grep -qs verify t.test.txt || { [ -f .pipeline/feat/IMPL.md ] && { test -f .pipeline/feat/.once || { touch .pipeline/feat/.once; false; }; }; }" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && ! grep -q '테스트 파일을 수정함' .pipeline/feat/FAIL_LOG.md 2>/dev/null \
   && [ -f .pipeline/feat/impl.attempt1.result.json ] && [ ! -f .pipeline/feat/verify.attempt1.result.json ]; then
  green "  PASS  재시도는 impl 만 다시 돌고, verify 의 테스트 변경을 impl 의 죄로 보지 않는다"; PASS=$((PASS+1))
else
  red   "  FAIL  재시도에서 기준선이 낡음 — exit=$got (기대 0)"
  grep -m1 'note:' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

# 계약 형식: 블록이 없거나 ALLOWED_FILES 밖의 파일을 담으면 설계 직후($0 추가 비용)에 죽는다.
expect "설계에 TEST_FILES 블록이 없으면 죽는다"            2 -- FAKE_SCENARIO=ok FAKE_NO_TEST_FILES=1
expect "TEST_FILES 가 ALLOWED_FILES 밖의 파일을 담으면 죽는다" 2 -- \
  FAKE_SCENARIO=ok FAKE_ALLOWED="x.txt" FAKE_TEST_FILES="other.test.txt"
setup
got=0
env FAKE_SCENARIO=ok FAKE_NO_TEST_FILES=1 AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/JUDGE.md ]; then
  green "  PASS  계약 위반은 판단검증을 띄우기 전에 잡힌다 (비용 \$0)"; PASS=$((PASS+1))
else
  red   "  FAIL  계약 위반인데 judge 가 돌았거나 exit 이 다름 — exit=$got"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 정본 주입 (REQUIRED_DOCS) ==="
# "읽어라"는 부탁이고 주입은 강제다. 확인할 것: 설계 프롬프트에 본문이 줄번호와 함께 들어가고,
# 다른 단계에는 안 들어가며, 없는 파일이면 에이전트를 띄우기 전에 죽는다.
setup
mkdir -p docs; printf 'REQ-1 로그인은 JWT\nREQ-2 세션 만료 30분\n' > docs/spec.md; git add -A; git commit -qm spec
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" REQUIRED_DOCS="docs/spec.md" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] \
   && grep -q -- '--- docs/spec.md ---' .pipeline/feat/DESIGN.prompt.txt 2>/dev/null \
   && grep -qE '^\s*2\s+REQ-2 세션 만료 30분' .pipeline/feat/DESIGN.prompt.txt 2>/dev/null \
   && ! grep -q 'REQ-2' .pipeline/feat/JUDGE.prompt.txt 2>/dev/null; then
  green "  PASS  정본 문서가 줄번호와 함께 설계 프롬프트에만 주입된다"; PASS=$((PASS+1))
else
  red   "  FAIL  정본 주입 — exit=$got"; grep -n 'spec.md\|REQ-' .pipeline/feat/DESIGN.prompt.txt 2>/dev/null | head -3 | sed 's/^/         /'; FAIL=$((FAIL+1))
fi
teardown

setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" REQUIRED_DOCS="docs/없는문서.md" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/DESIGN.md ]; then
  green "  PASS  없는 정본 경로는 에이전트를 띄우기 전에 죽는다 (비용 \$0)"; PASS=$((PASS+1))
else
  red   "  FAIL  없는 정본 — exit=$got (기대 2), DESIGN.md=$([ -f .pipeline/feat/DESIGN.md ] && echo 생성됨 || echo 없음)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 설계 읽기 게이트 ==="
# 고치겠다는 기존 소스를 열어보지 않은 설계는 judge 전에 죽는다. 확인할 것: 막는가, 그리고
# 신규 파일·테스트 파일은 요구하지 않는가(대조군), 재사용 설계는 건너뛰는가.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" FAKE_ALLOWED="x.txt" FAKE_READS="" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/JUDGE.md ] \
   && grep -q '읽지 않음' .pipeline/feat/FAIL_LOG.md 2>/dev/null \
   && grep -q '^x.txt$' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  고치겠다는 기존 소스를 안 읽은 설계는 judge 전에 죽고 파일명이 FAIL_LOG 에 남는다"; PASS=$((PASS+1))
else
  red   "  FAIL  읽기 게이트 — exit=$got (기대 2), JUDGE.md=$([ -f .pipeline/feat/JUDGE.md ] && echo 생성됨 || echo 없음)"
  grep -m1 'note:' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'; FAIL=$((FAIL+1))
fi
teardown

expect "기존 소스를 읽은 설계는 완주한다 (대조군)" 0 -- FAKE_SCENARIO=ok FAKE_ALLOWED="x.txt" FAKE_READS="x.txt"
expect "신규 파일은 읽기를 요구하지 않는다"       0 -- FAKE_SCENARIO=ok FAKE_ALLOWED="x.txt new/file.ts" FAKE_READS="x.txt"

setup
echo "old" > t.test.txt; git add -A; git commit -qm "old test"
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="$RG" FAKE_ALLOWED="x.txt t.test.txt" FAKE_TEST_FILES="t.test.txt" FAKE_READS="x.txt" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ]; then
  green "  PASS  테스트 파일은 읽기를 요구하지 않는다 (TEST_FILES 제외)"; PASS=$((PASS+1))
else
  red   "  FAIL  테스트 파일 제외 — exit=$got (기대 0)"; grep -m1 'note:' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'; FAIL=$((FAIL+1))
fi
teardown

# Read 의 file_path 는 절대 경로다. 접두사가 달라도(/tmp vs /private/tmp) 접미사로 맞아야 한다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" FAKE_ALLOWED="x.txt" FAKE_READS="x.txt" FAKE_READ_ROOT="/private$SANDBOX" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ]; then
  green "  PASS  Read 경로의 접두사가 달라도 접미사로 대조한다"; PASS=$((PASS+1))
else
  red   "  FAIL  경로 접미사 대조 — exit=$got (기대 0)"; grep -m1 'note:' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== codex judge 엔진 ==="
# judge 기본값(codex:gpt-6.1-sol → codex:gpt-6-astra)으로 돌린다. setup 의 claude 고정을 푼다.
# run_codex <env할당들...> — 결과 코드는 $got 에 남는다. 케이스마다 setup/teardown 은 호출자가 한다.
run_codex() {
  got=0
  env -u MODEL_JUDGE -u FALLBACK_JUDGE "$@" AUTO=1 TEST_CMD="true" \
    ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
}
pass() { green "  PASS  $1"; PASS=$((PASS+1)); }
fail() { red   "  FAIL  $1"; FAIL=$((FAIL+1)); }
args1() { head -1 .pipeline/feat/JUDGE.args 2>/dev/null; }

setup
run_codex FAKE_SCENARIO=ok
if [ "$got" -eq 0 ] \
   && [ "$(args1)" = "engine=codex model=gpt-6.1-sol sandbox=read-only" ] \
   && grep -q 'model_reasoning_effort="high"' .pipeline/feat/JUDGE.args \
   && grep -q -- '--ignore-user-config' .pipeline/feat/JUDGE.args \
   && grep -q '^UNVERIFIED: 0 REFUTED: 0' .pipeline/feat/JUDGE.md 2>/dev/null \
   && grep -q '읽기 전용 샌드박스' .pipeline/feat/JUDGE.prompt.txt 2>/dev/null \
   && grep -q '종료 계약' .pipeline/feat/JUDGE.prompt.txt 2>/dev/null    && grep -q '감사 대상 본문 (셸이 주입했다' .pipeline/feat/JUDGE.prompt.txt 2>/dev/null    && grep -q 'ALLOWED_FILES:' .pipeline/feat/JUDGE.prompt.txt 2>/dev/null; then
  pass "기본값이면 judge 가 codex(gpt-6.1-sol·read-only·effort high)로 돌고, DESIGN.md 본문이 주입되고, 최종 응답이 JUDGE.md 가 된다"
else
  fail "codex 기본 경로 — exit=$got, args: $(args1)"
fi
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    if grep -q 'windows.sandbox="elevated"' .pipeline/feat/JUDGE.args        && grep -q 'cmd /c type' .pipeline/feat/JUDGE.prompt.txt; then
      pass "Windows 에서는 샌드박스 설정을 명령줄로 넘기고 한글이 깨지지 않는 읽기 명령을 지시한다"
    else fail "Windows 샌드박스 설정 누락 — 읽기 명령까지 거부된다"; fi ;;
  *)
    if ! grep -q 'windows.sandbox' .pipeline/feat/JUDGE.args        && ! grep -q 'cmd /c type' .pipeline/feat/JUDGE.prompt.txt; then
      pass "Windows 가 아니면 windows.sandbox 를 넘기지 않는다"
    else fail "Windows 아닌데 windows.sandbox 전달"; fi ;;
esac
teardown

setup; run_codex FAKE_CODEX_SCENARIO=fenced
if [ "$got" -eq 0 ] && [ "$(head -1 .pipeline/feat/JUDGE.md 2>/dev/null)" = "STATUS: DONE" ]; then
  pass "최종 응답이 코드 블록으로 감싸져 와도 울타리를 벗겨 STATUS 를 첫 줄에 둔다"
else fail "코드 블록 울타리 — exit=$got, 첫 줄: $(head -1 .pipeline/feat/JUDGE.md 2>/dev/null)"; fi
teardown

setup; run_codex FAKE_CODEX_SCENARIO=nocount
[ "$got" -eq 2 ] && pass "codex 판정에 카운트 라인이 없으면 죽는다 (exit 2)" \
                 || fail "codex 카운트 누락 — 기대 exit 2, 실제 $got"
teardown

setup; run_codex FAKE_CODEX_SCENARIO=no_message
[ "$got" -eq 2 ] && [ ! -f .pipeline/feat/JUDGE.md ] \
  && pass "codex 최종 응답이 없으면 산출물 없음으로 죽는다 (exit 2)" \
  || fail "codex 최종 응답 없음 — 기대 exit 2, 실제 $got"
teardown

setup; run_codex FAKE_CODEX_SCENARIO=crash
if [ "$got" -eq 2 ] && ! grep -q 'ENGINE_EXHAUSTED' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q 'judge 단계 프로세스 사망' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  pass "리밋이 아닌 codex 실패는 갈아타지 않고 사인을 남기고 죽는다 (exit 2)"
else fail "codex 일반 실패 — 기대 exit 2, 실제 $got"; fi
teardown

setup; run_codex CODEX_BIN=codex-없는-명령-xyz
if [ "$got" -eq 5 ] && grep -q 'phase: SETUP_NEEDED' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q 'npm install -g @openai/codex' .pipeline/feat/STATE.md \
   && [ ! -f .pipeline/feat/DESIGN.args ]; then
  pass "codex 미설치면 설계 전에(\$0) exit 5 로 멈추고 설치 안내를 남긴다"
else fail "codex 미설치 — 기대 exit 5·SETUP_NEEDED, 실제 $got"; fi
teardown

setup; run_codex FAKE_CODEX_LOGGED_OUT=1
if [ "$got" -eq 5 ] && grep -q 'phase: LOGIN_NEEDED' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q 'codex login' .pipeline/feat/STATE.md \
   && [ ! -f .pipeline/feat/DESIGN.args ]; then
  pass "codex 미로그인이면 설계 전에(\$0) exit 5 로 멈추고 로그인 안내를 남긴다"
else fail "codex 미로그인 — 기대 exit 5·LOGIN_NEEDED, 실제 $got"; fi
teardown

setup; run_codex FAKE_CODEX_RATELIMIT_MODELS="gpt-6.1-sol"
if [ "$got" -eq 0 ] && [ -f .pipeline/feat/judge.ratelimit1.stream.jsonl ] \
   && [ "$(args1)" = "engine=codex model=gpt-6-astra sandbox=read-only" ]; then
  pass "gpt-6.1-sol 이 리밋이면 codex 안에서 gpt-6-astra 로 갈아탄다"
else fail "codex 체인 순환 — exit=$got, args: $(args1)"; fi
teardown

setup; run_codex FAKE_CODEX_RATELIMIT_MODELS="gpt-6.1-sol gpt-6-astra"
if [ "$got" -eq 5 ] && grep -q 'phase: ENGINE_EXHAUSTED:judge' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q 'MODEL_JUDGE=claude-opus-5-5' .pipeline/feat/STATE.md \
   && [ ! -f .pipeline/feat/IMPL.md ] \
   && ! grep -q 'engine=claude\|model=claude' .pipeline/feat/JUDGE.args; then
  pass "codex 체인이 전부 리밋이면 claude 로 자동 전환하지 않고 exit 5 로 전환 여부를 묻는다"
else fail "codex 체인 소진 — 기대 exit 5·ENGINE_EXHAUSTED, 실제 $got, args: $(args1)"; fi
teardown

setup
run_codex FAKE_CODEX_RATELIMIT_MODELS="gpt-6.1-sol gpt-6-astra" FALLBACK_JUDGE="codex:gpt-6-astra,claude-opus-5-5"
if [ "$got" -eq 0 ] && [ "$(args1)" = "model=claude-opus-5-5 turns=80 budget=없음" ]; then
  pass "체인 끝에 claude 를 넣으면(B 방식) 사람 확인 없이 claude 로 넘어간다"
else fail "혼합 체인 — exit=$got, args: $(args1)"; fi
teardown

setup; run_codex MODEL_DESIGN=codex:gpt-6.1-sol
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/DESIGN.args ]; then
  pass "codex 엔진을 judge 밖 단계에 걸면 시작 전에 죽는다 (read-only 라 산출물을 못 쓴다)"
else fail "judge 전용 제약 — 기대 exit 2, 실제 $got"; fi
teardown

echo
echo "=== red→green 게이트 ==="
# 판정권은 셸에 있어도 판정 재료(테스트)는 에이전트가 만든다. 셸이 재료를 검사한다:
# 테스트 작성 전 기준선 녹색 → 구현 전 red → 구현 뒤 green.
TF=(FAKE_ALLOWED="x.txt t.test.txt" FAKE_TEST_FILES="t.test.txt")

setup
env FAKE_SCENARIO=ok "${TF[@]}" AUTO=1 TEST_CMD="$RG" ./orchestrate.sh feat >/dev/null 2>&1; got=$?
if [ "$got" -eq 0 ] && grep -q '^- 기준선(테스트 작성 전): 통과' .pipeline/feat/STATE.md \
   && grep -q '^- red(구현 전 새 테스트 실패): 확인' .pipeline/feat/STATE.md \
   && [ -f .pipeline/feat/red_out.txt ] && [ -f .pipeline/feat/baseline_out.txt ] \
   && grep -q 'VERIFY.md' .pipeline/feat/IMPL.prompt.txt 2>/dev/null; then
  pass "기준선 녹색 → 구현 전 red → 구현 뒤 green 이면 완주하고, 구현은 VERIFY.md 를 입력으로 받는다"
else fail "red→green 정상 경로 — exit=$got"; grep -E '^- (기준선|red)' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'; fi
teardown

setup
env FAKE_SCENARIO=ok "${TF[@]}" AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1; got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/IMPL.md ] \
   && grep -q 'red 게이트: 구현 전인데 검증 명령이 전부 통과' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  pass "테스트가 구현 전에도 통과하면 구현을 띄우지 않고 죽는다 (바뀔 동작을 검사하지 않는 테스트)"
else fail "red 게이트 — 기대 exit 2·IMPL.md 없음, 실제 exit=$got"; fi
teardown

setup
env FAKE_SCENARIO=ok "${TF[@]}" AUTO=1 TEST_CMD="false" ./orchestrate.sh feat >/dev/null 2>&1; got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/VERIFY.md ] \
   && grep -q '기준선이 이미 빨갛다' .pipeline/feat/STATE.md 2>/dev/null; then
  pass "테스트를 쓰기 전 기준선이 빨가면 verify 를 띄우지 않고 죽는다 (red 를 새 테스트 탓으로 판정 불가)"
else fail "기준선 게이트 — 기대 exit 2·VERIFY.md 없음, 실제 exit=$got"; fi
teardown

setup
env FAKE_SCENARIO=ok "${TF[@]}" AUTO=1 VERIFY_TIMEOUT=1 TEST_CMD="! grep -qs verify t.test.txt || sleep 20" \
  ./orchestrate.sh feat >/dev/null 2>&1; got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/IMPL.md ] && grep -q 'red 로 치지 않는다' .pipeline/feat/STATE.md 2>/dev/null; then
  pass "red 확인 중 시간 초과는 실패가 아니라 대기라서 red 로 치지 않는다"
else fail "red 시간 초과 — 기대 exit 2, 실제 $got"; fi
teardown

setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1; got=$?
if [ "$got" -eq 0 ] && grep -q '^- red(구현 전 새 테스트 실패): 해당 없음' .pipeline/feat/STATE.md \
   && [ ! -f .pipeline/feat/red_out.txt ]; then
  pass "설계가 TEST_FILES 를 비우면(동작 불변 변경) red 는 해당 없음으로 남기고 기존 검증만 본다"
else fail "테스트 없는 설계 — exit=$got"; fi
teardown

echo
echo "=== 단계 사이 봉인 ==="
# 게이트는 단계 직전 기준선으로 본다 — 단계 사이의 수정은 봉인 대조만 잡는다.

setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1; got=$?
if [ "$got" -eq 0 ] && [ ! -f .pipeline/feat/.seal ] \
   && grep -q '^stage_env=impl$' .pipeline/feat/IMPL.args 2>/dev/null \
   && grep -q '^stage_env=design$' .pipeline/feat/DESIGN.args 2>/dev/null; then
  pass "완주하면 봉인이 풀리고, 단계 에이전트에는 PIPELINE_STAGE 가 전달된다"
else fail "봉인 해제·PIPELINE_STAGE — exit=$got, seal=$([ -f .pipeline/feat/.seal ] && echo 남음 || echo 없음)"; fi
teardown

setup
env FAKE_SCENARIO=ok FAKE_SCENARIO_IMPL=scope_creep AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1; got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/.seal ]; then
  pass "게이트 위반으로 죽으면 봉인이 풀린다 (사람이 되돌릴 차례)"
else fail "die 후 봉인 — exit=$got"; fi
teardown

# exit 4(승인 대기)로 멈춘 뒤의 틈. tty 가 없으면 설계 게이트에서 exit 4 로 멈추고, tty 가 있으면
# 게이트가 입력을 기다린다 — 그때는 거기서 프로세스를 끊는다. 둘 다 "봉인은 남고 승인은 안 된"
# 같은 상태다 (끊긴 프로세스는 봉인을 치우지 않는다).
# stop_at_gate — 설계 게이트에서 멈췄으면(exit 4 또는 끊음) 0, 다른 결말이면 1.
stop_at_gate() {
  env FAKE_SCENARIO=ok TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 &
  local pid=$! rc=0
  for _ in $(seq 1 600); do
    kill -0 $pid 2>/dev/null || break
    if grep -q 'phase: GATE' .pipeline/feat/STATE.md 2>/dev/null; then kill $pid 2>/dev/null; wait $pid 2>/dev/null; return 0; fi
    sleep 0.1
  done
  wait $pid; rc=$?
  [ "$rc" -eq 4 ]
}

setup
if stop_at_gate; then
  have_seal=0; [ -f .pipeline/feat/.seal ] && have_seal=1
  ./approve.sh feat DESIGN.md --relayed y >/dev/null 2>&1
  env FAKE_SCENARIO=ok TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1; got=$?
  if [ "$have_seal" = 1 ] && [ "$got" -eq 0 ]; then
    pass "승인 대기(exit 4) 동안 봉인이 유지되고, 아무도 안 고쳤으면 승인 후 재실행이 완주한다"
  else fail "exit 4 봉인 대조군 — seal=$have_seal, 재실행 exit=$got (기대 0)"; fi
else
  fail "설계 게이트에서 멈추지 않음 (승인 대기 봉인 케이스 준비 실패)"
fi
teardown

setup
if stop_at_gate; then
  echo "(승인 대기 중 누군가 고침)" >> x.txt
  ./approve.sh feat DESIGN.md --relayed y >/dev/null 2>&1
  env FAKE_SCENARIO=ok TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1; got=$?
  if [ "$got" -eq 2 ] && grep -q '단계 사이 수정 감지' .pipeline/feat/FAIL_LOG.md 2>/dev/null \
     && grep -q '^x.txt$' .pipeline/feat/FAIL_LOG.md && [ -f .pipeline/feat/.seal ] \
     && [ ! -f .pipeline/feat/IMPL.md ]; then
    pass "승인 대기 중 worktree 를 고치면 재실행이 구현 전에 멈추고 봉인을 유지한다"
  else fail "단계 사이 수정 — 기대 exit 2·FAIL_LOG 기록·봉인 유지, 실제 exit=$got"; fi
else
  fail "설계 게이트에서 멈추지 않음 (단계 사이 수정 케이스 준비 실패)"
fi
teardown

echo
echo "=== 런처 읽기 전용 훅 ==="
HOOK="$SRC/hooks/pipeline-launcher-guard.sh"
HB="$(mktemp -d)"
( cd "$HB" && git init -q main && cd main && git config user.email t@t && git config user.name t \
  && echo x > x.txt && git add -A && git commit -qm init && git worktree add -q ../wt -b wtb )
mkdir -p "$HB/wt/.pipeline/feat"
# hook_rc <tool> <file_path> <cwd> [PIPELINE_STAGE] — 훅의 종료 코드
hook_rc() {
  jq -cn --arg t "$1" --arg p "$2" --arg c "$3" '{tool_name:$t, tool_input:{file_path:$p}, cwd:$c}' \
    | env ${4:+PIPELINE_STAGE=$4} bash "$HOOK" >/dev/null 2>&1
  echo $?
}
check_hook() {   # check_hook <설명> <기대 rc> <tool> <path> <cwd> [stage]
  local rc; rc="$(hook_rc "$3" "$4" "$5" "${6:-}")"
  if [ "$rc" = "$2" ]; then pass "$1"; else fail "$1 — 기대 $2, 실제 $rc"; fi
}
check_hook "봉인이 없으면 worktree 파일 수정을 막지 않는다"       0 Edit  "$HB/wt/x.txt"   "$HB/main"
echo "실행 시작" > "$HB/wt/.pipeline/feat/.seal.at"; : > "$HB/wt/.pipeline/feat/.seal"
check_hook "봉인이 있으면 그 worktree 파일의 Edit 를 막는다 (exit 2)" 2 Edit  "$HB/wt/x.txt"   "$HB/main"
check_hook "봉인이 있으면 Write 도 막는다"                        2 Write "$HB/wt/new.txt" "$HB/main"
check_hook "상대 경로도 cwd 기준으로 풀어서 막는다"               2 Edit  "x.txt"          "$HB/wt"
check_hook "다른 worktree(메인 체크아웃)의 수정은 막지 않는다"     0 Edit  "$HB/main/x.txt" "$HB/main"
check_hook "읽기 도구는 막지 않는다"                              0 Read  "$HB/wt/x.txt"   "$HB/main"
check_hook "단계 에이전트(PIPELINE_STAGE)는 통과한다"             0 Edit  "$HB/wt/x.txt"   "$HB/wt" impl
if command -v cygpath >/dev/null 2>&1; then
  check_hook "Windows 경로 표기(역슬래시·대문자)도 같은 파일로 본다" 2 Edit "$(cygpath -w "$HB/wt/x.txt" | tr '[:lower:]' '[:upper:]')" "$HB/main"
fi
rm -rf "$HB"

echo
echo "════════════════════════════"
printf "  통과 %d / 실패 %d\n" "$PASS" "$FAIL"
echo "════════════════════════════"
[ "$FAIL" -eq 0 ]
