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

# Prints "open" if tcp/$PORT from $CIDR is still allowed, "clear" otherwise.
# Description is deliberately ignored: it is not what makes a rule effective.
ingress_state() {
  local sg_json
  # Bail before parsing: `|| {}` at the call site disables errexit in here, so a
  # failed describe would otherwise reach python as an empty string.
  sg_json="$(aws ec2 describe-security-groups --group-ids "$SG_ID" --output json)" || return 1
  python3 - "$PORT" "$CIDR" "$sg_json" <<'PY'
import json, sys
port, cidr, raw = int(sys.argv[1]), sys.argv[2], sys.argv[3]
groups = json.loads(raw)["SecurityGroups"]
for perm in groups[0].get("IpPermissions", []):
    if perm.get("IpProtocol") != "tcp":
        continue
    if perm.get("FromPort") != port or perm.get("ToPort") != port:
        continue
    if any(r.get("CidrIp") == cidr for r in perm.get("IpRanges", [])):
        print("open")
        break
else:
    print("clear")
PY
}

if [[ "$ACTION" == "allow" ]]; then
  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --ip-permissions "$PERM_JSON"
  echo "allowed SSH ingress for $CIDR on $SG_ID (port $PORT, description '$DESC')."
  exit 0
fi

# revoke does not fail when nothing matched. It exits 0 and reports the miss in
# the response body's unknownIpPermissions, so exit code alone reads a surviving
# rule as success (observed 2026-08-02). The post-check below is what decides.
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
else
  # A miss comes back as exit 0 with the permission echoed under
  # UnknownIpPermissions (verified against the live API 2026-08-06). The field is
  # absent on a clean revoke, so count it rather than grepping for the name.
  UNKNOWN="$(
    printf '%s' "$OUTPUT" |
      python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("UnknownIpPermissions") or []))' \
      2>/dev/null || echo 0
  )"
  if [[ "$UNKNOWN" != "0" ]]; then
    echo "AWS matched no permission to revoke — verifying actual state." >&2
  fi
fi

STATE="$(ingress_state)" || {
  echo "Could not verify $SG_ID after revoke — treat tcp/$PORT from $CIDR as OPEN." >&2
  exit 1
}

case "$STATE" in
  clear)
    echo "revoked and verified: tcp/$PORT from $CIDR is not open on $SG_ID."
    ;;
  open)
    echo "REVOKE FAILED: tcp/$PORT from $CIDR is still open on $SG_ID." >&2
    echo "Delete it by rule id (needs ec2:DescribeSecurityGroupRules):" >&2
    echo "  aws ec2 describe-security-group-rules --filters Name=group-id,Values=$SG_ID" >&2
    exit 1
    ;;
  *)
    echo "Unexpected verification result '$STATE' — treat tcp/$PORT from $CIDR as OPEN." >&2
    exit 1
    ;;
esac
