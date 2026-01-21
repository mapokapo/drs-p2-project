#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <node_id> <command>"
  echo "Example: $0 1 elect"
  exit 1
fi

NODE_ID="$1"
COMMAND="$2"

KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/labsuser.pem}"
SSH_USER="${SSH_USER:-ubuntu}"
TMUX_SESSION="${TMUX_SESSION:-node}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/../terraform"

if ! command -v terraform >/dev/null 2>&1; then
  echo "terraform is required on PATH."
  exit 1
fi

TF_OUTPUT=$(cd "$TERRAFORM_DIR" && terraform output -json node_ips)

if [[ -z "$TF_OUTPUT" ]]; then
  echo "Error: Failed to get terraform output."
  exit 1
fi

NODE_IP=$(echo "$TF_OUTPUT" | python3 - "$NODE_ID" <<'PY'
import json
import sys

node_id = sys.argv[1]
try:
    data = json.load(sys.stdin)
    key = f"Node {node_id}"
    ip = data.get(key)
    if not ip:
        sys.stderr.write(f"Node {node_id} not found in terraform output.\n")
        sys.exit(2)
    print(ip)
except Exception as e:
    sys.stderr.write(f"Error parsing JSON: {e}\n")
    sys.exit(1)
PY
)

ssh -i "$KEY_PATH" "$SSH_USER@$NODE_IP" "tmux send-keys -t $TMUX_SESSION '$COMMAND' C-m"
