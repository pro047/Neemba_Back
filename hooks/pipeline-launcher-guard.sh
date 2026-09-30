#!/usr/bin/env bash
# 런처 읽기 전용 훅 (Claude Code PreToolUse — Edit|Write|MultiEdit|NotebookEdit)
#
# 파이프라인이 실행 중인 worktree 의 파일을 런처 세션이 고치지 못하게 막는다.
# "실행 중" = 그 worktree 에 봉인(.pipeline/<feature>/.seal)이 있다. orchestrate.sh 가
# 시작할 때 찍고 DONE·DIED·BLOCKED 에서 지우며, exit 4(승인 대기)·5(사람 조치)에서는 남긴다
# — 런처가 승인을 중계하는 그 틈이 막아야 할 구간이다.
#
# 왜 필요한가: 게이트는 단계 **직전** 기준선으로 "이 단계가 바꾼 것"을 본다. 단계 **사이**의
# 수정은 다음 기준선에 흡수돼 안 잡힌다. 그 틈에 쓰기 도구를 가진 채 깨어 있는 LLM 이 런처다
# (2026-09-30 상담역 제거 — design-notes §3). launcher-protocol.md 의 규칙은 부탁이고 이건 권한이다.
#
# 이 훅이 못 막는 것: Bash 로 하는 쓰기(sed -i, >). 명령 문자열로 쓰기 여부를 가르는 것은
# 오탐·누락이 둘 다 커서 하지 않는다 — 그 경로는 orchestrate.sh 의 봉인 대조(check_seal)가
# 다음 단계 직전에 잡는다. 이 훅은 사전 차단, 봉인 대조는 사후 감지다.
#
# 단계 에이전트(claude -p)는 PIPELINE_STAGE 가 설정돼 있어 통과한다 — 쓰는 게 그들의 일이다.
# 차단은 exit 2 (stderr 가 세션에 전달된다). exit 1 은 통과시키므로 쓰지 않는다.

set -uo pipefail

[ -n "${PIPELINE_STAGE:-}" ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0   # jq 없으면 판정 불가 — 봉인 대조가 백스톱이다

input="$(cat)"
tool="$(jq -r '.tool_name // ""' <<<"$input" 2>/dev/null)"
case "$tool" in Edit|Write|MultiEdit|NotebookEdit) ;; *) exit 0 ;; esac

path="$(jq -r '.tool_input.file_path // .tool_input.notebook_path // ""' <<<"$input" 2>/dev/null)"
[ -n "$path" ] || exit 0
cwd="$(jq -r '.cwd // ""' <<<"$input" 2>/dev/null)"
[ -n "$cwd" ] || cwd="$PWD"

# 심볼릭 링크를 푼 실제 경로. macOS 는 /var → /private/var 라서 도구가 준 경로와
# git worktree list 가 보고하는 경로가 문자열로 어긋난다. 아직 없는 파일(Write)은
# 존재하는 가장 가까운 조상까지 올라가 풀고 나머지를 붙인다.
physical() {
  local p=$1 rest=""
  while [ -n "$p" ] && [ ! -d "$p" ]; do
    rest="/${p##*/}$rest"; p="${p%/*}"
  done
  if [ -z "$p" ]; then printf '%s' "$1"; return; fi
  printf '%s%s' "$(cd "$p" 2>/dev/null && pwd -P || printf '%s' "$p")" "$rest"
}

# 경로 정규화: Windows 는 C:\a\b · C:/a/b · /c/a/b 가 같은 파일이고 대소문자를 가리지 않는다.
norm() {
  local p
  p="$(physical "$1")"
  if command -v cygpath >/dev/null 2>&1; then
    p="$(cygpath -m "$p" 2>/dev/null || printf '%s' "$p")"
    p="${p//\\//}"
    p="$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')"
  fi
  printf '%s' "${p%/}"
}
case "$path" in
  /*|[A-Za-z]:[\\/]*) ;;
  *) path="$cwd/$path" ;;
esac
target="$(norm "$path")"

while IFS= read -r wt; do
  [ -n "$wt" ] || continue
  root="$(norm "$wt")"
  case "$target" in "$root"/*) ;; *) continue ;; esac
  for seal in "$wt"/.pipeline/*/.seal; do
    [ -f "$seal" ] || continue
    at="$(cat "$seal.at" 2>/dev/null || echo '?')"
    {
      echo "파이프라인 실행 중이라 이 worktree 의 파일을 고칠 수 없다: $path"
      echo "  봉인: $seal ($at 이후)"
      echo "  런처는 실행 중 worktree 를 수정하지 않는다 (launcher-protocol.md). 사람에게 보고하고,"
      echo "  수정은 파이프라인이 끝나거나(DONE) 멈춘(DIED·BLOCKED) 뒤에 한다."
      echo "  봉인이 이전 실행의 잔재라고 사람이 확인한 경우에만: rm '$seal'"
    } >&2
    exit 2
  done
done < <(git -C "$cwd" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p')

exit 0
