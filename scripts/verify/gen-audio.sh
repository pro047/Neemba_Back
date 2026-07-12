#!/usr/bin/env bash
# Generate Korean test audio as raw 16kHz mono s16le PCM for run-scenario.mjs.
# Requires: macOS `say` (Korean voice) + ffmpeg (brew install ffmpeg).
set -euo pipefail
cd "$(dirname "$0")"

VOICE="${VOICE:-Yuna}"
if ! say -v '?' | grep -q "^${VOICE} "; then
  VOICE=$(say -v '?' | awk '/ko_KR/{print $1; exit}')
  echo "requested voice unavailable, falling back to: ${VOICE}"
fi

mkdir -p audio

# short: first-utterance + timeout-flush probe. The trailing fragment ends
# WITHOUT a Korean sentence ending on purpose — it should still arrive via
# the 2s timeout flush (PR #8).
SHORT_TEXT="안녕하세요 여러분. 오늘은 실시간 번역 파이프라인을 검증합니다. \
첫 문장이 잘 도착하는지 확인해 보겠습니다. 두 번째 문장도 이어서 확인합니다. \
그리고 마지막 조각은 종결어미 없이 끝나는 미완성"
say -v "$VOICE" -o audio/short.aiff "$SHORT_TEXT"
ffmpeg -y -loglevel error -i audio/short.aiff -ar 16000 -ac 1 -f s16le audio/short.pcm

# long: must exceed 320s so the 285s stream rotation fires mid-speech
# (PR #6). ~11s per repeat → 36 repeats ≈ 390s. Bump REPEATS if the
# reported duration is under 340s.
REPEATS="${REPEATS:-36}"
LONG_TEXT=$(python3 - "$REPEATS" <<'PY'
import sys
n = int(sys.argv[1])
base = ("지금부터 스트림 회전 경계를 검증하기 위한 긴 발화를 계속 이어갑니다. "
        "이것은 문장 번호 {i}번입니다. 회전이 일어나도 문장이 오염되면 안 됩니다. ")
print(" ".join(base.format(i=i) for i in range(1, n + 1)))
PY
)
say -v "$VOICE" -o audio/long.aiff "$LONG_TEXT"
ffmpeg -y -loglevel error -i audio/long.aiff -ar 16000 -ac 1 -f s16le audio/long.pcm

for f in audio/short.pcm audio/long.pcm; do
  bytes=$(stat -f%z "$f")
  echo "$f: $((bytes / 32000))s"
done
echo "done — pass these to run-scenario.mjs"
