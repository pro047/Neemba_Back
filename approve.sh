#!/usr/bin/env bash
# 사람 전용 승인 도구 — 게이트 마커를 만든다.
#
# 오케스트레이터가 tty 없이(런처 모드) 게이트에 걸리면 exit 4 로 멈추고 이
# 명령을 찍어 준다. 두 경로가 있다.
#
#   1) 사람이 터미널에서 직접:   ./approve.sh <feature> <artifact>
#      /dev/tty 에서 y 를 받아야 마커를 쓴다.
#   2) 런처(메인 세션)가 중계:  ./approve.sh <feature> <artifact> --relayed <사람의 답>
#      런처는 판단하지 않는다. 사람에게 파일을 보여주고 "승인? (y/n)" 하나만 물은 뒤,
#      사람이 친 답을 **그대로** 넘긴다. 답이 정확히 y/Y 일 때만 마커를 쓴다.
#      "괜찮아 보이면 해", "알아서" 는 y 가 아니다 — 마커를 만들지 않고 exit 1.
#      (2026-09-04 사용자 결정: 런처는 판단 금지, y 입력만 받는 게이트로 쓴다)
#
# 마커는 대상 파일의 sha256 을 담는다. 승인 후 파일이 한 글자라도 바뀌면 마커가
# 무효가 되므로 "무엇을 승인했는가"가 항상 내용 단위로 남는다.
# 누가 어떤 경로로 승인했는지는 .pipeline/<feature>/APPROVALS.md 에 append 된다 —
# 중계 경로는 tty 확인이 없으므로 감사 기록이 그 자리를 대신한다.
#
# 사용법:
#   ./approve.sh <feature> <artifact>                   # 예: ./approve.sh add-auth DESIGN.md
#   ./approve.sh <feature> <artifact> --relayed y       # 런처 중계
#   ./approve.sh --hash <file>                          # 내부/테스트용: 해시만 출력 (마커 안 만듦)
#   승인 취소: rm .pipeline/<feature>/<artifact>.approved

set -euo pipefail

# orchestrate.sh 의 file_hash 와 결과가 같아야 한다 (run-tests 가 교차 검증한다)
file_hash() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1"
  else sha256sum "$1"; fi | awk '{print $1}'
}

if [ "${1:-}" = "--hash" ]; then
  file_hash "${2:?사용법: ./approve.sh --hash <file>}"
  exit 0
fi

# cwd 가 아니라 스크립트 위치로 저장소를 잡는다 — 런처 세션의 cwd 는 worktree
# 밖(메인 체크아웃·홈 등)일 수 있고, 승인은 항상 이 스크립트가 놓인 저장소의
# .pipeline 을 향해야 한다.
ROOT="$(cd "$(dirname "$0")" && pwd)"
FEATURE="${1:?사용법: ./approve.sh <feature> <artifact> [--relayed <답>]}"
NAME="${2:?사용법: ./approve.sh <feature> <artifact> [--relayed <답>]}"
FILE="$ROOT/.pipeline/$FEATURE/$NAME"
AUDIT="$ROOT/.pipeline/$FEATURE/APPROVALS.md"

RELAY=0; ANSWER=""
if [ "${3:-}" = "--relayed" ]; then
  RELAY=1; ANSWER="${4-}"
elif [ -n "${3:-}" ]; then
  printf '알 수 없는 인자: %s\n' "$3" >&2; exit 2
fi

[ -f "$FILE" ] || { printf '없는 파일: %s\n' "$FILE" >&2; exit 2; }

approve() {   # approve <경로 설명>
  local how=$1 hash
  hash="$(file_hash "$FILE")"
  printf '%s\n' "$hash" > "$FILE.approved"
  printf -- '- %s | %s | %s | %s\n' "$(date -Iseconds)" "$NAME" "${hash:0:12}" "$how" >> "$AUDIT"
  printf '승인됨 → %s.approved (%s)\n재실행하면 이 게이트는 마커로 통과한다. 파일이 바뀌면 재승인이 필요하다.\n' "$FILE" "$how"
}

if [ "$RELAY" = "1" ]; then
  case "$ANSWER" in
    y|Y) approve "런처 중계 — 사람의 답: $ANSWER" ;;
    *)
      printf '중계된 답이 y 가 아니다 (%s) — 마커를 만들지 않음. 런처는 사람이 정확히 y 라고 답했을 때만 이 명령을 부른다.\n' "${ANSWER:-빈 값}" >&2
      exit 1 ;;
  esac
  exit 0
fi

printf '승인 대상: %s\n' "$FILE"
printf '  %s\n' "$(grep -m1 '^STATUS:' "$FILE" 2>/dev/null || echo '(STATUS 라인 없음)')"
printf '이 내용을 읽고 검토했는가? (y = 승인 마커 생성 / 그 외 = 취소)\n> '
if ! read -t 120 -r ans < /dev/tty; then
  printf '\ntty 없음 — 사람이 터미널에서 직접 하거나, 런처가 사람의 답을 --relayed 로 중계한다\n' >&2
  exit 2
fi
case "$ans" in
  y|Y) approve "사람이 터미널에서 직접" ;;
  *)   printf '취소 — 마커를 만들지 않음\n'; exit 1 ;;
esac
