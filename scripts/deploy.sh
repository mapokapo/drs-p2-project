#!/bin/bash
NODE_ID=$1

# Wait for apt lock to be released (in case user_data is still running)
while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 1; done

# Kill existing process
pkill -f node.py || true
tmux has-session -t node 2>/dev/null && tmux kill-session -t node || true

# Start new session
tmux new-session -d -s node "USE_CLOUDWATCH=true AWS_REGION=us-east-1 python3 node.py --id $NODE_ID --peers peers.json | tee node.log"
sleep 1
