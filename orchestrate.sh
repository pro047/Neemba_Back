#!/usr/bin/env bash
# 실행 역할 터미널 (터미널 1)
#
# 역할 분리:
#   이 스크립트  = 오케스트레이터. 진행 결정권을 독점한다.
#   advisor.sh   = 상담역. 읽기 전용. 진행 권한 없음.
#   사람         = 유일하게 게이트 버튼을 누르는 주체.
#
# 사용법:
#   ./orchestrate.sh <feature-name>
#   AUTO=1 ./orchestrate.sh <feature-name>     # 사람 게이트 건너뜀 (무인)
#   MAX_RETRY=3 ./orchestrate.sh <feature-name>

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
# neemba 는 services/{node,python} 두 스택이고 루트에 package.json 이 없다.
# python 은 **반드시 .venv/bin/python** — 시스템 python 으로 돌리면
# prometheus_client·nats 미설치로 수집 단계에서 12건 에러가 나고, 그걸 코드
# 결함으로 오독하기 쉽다 (docs/handover-2026-08-02.md §5).
TEST_CMD="${TEST_CMD:-cd services/python && .venv/bin/python -m pytest -q && cd ../node && npm test}"

# ── 모델 티어링 ──────────────────────────────────────
# 별칭 대신 풀 ID를 박는다. 별칭은 어느 날 조용히 다른 모델을 가리킨다.
#
#   설계  : 최상위. 여기가 틀리면 뒤가 전부 낭비다.
#   구현  : 설계가 확정돼 있으면 난이도가 내려간다. 중간 티어로 충분.
#   검증  : 다시 최상위. "설계에서 벗어난 지점 찾기"는 적대적 추론이라 구현보다 어렵다.
#
# FALLBACK_* 은 가용성 폴백(529 과부하 등) 전용이다.
# 안전 분류기에 의한 모델 교체는 이걸로 막을 수 없다 — MODEL_LOG.md 로 감시한다.
MODEL_DESIGN="${MODEL_DESIGN:-claude-fable-5}"
# 판단 검증은 설계를 반박하는 일이라 verify 와 같은 적대적 추론이다. 상위 모델.
MODEL_JUDGE="${MODEL_JUDGE:-claude-fable-5}"
MODEL_IMPL="${MODEL_IMPL:-claude-sonnet-5}"
MODEL_VERIFY="${MODEL_VERIFY:-claude-fable-5}"

# 폴백은 티어를 내리지 않는다. 구현 주 모델이 이미 중간 티어라 아래로 갈 곳이 없고,
# 과부하 때 하위 티어로 떨어뜨리면 산출물 품질이 조용히 무너진다 — 그래서 위로 올린다.
FALLBACK_DESIGN="${FALLBACK_DESIGN:-claude-opus-5,claude-sonnet-5}"
FALLBACK_JUDGE="${FALLBACK_JUDGE:-claude-opus-5,claude-sonnet-5}"
FALLBACK_IMPL="${FALLBACK_IMPL:-claude-opus-5}"
FALLBACK_VERIFY="${FALLBACK_VERIFY:-claude-opus-5,claude-sonnet-5}"

# ── 단계별 상한 ──────────────────────────────────────
# 턴 상한은 모델 티어와 같이 움직인다. 티어를 내리면 시행착오가 늘어 턴을 더 먹는다.
# 실측: opus-5 구현이 41턴에서 error_max_turns 로 죽었다 (2026-08-17, Flutter 앱)
#
# 실질 브레이크는 예산이다. 턴 상한은 무한루프 탈출용으로만 둔다 —
# 턴으로 조이면 "일은 잘 하는데 상한에 걸려 죽는" 낭비가 생긴다.
TURNS_DESIGN="${TURNS_DESIGN:-40}"
TURNS_JUDGE="${TURNS_JUDGE:-40}"
TURNS_IMPL="${TURNS_IMPL:-80}"
TURNS_VERIFY="${TURNS_VERIFY:-40}"

BUDGET_DESIGN="${BUDGET_DESIGN:-5}"
BUDGET_JUDGE="${BUDGET_JUDGE:-5}"
BUDGET_IMPL="${BUDGET_IMPL:-8}"
BUDGET_VERIFY="${BUDGET_VERIFY:-5}"

MODEL_LOG=""   # WORK 확정 후 아래에서 설정

# ── worktree 격리 강제 ───────────────────────────────
# 각 단계는 --permission-mode acceptEdits 로 돈다. 메인 체크아웃에서 돌리면
# 사람이 작업 중인 파일을 에이전트가 그대로 덮어쓴다. 규칙으로 부탁하지 않고 막는다.
#
# 판별: worktree 안에서는 git-dir 이 <main>/.git/worktrees/<name>,
#       git-common-dir 은 <main>/.git 을 가리킨다. 메인 체크아웃에서는 둘이 같다.
#
# die() 를 안 쓴다 — 아직 아무 단계도 안 돌았는데 STATE.md 에 DIED 를 남기면
# 상담역이 "돌다가 죽었다"로 읽는다. 시작 자체를 거부한 것과는 다른 사건이다.
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
STATE="$WORK/STATE.md"           # 상담역이 읽는 유일한 실시간 창구
MODEL_LOG="$WORK/MODEL_LOG.md"   # 요청 모델 vs 실제 실행 모델
touch "$FAIL_LOG" "$MODEL_LOG"

export FEATURE WORK ROOT

log() { printf '\033[1;36m[orch]\033[0m %s\n' "$*" >&2; }
die() { state "DIED" "$*"; printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 2; }

# ─────────────────────────────────────────── 상담역용 상태 브로드캐스트
# 셸은 대화를 못 한다. 대신 상태를 파일로 흘려서 상담역이 읽게 한다.
state() {
  local phase=$1 note=${2:-}
  cat > "$STATE" <<EOF
# 파이프라인 상태 (셸이 자동 생성 — 사람이 편집하지 말 것)

- feature: $FEATURE
- phase: $phase
- attempt: ${ATTEMPT:-0} / $((MAX_RETRY + 1))
- pid: $$
- updated: $(date -Iseconds)
- note: $note

## 지금까지 생성된 산출물
$(ls -1 "$WORK"/*.md 2>/dev/null | sed 's|.*/|- |' || echo "- (없음)")

## 마지막 테스트 출력 (tail 20)
\`\`\`
$(tail -20 "$WORK/test_out.txt" 2>/dev/null || echo "(아직 없음)")
\`\`\`
EOF
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
  # 인자로 더 받지 않는 이유 — 이미 5개다. 7개짜리 위치 인자는 호출부에서 순서를 틀리게 된다.
  local upper turns_var budget_var turns budget
  upper="$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')"
  turns_var="TURNS_$upper"; budget_var="BUDGET_$upper"
  turns="${!turns_var:-40}"; budget="${!budget_var:-5}"

  state "RUNNING:$name" "model=$model"
  log "▶ $name (model=$model, fallback=$fallback, 턴≤$turns, 예산≤\$$budget)"

  set +e
  envsubst < "$prompt_file" | claude -p \
    --model "$model" \
    --fallback-model "$fallback" \
    --output-format stream-json \
    --verbose \
    --max-turns "$turns" \
    --max-budget-usd "$budget" \
    --permission-mode acceptEdits \
    --append-system-prompt "$(cat "$PROMPTS/_contract.md")" \
    | tee "$stream" \
    | jq --unbuffered -r '
        select(.type? == "assistant") | .message.content[]? |
        if .type == "tool_use" then
          "  ⚙ \(.name)  \((.input.file_path // .input.command // .input.pattern // .input.description // "") | tostring | .[0:90])"
        elif .type == "text" and ((.text // "") | length) > 0 then
          "  💬 \(.text | gsub("\\s+"; " ") | .[0:160])"
        else empty end' >&2
  code=${PIPESTATUS[1]}   # [0]=envsubst [1]=claude [2]=tee [3]=jq — 판정 기준은 claude
  set -e

  [ "$code" -eq 0 ] || die "$name: claude 프로세스 실패 (exit $code)"

  # 스트림 마지막의 result 이벤트 = 기존 --output-format json 이 주던 것과 같은 오브젝트
  jq -s '[.[] | select(.type? == "result")] | last' "$stream" > "$out" 2>/dev/null || true
  [ "$(jq -r 'type' "$out" 2>/dev/null)" = "object" ] \
    || die "$name: 스트림에 result 이벤트가 없음 → $stream 확인"

  [ "$(jq -r '.is_error' "$out")" = "false" ] \
    || die "$name: 에이전트 에러 — $(jq -r '.result' "$out" | head -c 300)"

  log "  \$$(jq -r '.total_cost_usd' "$out") / 턴 $(jq -r '.num_turns' "$out")"

  # ── 모델 교체 감시 ────────────────────────────────
  # 안전 분류기가 걸리면 --model 로 지정한 모델이 아닌 다른 모델이 돈다.
  # 이건 --fallback-model 로 막을 수 없으므로, 막는 대신 기록해서 눈에 띄게 한다.
  # ※ 필드명은 버전마다 다를 수 있다. 첫 실행 후 `jq 'keys' result.json` 으로 확인할 것.
  local actual
  actual=$(jq -r '(.modelUsage // {} | keys | join(",")) // empty' "$out" 2>/dev/null || true)
  [ -z "$actual" ] && actual=$(jq -r '.model // empty' "$out" 2>/dev/null || true)

  if [ -n "$actual" ] && [[ "$actual" != *"$model"* ]]; then
    log "  ⚠ 모델 교체 감지: 요청=$model 실제=$actual"
    echo "- $(date -Iseconds) | $name | 요청 $model → 실제 $actual" >> "$MODEL_LOG"
    if [ "$AUTO" != "1" ]; then
      gate_human "요청한 모델이 안 돌았다. 결과를 신뢰할지 판단해라" "$MODEL_LOG"
    fi
  elif [ -z "$actual" ]; then
    echo "- $(date -Iseconds) | $name | 실제 모델 확인 불가 (필드명 점검 필요)" >> "$MODEL_LOG"
  fi

  # 게이트 1: 산출물 물리적 존재
  [ -f "$artifact" ] || die "$name: 산출물 없음 → $artifact"

  # 게이트 2: 종료 형식
  local verdict
  verdict="$(grep -m1 '^STATUS:' "$artifact" | awk '{print $2}' || true)"
  case "${verdict:-MISSING}" in
    DONE)
      log "  ✔ $name DONE" ;;
    BLOCKED)
      state "BLOCKED:$name" "사람 판단 필요"
      log "  ⛔ $name BLOCKED"
      sed -n '/^BLOCKED_REASON:/,$p' "$artifact" >&2
      printf '\n\033[1;33m→ 터미널 2에서 이렇게 물어봐:\033[0m\n  "%s BLOCKED 났어. 원인 뭐야?"\n\n' "$name" >&2
      exit 3 ;;
    *)
      die "$name: STATUS 라인 없음 또는 형식 위반 (DONE|BLOCKED 필수)" ;;
  esac
}

# ─────────────────────────────────────────── 사람 게이트
# 상담역은 여기에 손댈 수 없다. 오직 사람만 누른다.
# gate_human <메시지> <검토파일> [force]
#
# force=1 이면 AUTO=1 이어도 멈춘다. 검증되지 않은 주장을 무인으로 통과시키면
# 이 파이프라인이 막으려는 것(근거 없는 판단이 구현까지 흘러가는 것)이 그대로
# 일어난다 — 무인 모드는 "게이트를 없앤다"가 아니라 "판정 가능한 것만 자동으로
# 넘긴다"는 뜻이다.
gate_human() {
  local msg=$1 file=$2 force=${3:-0}
  [ "$AUTO" = "1" ] && [ "$force" != "1" ] \
    && { log "  (AUTO=1 — 게이트 통과: $msg)"; return 0; }

  state "GATE" "$msg"
  cat >&2 <<EOF

$(printf '\033[1;33m[게이트]\033[0m') $msg
  검토 대상: $file
  상담역에게: "$(basename "$file") 봐줘"

  y = 진행   e = 열어보기   n = 중단
EOF
  printf '  > ' >&2
  # `|| ans=n` 은 tty 가 없을 때(cron·백그라운드·CI)를 위한 것이다. 이 스크립트는
  # `set -e` 로 도는데, /dev/tty 를 못 열면 read 가 rc=1 로 끝나 **그 자리에서
  # exit 1** 이 된다 — 아래 case 도 die 도 타지 않아서 호출자는 "게이트에서 막힘"
  # 을 다른 실패와 구분할 수 없다. 없는 tty 는 "사람이 y 를 누르지 않았다" 와 같은
  # 뜻이므로 중단(n)으로 떨어뜨려 의도한 die 경로를 타게 한다.
  local ans; read -r ans < /dev/tty || ans=n
  case "$ans" in
    y|Y) return 0 ;;
    e|E) "${EDITOR:-less}" "$file"; gate_human "$msg" "$file" "$force" ;;
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
gate_scope() {
  local stage=$1
  local allowed="$WORK/allowed_files.txt" changed="$WORK/changed_files.txt"

  # grep 은 매치가 0건이면 exit 1 이다. set -e 아래에서 그건 "계약이 비었다"가 아니라
  # "스크립트 사망"으로 나타난다 — 게이트가 판정하기 전에 죽으므로 반드시 감싼다.
  set +e
  sed -n '/^ALLOWED_FILES:/,/^[[:space:]]*$/p' "$WORK/DESIGN.md" \
    | grep '^- ' | sed -e 's/^- *//' -e 's|^\./||' | sort -u > "$allowed"

  # cut -c4- : git status --porcelain 은 앞 3칸이 상태코드+공백이다.
  # .pipeline/ 은 산출물이라 항상 제외한다 (.gitignore 가 지워져도 게이트는 살아 있어야 한다)
  git -C "$ROOT" status --porcelain \
    | cut -c4- | sed 's|^\./||' | grep -v '^\.pipeline/' | sort -u > "$changed"
  set -e

  [ -s "$allowed" ] \
    || die "$stage: DESIGN.md 에 ALLOWED_FILES 블록이 없다 — 범위를 계약으로 만들 수 없다 (prompts/design.md 참조)"

  local strays count
  strays="$(comm -13 "$allowed" "$changed")"
  if [ -n "$strays" ]; then
    count=$(printf '%s\n' "$strays" | wc -l | tr -d ' ')
    log "⚠ $stage: 설계에 없는 파일이 변경됐다 (${count}개)"
    printf '%s\n' "$strays" | sed 's/^/     /' >&2
    {
      echo "## scope creep ($stage) — $(date -Iseconds)"
      printf '%s\n' "$strays"
      echo
    } >> "$FAIL_LOG"
    die "$stage: 범위 이탈 ${count}개 → $FAIL_LOG"
  fi

  log "  ✔ $stage 범위 준수 (계약 $(wc -l < "$allowed" | tr -d ' ')개 파일)"
}

# ─────────────────────────────────────────── 파이프라인
ATTEMPT=0
state "START"
log "=== $FEATURE 시작 ==="
log "상담역 띄우려면 다른 터미널에서: ./advisor.sh $FEATURE"

if [ "$FRESH_DESIGN" != "1" ] && [ -f "$WORK/DESIGN.md" ] \
   && [ "$(grep -m1 '^STATUS:' "$WORK/DESIGN.md" | awk '{print $2}')" = "DONE" ]; then
  log "↺ 기존 DESIGN.md 재사용 ($(date -r "$WORK/DESIGN.md" '+%m-%d %H:%M') 생성) — 새로 뽑으려면 FRESH_DESIGN=1"
  state "REUSED:design" "기존 산출물 재사용"
else
  run_stage design "$MODEL_DESIGN" "$FALLBACK_DESIGN" "$PROMPTS/design.md" "$WORK/DESIGN.md"
fi

# ─────────────────────────────────────────── 판단 검증
# 설계의 '주장'을 별 프로세스가 감사한다. 구현물에는 테스트·게이트가 있는데
# 판단물(원인 판정·우선순위·"X 가 없다")은 아무 검사 없이 구현으로 흘러갔다.
# DESIGN.md 보다 새로우면 재사용한다 — 설계가 새로 돌면 판정도 다시 받아야 한다.
if [ -f "$WORK/JUDGE.md" ] && [ "$WORK/JUDGE.md" -nt "$WORK/DESIGN.md" ] \
   && [ "$(grep -m1 '^STATUS:' "$WORK/JUDGE.md" | awk '{print $2}')" = "DONE" ]; then
  log "↺ 기존 JUDGE.md 재사용 (DESIGN.md 보다 최신)"
  state "REUSED:judge" "기존 산출물 재사용"
else
  run_stage judge "$MODEL_JUDGE" "$FALLBACK_JUDGE" "$PROMPTS/judge.md" "$WORK/JUDGE.md"
fi

# 판단 검증은 읽기 전용 단계다 — 확인용 임시 파일을 만들었다면 지웠어야 한다.
# 카운트 게이트보다 먼저 본다: judge 가 파일을 건드렸다면 사람이 y 를 누르기 전에 드러나야 한다.
gate_scope judge

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
  state "JUDGE_FLAGGED" "미확인 $UNVERIFIED / 반박 $REFUTED"
  gate_human \
    "설계의 주장 중 반박 $REFUTED 건·미확인 $UNVERIFIED 건 — 이대로 구현하면 그 위에 코드가 쌓인다" \
    "$WORK/JUDGE.md" 1
fi

gate_human "설계 검토 — 여기서 틀리면 뒤가 전부 낭비다" "$WORK/DESIGN.md"

while :; do
  ATTEMPT=$((ATTEMPT + 1))
  log "── 시도 $ATTEMPT/$((MAX_RETRY + 1))"

  run_stage impl   "$MODEL_IMPL"   "$FALLBACK_IMPL"   "$PROMPTS/impl.md"   "$WORK/IMPL.md"
  gate_scope impl
  run_stage verify "$MODEL_VERIFY" "$FALLBACK_VERIFY" "$PROMPTS/verify.md" "$WORK/VERIFY.md"
  gate_scope verify

  # ★ 최종 판정은 셸이 한다. 에이전트에게 안 맡긴다.
  state "TESTING" "$TEST_CMD"
  if (cd "$ROOT" && eval "$TEST_CMD") > "$WORK/test_out.txt" 2>&1; then
    log "✅ 검증 통과 ($TEST_CMD)"
    break
  fi

  log "❌ 테스트 실패"
  tail -30 "$WORK/test_out.txt" >&2
  state "TEST_FAILED" "attempt $ATTEMPT"

  [ "$ATTEMPT" -gt "$MAX_RETRY" ] \
    && die "검증 ${MAX_RETRY}회 재시도 후에도 실패 → $FAIL_LOG"

  {
    echo "## attempt $ATTEMPT — $(date -Iseconds)"
    echo '```'
    tail -60 "$WORK/test_out.txt"
    echo '```'
    echo
  } >> "$FAIL_LOG"

  gate_human "재시도 $((ATTEMPT + 1)) 진행? (상담역에게 FAIL_LOG 물어봐도 됨)" "$FAIL_LOG"
done

state "DONE"
log "=== $FEATURE 완료 ==="
log "산출물: $WORK/{DESIGN,JUDGE,IMPL,VERIFY}.md"
