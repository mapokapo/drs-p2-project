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

# Debug info
echo "DEBUG: Fetching IP for Node $NODE_ID..."
echo "DEBUG: Terraform Dir: $TERRAFORM_DIR"

# switch to terraform dir to capture output
pushd "$TERRAFORM_DIR" > /dev/null
TF_OUTPUT=$(terraform output -json node_ips)
TF_EXIT=$?
popd > /dev/null

if [[ $TF_EXIT -ne 0 ]]; then
    echo "Error: 'terraform output' command failed with exit code $TF_EXIT"
    exit 1
fi

# Trim whitespace and check
if [[ -z "${TF_OUTPUT//[[:space:]]/}" ]]; then
  echo "Error: Terraform output was empty or whitespace only."
  echo "DEBUG: Raw output: '$TF_OUTPUT'"
  exit 1
fi

# Create a permanent debugging block regarding the output content
echo "DEBUG: Terraform output captured (length: ${#TF_OUTPUT} chars)" >&2

# Use a temp file to pass data to python to avoid pipe issues
TMP_JSON="/tmp/node_ips_$$.json"
echo "$TF_OUTPUT" > "$TMP_JSON"

NODE_IP=$(python3 - "$NODE_ID" "$TMP_JSON" <<'PY'
import json
import sys

node_id = sys.argv[1]
json_file = sys.argv[2]

try:
    with open(json_file, 'r') as f:
        content = f.read()
        
    if not content.strip():
        raise ValueError("Empty input in temp file")
    
    data = json.loads(content)
    
    key = f"Node {node_id}"
    ip = data.get(key)
    if not ip:
        sys.stderr.write(f"Node {node_id} not found in terraform output keys: {list(data.keys())}\n")
        sys.exit(2)
    print(ip)
except Exception as e:
    sys.stderr.write(f"Error parsing JSON from file: {e}\nInput content preview: {content[:100]!r}\n")
    sys.exit(1)
PY
)

rm -f "$TMP_JSON"

ssh -o StrictHostKeyChecking=no -i "$KEY_PATH" "$SSH_USER@$NODE_IP" "tmux send-keys -t $TMUX_SESSION '$COMMAND' C-m"
