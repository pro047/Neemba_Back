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

# ── 매 테스트마다 깨끗한 샌드박스 repo 를 만든다
setup() {
  SANDBOX="$(mktemp -d)"
  cd "$SANDBOX"
  git init -q .
  git config user.email t@t; git config user.name t
  mkdir -p prompts test
  cp "$SRC/orchestrate.sh" .
  cp "$SRC/prompts/"*.md prompts/
  cp "$HERE/fake-claude" test/claude       # ← 이름이 'claude' 여야 가로챈다
  chmod +x orchestrate.sh test/claude
  printf '.pipeline/\n' > .gitignore
  echo x > x.txt; git add -A; git commit -qm init
  export PATH="$SANDBOX/test:$PATH"
  # 샌드박스는 worktree 가 아닌 평범한 저장소다. 격리 가드를 켠 채로 두면
  # 나머지 케이스가 전부 "가드에 막혀 exit 2" 로 통과해버린다 —
  # 통과하지만 아무것도 검증하지 않는 상태가 되므로 여기서 끈다.
  export REQUIRE_WORKTREE=0
}

teardown() { cd /; rm -rf "$SANDBOX"; }

# expect <설명> <기대exit코드> -- <env할당들...>
expect() {
  local desc=$1 want=$2; shift 3
  setup
  local got=0
  env "$@" AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then
    green "  PASS  $desc (exit $got)"; PASS=$((PASS+1))
  else
    red   "  FAIL  $desc — 기대 exit $want, 실제 $got"; FAIL=$((FAIL+1))
  fi
  teardown
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
# 그래서 phase 가 아니라 그걸 단정한다 (tty 유무에 따라 결과가 갈리지 않게).
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

# tty 없는 환경(cron·백그라운드·CI)에서 게이트가 **의도한 경로로** 죽는지.
# 이 스크립트는 set -e 로 도는데, /dev/tty 를 못 열면 read 가 rc=1 → 그 자리에서
# exit 1 이 되어 die 를 건너뛴다. 그러면 호출자가 "게이트에서 막힘"(exit 2)을
# 다른 실패와 구분할 수 없다. 실전 1회 실행에서 실제로 겪은 뒤 넣은 케이스다.
#
# macOS 에 setsid 가 없어서 python3 로 세션을 떼어 controlling terminal 을 없앤다.
# 테스트 전용 의존성이고, 없으면 케이스를 건너뛴다 (조용히 통과시키지 않는다).
if command -v python3 >/dev/null 2>&1; then
  setup
  got=0
  python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    env FAKE_SCENARIO_JUDGE=judge_flagged AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/IMPL.md ] \
     && grep -q 'phase: DIED' .pipeline/feat/STATE.md 2>/dev/null; then
    green "  PASS  tty 가 없으면 게이트가 exit 2 로 죽는다 (die 경로)"; PASS=$((PASS+1))
  else
    red   "  FAIL  tty 부재 시 종료 경로 — exit=$got (기대 2), phase=$(grep -m1 'phase:' .pipeline/feat/STATE.md 2>/dev/null)"
    FAIL=$((FAIL+1))
  fi
  teardown
else
  red "  SKIP  tty 부재 케이스 — python3 없음 (setsid 대체 불가)"
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
printf 'STATUS: DONE\n\n(새 설계)\n\nALLOWED_FILES:\n- x.txt\n\n' > .pipeline/feat/DESIGN.md
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ -f .pipeline/feat/judge.result.json ]; then
  green "  PASS  설계가 판정보다 새로우면 판단 검증을 다시 돌린다"; PASS=$((PASS+1))
else
  red   "  FAIL  낡은 JUDGE.md 를 그대로 재사용했다"; FAIL=$((FAIL+1))
fi
teardown

setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\n\n(사람이 이미 검토한 설계)\n\nALLOWED_FILES:\n- x.txt\n\n' > .pipeline/feat/DESIGN.md
sleep 1
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
echo "=== 재시도 루프 ==="
# 테스트가 항상 실패하면 MAX_RETRY 만큼 돌고 죽어야 한다
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 MAX_RETRY=2 TEST_CMD="false" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
attempts=$(grep -c '^## attempt' .pipeline/feat/FAIL_LOG.md 2>/dev/null | head -1)
attempts=${attempts:-0}
if [ "$got" -eq 2 ] && [ "$attempts" -eq 2 ]; then
  green "  PASS  테스트 계속 실패 → 2회 기록 후 포기 (exit 2)"; PASS=$((PASS+1))
else
  red   "  FAIL  재시도 루프 — exit=$got, FAIL_LOG 기록=$attempts (기대: 2, 2)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 설계 재사용 ==="
# 이미 DONE 인 DESIGN.md 가 있으면 설계 단계를 아예 호출하지 않아야 한다
setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\n\n(사람이 이미 검토한 설계)\n\nALLOWED_FILES:\n- x.txt\n\n' > .pipeline/feat/DESIGN.md
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
printf 'STATUS: DONE\n\n(사람이 이미 검토한 설계)\n\nALLOWED_FILES:\n- x.txt\n\n' > .pipeline/feat/DESIGN.md
env FAKE_SCENARIO=ok AUTO=1 FRESH_DESIGN=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ -f .pipeline/feat/design.result.json ]; then
  green "  PASS  FRESH_DESIGN=1 이면 설계를 다시 뽑는다"; PASS=$((PASS+1))
else
  red   "  FAIL  FRESH_DESIGN=1 인데 설계 단계가 안 돌았다"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 진행 스트림 ==="
# tee 가 원본 스트림을 보존해야 result 추출이 가능하다.
# 스트림이 비면 진행 표시도 죽고 게이트 판정 근거도 사라진다.
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if grep -q '"type":"assistant"' .pipeline/feat/design.stream.jsonl 2>/dev/null \
   && [ "$(jq -r '.is_error' .pipeline/feat/design.result.json 2>/dev/null)" = "false" ]; then
  green "  PASS  스트림이 보존되고 마지막 result 만 추출된다"; PASS=$((PASS+1))
else
  red   "  FAIL  스트림 보존/추출 실패"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 모델 교체 감시 ==="
setup
env FAKE_SCENARIO=model_swap AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1
if grep -q '요청 claude-fable-5 → 실제 claude-opus-4-8' .pipeline/feat/MODEL_LOG.md 2>/dev/null; then
  green "  PASS  다른 모델이 돌면 MODEL_LOG 에 기록된다"; PASS=$((PASS+1))
else
  red   "  FAIL  모델 교체가 기록되지 않음"
  echo "         MODEL_LOG 내용:"; sed 's/^/         /' .pipeline/feat/MODEL_LOG.md 2>/dev/null
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 상담역 상태 창구 ==="
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if grep -q 'phase: DONE' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  STATE.md 가 최종 상태를 반영한다"; PASS=$((PASS+1))
else
  red   "  FAIL  STATE.md 미갱신"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== worktree 격리 강제 ==="
# acceptEdits 로 도는 파이프라인이 메인 체크아웃을 덮어쓰는 것을 막는 가드.
# 확인할 것은 두 방향이다 — 메인에서 막는가, 그리고 worktree 에서는 통과시키는가.
# 앞만 테스트하면 "언제나 막는" 가드도 통과한다.
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
echo "=== 단계별 상한 ==="
# 모델과 턴/예산이 단계마다 다르게 전달되는지. 여기가 어긋나면
# "구현만 중간 티어 80턴" 같은 티어링 결정이 조용히 무효가 된다.
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
d="$(cat .pipeline/feat/DESIGN.args 2>/dev/null)"
j="$(cat .pipeline/feat/JUDGE.args  2>/dev/null)"
i="$(cat .pipeline/feat/IMPL.args   2>/dev/null)"
v="$(cat .pipeline/feat/VERIFY.args 2>/dev/null)"
if [ "$d" = "model=claude-fable-5 turns=40 budget=5" ] \
   && [ "$j" = "model=claude-fable-5 turns=40 budget=5" ] \
   && [ "$i" = "model=claude-sonnet-5 turns=80 budget=8" ] \
   && [ "$v" = "model=claude-fable-5 turns=40 budget=5" ]; then
  green "  PASS  단계별 모델·턴·예산이 각각 전달된다"; PASS=$((PASS+1))
else
  red   "  FAIL  상한 전달 어긋남"
  printf '         design: %s\n         judge : %s\n         impl  : %s\n         verify: %s\n' "$d" "$j" "$i" "$v"
  FAIL=$((FAIL+1))
fi
teardown

setup
env FAKE_SCENARIO=ok AUTO=1 TURNS_IMPL=7 BUDGET_IMPL=2 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1
if grep -q 'turns=7 budget=2' .pipeline/feat/IMPL.args 2>/dev/null; then
  green "  PASS  TURNS_IMPL/BUDGET_IMPL 환경변수가 기본값을 덮는다"; PASS=$((PASS+1))
else
  red   "  FAIL  상한 오버라이드가 안 먹음 — $(cat .pipeline/feat/IMPL.args 2>/dev/null)"
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 범위 이탈 게이트 ==="
# 확인할 것은 두 방향이다 — 이탈을 잡는가, 그리고 계약 안이면 통과시키는가.
# 앞만 보면 "언제나 죽이는" 게이트도 PASS 로 보인다.
expect "구현이 계약에 없는 파일을 만들면 죽는다" 2 -- FAKE_SCENARIO=scope_creep
expect "계약 안에서만 움직이면 완주한다"         0 -- FAKE_SCENARIO=ok

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

echo
echo "════════════════════════════"
printf "  통과 %d / 실패 %d\n" "$PASS" "$FAIL"
echo "════════════════════════════"
[ "$FAIL" -eq 0 ]
