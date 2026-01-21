#!/usr/bin/env bash
# Local demo script - starts 5 nodes in tmux panes for easy demonstration
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/../src"
SESSION_NAME="demo"

# Kill existing session if exists
tmux has-session -t $SESSION_NAME 2>/dev/null && tmux kill-session -t $SESSION_NAME

# Create new tmux session with first node
cd "$SRC_DIR"
tmux new-session -d -s $SESSION_NAME -n nodes "python3 node.py --id 1 --peers peers.json"

# Split and add remaining nodes
tmux split-window -t $SESSION_NAME:nodes -h "cd $SRC_DIR && python3 node.py --id 2 --peers peers.json"
tmux split-window -t $SESSION_NAME:nodes -v "cd $SRC_DIR && python3 node.py --id 3 --peers peers.json"
tmux select-pane -t $SESSION_NAME:nodes.0
tmux split-window -t $SESSION_NAME:nodes -v "cd $SRC_DIR && python3 node.py --id 4 --peers peers.json"
tmux select-pane -t $SESSION_NAME:nodes.2
tmux split-window -t $SESSION_NAME:nodes -v "cd $SRC_DIR && python3 node.py --id 5 --peers peers.json"

# Set layout for better visibility
tmux select-layout -t $SESSION_NAME:nodes tiled

echo "=================================================="
echo "  Demo session started in tmux!"
echo "=================================================="
echo ""
echo "To attach to the demo session:"
echo "  tmux attach -t $SESSION_NAME"
echo ""
echo "Commands available in each node pane:"
echo "  req     - Request critical section (mutex)"
echo "  elect   - Start leader election"
echo "  status  - Show current state"
echo "  quit    - Kill the node"
echo ""
echo "Tips for demo:"
echo "  1. Watch for LEADER_UPDATE - Node 5 should become leader"
echo "  2. Type 'req' in multiple panes to test mutex"
echo "  3. Type 'quit' in Node 5 pane to test leader election"
echo "  4. Press Ctrl+B then arrow keys to switch panes"
echo ""
echo "To kill the demo:"
echo "  tmux kill-session -t $SESSION_NAME"
echo "=================================================="
