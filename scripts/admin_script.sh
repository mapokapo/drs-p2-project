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
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/../terraform"

if ! command -v terraform >/dev/null 2>&1; then
  echo "terraform is required on PATH."
  exit 1
fi

if [[ ! -d "$TERRAFORM_DIR" ]]; then
    echo "Error: Terraform directory not found at $TERRAFORM_DIR"
    exit 1
fi

TF_OUTPUT=$(cd "$TERRAFORM_DIR" && terraform output -json node_ips)

if [[ -z "$TF_OUTPUT" ]]; then
  echo "Error: Failed to get terraform output from directory $TERRAFORM_DIR"
  exit 1
fi

NODE_IP=$(echo "$TF_OUTPUT" | python3 - "$NODE_ID" <<'PY'
import json
import sys

node_id = sys.argv[1]
content = ""
try:
    content = sys.stdin.read()
    if not content.strip():
        raise ValueError("Empty input")
    data = json.loads(content)
    
    key = f"Node {node_id}"
    ip = data.get(key)
    if not ip:
        sys.stderr.write(f"Node {node_id} not found in terraform output keys: {list(data.keys())}\n")
        sys.exit(2)
    print(ip)
except Exception as e:
    sys.stderr.write(f"Error parsing JSON: {e}\nInput content preview: {content[:100]!r}\n")
    sys.exit(1)
PY
)

ssh -i "$KEY_PATH" "$SSH_USER@$NODE_IP" "tmux send-keys -t $TMUX_SESSION '$COMMAND' C-m"
