#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <security-group-id> <allow|revoke> [port] [description] [ip]" >&2
  exit 1
fi

SG_ID="$1"
ACTION="$2"
PORT="${3:-22}"
DESC="${4:-gha-runner}"
IP="${5:-}"

if [[ "$ACTION" != "allow" && "$ACTION" != "revoke" ]]; then
  echo "Action must be 'allow' or 'revoke'." >&2
  exit 1
fi

if [[ -z "$IP" ]]; then
  if ! IP="$(curl -fsSL https://api.ipify.org)"; then
    echo "Failed to fetch runner public IP." >&2
    exit 1
  fi
fi

if [[ "$IP" != */* ]]; then
  CIDR="${IP}/32"
else
  CIDR="$IP"
fi

PERM_JSON="$(
  python3 - "$PORT" "$DESC" "$CIDR" <<'PY'
import json, sys
port = int(sys.argv[1])
desc = sys.argv[2]
cidr = sys.argv[3]
perm = {
  "IpProtocol": "tcp",
  "FromPort": port,
  "ToPort": port,
  "IpRanges": [{"CidrIp": cidr, "Description": desc}],
}
print(json.dumps([perm]))
PY
)"

if [[ "$ACTION" == "allow" ]]; then
  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --ip-permissions "$PERM_JSON"
else
  if ! OUTPUT="$(
    aws ec2 revoke-security-group-ingress \
      --group-id "$SG_ID" \
      --ip-permissions "$PERM_JSON" 2>&1
  )"; then
    if echo "$OUTPUT" | grep -q "InvalidPermission.NotFound"; then
      echo "No matching ingress rule to revoke for $CIDR." >&2
    else
      echo "$OUTPUT" >&2
      exit 1
    fi
  fi
fi

echo "${ACTION}ed SSH ingress for $CIDR on $SG_ID (port $PORT, description '$DESC')."
