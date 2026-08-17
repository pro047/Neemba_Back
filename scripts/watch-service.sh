#!/usr/bin/env bash
# One probe of the neemba prod service, condensed to a single 5m tick.
# Usage: watch-service.sh [since]   (default 5m)
#
# Cumulative counters are meaningless on their own (ffmpeg_stale_total is a
# multi-day sum), so the previous tick's values are kept in STATE and each
# tick reports the delta.
#
# TODO: move the SSH key path to a NEEMBA_SSH_KEY env var with the value below
#       as the default. ~/Downloads is a location that may be wiped anytime.
set -uo pipefail

SINCE="${1:-5m}"
KEY=~/Downloads/neemba.pem
HOST=ubuntu@13.125.26.93
STATE="${TMPDIR:-/tmp}/neemba-watch-state.env"

RAW=$(ssh -i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes "$HOST" "SINCE='$SINCE' bash -s" <<'REMOTE'
set -u
echo "### TIME"
TZ=Asia/Seoul date '+kst=%Y-%m-%d %H:%M:%S'

echo "### SESSION"
M=$( { docker exec python curl -fsS http://localhost:8000/metrics; docker exec node curl -fsS http://localhost:3000/metrics; } 2>/dev/null )
lb=$(printf '%s' "$M" | awk '/^neemba_hub_last_broadcast_timestamp_seconds /{print $2}')
lb_i=$(printf '%.0f' "${lb:-0}" 2>/dev/null || echo 0)
now=$(date +%s)
# stt_paused and publish_buffer_size are per-session labelled series
# (`name{sessionId="..."} v`), so `[ {]` matches both labelled and bare forms.
# With no session the family is 0 lines, which is normal. Assumes no spaces
# inside the label value (uuids).
printf '%s' "$M" | awk '/^neemba_(hub_active_session|nats_connected|stt_paused|publish_buffer_size)[ {]/{printf "%s=%d\n",$1,$2}'
if [ "$lb_i" -gt 0 ]; then
  echo "last_broadcast=$(TZ=Asia/Seoul date -d @"$lb_i" '+%m-%d %H:%M:%S') age_min=$(( (now-lb_i)/60 ))"
fi
printf '%s' "$M" | awk '/^neemba_(ffmpeg_stale_total|hub_send_failed_total|consumer_unparseable_total|publish_buffer_dropped_total|requests_total) /{printf "CTR %s %d\n",$1,$2}'

echo "### CONTAINERS"
bad=0; now=$(date +%s)
for c in python node nats rtmp nginx postgres monitor certbot pg-backup pg-retention nats-box; do
  info=$(docker inspect -f '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}|{{.State.StartedAt}}' "$c" 2>/dev/null) || continue
  st=${info%%|*}; rest=${info#*|}; hl=${rest%%|*}; started=${rest#*|}
  s_i=$(date -d "$started" +%s 2>/dev/null || echo 0)
  age=$(( (now - s_i) / 60 ))
  # RestartCount is lifetime history and false-alarms on every tick. A start
  # time inside the watch window is the real "it just came back" signal.
  if [ "$st" != running ] || [ "$hl" = unhealthy ] || [ "$age" -lt 15 ]; then
    echo "$c status=$st health=$hl up_min=$age"; bad=1
  fi
done
[ "$bad" = 0 ] && echo "all nominal"

echo "### ERRORS"
found=0
for c in python node nats postgres rtmp monitor; do
  # Drop nginx access-log lines (IP - - [..]): that is scanner traffic.
  out=$(docker logs --since "$SINCE" "$c" 2>&1 | grep -iE 'error|exception|traceback|critical|fatal|panic' | grep -vE '^[0-9.]+ - - \[' | tail -8)
  [ -n "$out" ] && { echo "-- $c"; echo "$out"; found=1; }
done
[ "$found" = 0 ] && echo "none"

echo "### DROPS"
d=$(docker logs --since "$SINCE" python 2>&1 | grep -iE 'websocket disconnected|client disconnected|waiting for reconnect|reconnection timeout|detached|stopped|nats.*(disconnect|reconnect)' | tail -10)
[ -n "$d" ] && { echo "-- python"; echo "$d"; } || echo "python: none"
nd=$(docker logs --since "$SINCE" node 2>&1 | grep -iE 'disconnect|reconnect|stall|paused|publish_ended|on_publish|drop' | tail -10)
[ -n "$nd" ] && { echo "-- node"; echo "$nd"; } || echo "node: none"
a=$(docker logs --since "$SINCE" monitor 2>&1 | grep -E 'alerted' | tail -10)
[ -n "$a" ] && { echo "-- monitor alerts"; echo "$a"; } || echo "monitor: no alerts"
echo "nginx_5xx=$(docker logs --since "$SINCE" nginx 2>&1 | grep -cE '" 5[0-9]{2} ')"

echo "### TRANSLATIONS"
# Translations are not in the DB — the public schema holds only
# alembic_version, so broadcast text lives solely in the python container's
# 'hub: broadcast:' log lines.
b=$(docker logs --since "$SINCE" python 2>&1 | grep 'hub: broadcast:')
n=$(printf '%s' "$b" | grep -c . )
echo "count=$n"
[ "$n" -gt 0 ] && printf '%s\n' "$b" | tail -6 | sed 's/^/  /'
exit 0
REMOTE
)

[ -f "$STATE" ] && . "$STATE"
NEW_STATE=""
SEEN=""
echo "$RAW" | grep -v '^CTR '
echo "### COUNTER DELTAS"
while read -r _ name val; do
  [ -z "${name:-}" ] && continue
  key="prev_${name}"
  prev=$(eval "printf '%s' \"\${$key:-}\"")
  if [ -n "$prev" ]; then echo "$name +$(( val - prev )) (total=$val)"; else echo "$name total=$val (baseline)"; fi
  NEW_STATE="${NEW_STATE}${key}=${val}"$'\n'
  SEEN="${SEEN}${key} "
done < <(echo "$RAW" | grep '^CTR ')

# Never overwrite the baseline with what this tick failed to collect. An ssh
# timeout (SG closed, network blip) yields zero CTR lines, and blindly saving
# that empties STATE — the next tick then prints "(baseline)" for everything
# and the increments that happened during the outage vanish silently.
if [ -z "$NEW_STATE" ]; then
  echo "(counters not collected — previous baseline kept)"
  exit 0
fi
# Partial collection (e.g. only the node container is down) drops just that
# container's CTR lines, so carry unseen keys forward instead of losing them.
if [ -f "$STATE" ]; then
  while IFS= read -r line; do
    k=${line%%=*}
    [ -z "$k" ] && continue
    case " $SEEN " in
      *" $k "*) ;;
      *) NEW_STATE="${NEW_STATE}${line}"$'\n' ;;
    esac
  done < "$STATE"
fi
printf '%s' "$NEW_STATE" > "$STATE"
exit 0
