#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <security-group-id> [port] [description]" >&2
  exit 1
fi

SG_ID="$1"
PORT="${2:-22}"
DESC="${3:-gha-actions}"

META_JSON="$(curl -fsSL https://api.github.com/meta)"

REVOKE_JSON="$(
  aws ec2 describe-security-groups \
    --group-ids "$SG_ID" \
    --query 'SecurityGroups[0].IpPermissions' \
    --output json |
  python3 - "$PORT" "$DESC" <<'PY'
import sys, json
port = int(sys.argv[1])
desc = sys.argv[2]
perms = json.load(sys.stdin)

def filter_ranges(key, cidr_key):
  ranges = []
  for p in perms:
    if p.get("IpProtocol") != "tcp":
      continue
    if p.get("FromPort") != port or p.get("ToPort") != port:
      continue
    for r in p.get(key, []):
      if r.get("Description") == desc and r.get(cidr_key):
        ranges.append(r.get(cidr_key))
  return ranges

ipv4 = filter_ranges("IpRanges", "CidrIp")
ipv6 = filter_ranges("Ipv6Ranges", "CidrIpv6")

perm = {"IpProtocol": "tcp", "FromPort": port, "ToPort": port}
out = []
if ipv4:
  perm_v4 = dict(perm)
  perm_v4["IpRanges"] = [{"CidrIp": c, "Description": desc} for c in ipv4]
  out.append(perm_v4)
if ipv6:
  perm_v6 = dict(perm)
  perm_v6["Ipv6Ranges"] = [{"CidrIpv6": c, "Description": desc} for c in ipv6]
  out.append(perm_v6)

print(json.dumps(out))
PY
)"

if [[ "$REVOKE_JSON" != "[]" ]]; then
  aws ec2 revoke-security-group-ingress \
    --group-id "$SG_ID" \
    --ip-permissions "$REVOKE_JSON"
fi

echo "$META_JSON" | python3 - "$PORT" "$DESC" <<'PY' | while IFS= read -r perm; do
import sys, json
port = int(sys.argv[1])
desc = sys.argv[2]
data = json.load(sys.stdin)
cidrs = data.get("actions", [])
ipv4 = [c for c in cidrs if ":" not in c]
ipv6 = [c for c in cidrs if ":" in c]

def chunks(items, size=50):
  for i in range(0, len(items), size):
    yield items[i:i+size]

def emit(cidrs, ipv6=False):
  for chunk in chunks(cidrs):
    perm = {"IpProtocol": "tcp", "FromPort": port, "ToPort": port}
    if ipv6:
      perm["Ipv6Ranges"] = [{"CidrIpv6": c, "Description": desc} for c in chunk]
    else:
      perm["IpRanges"] = [{"CidrIp": c, "Description": desc} for c in chunk]
    print(json.dumps([perm]))

emit(ipv4, False)
emit(ipv6, True)
PY
  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --ip-permissions "$perm"
done

echo "Updated SSH ingress rules for $SG_ID (port $PORT, description '$DESC')."
