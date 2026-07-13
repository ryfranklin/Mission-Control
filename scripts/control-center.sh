#!/usr/bin/env bash
#
# control-center.sh — power up the Mission Control tmux workspace.
#
# Layout (session: "mission-control"):
#
#   ┌───────────────────────┬───────────────────┐
#   │                       │      terminal      │
#   │   claude              ├───────────────────┤
#   │   (agent session)     │     operations     │
#   │                       │  (venv + runtime)  │
#   └───────────────────────┴───────────────────┘
#
#   • claude      — an interactive Claude Code session
#   • terminal    — a plain shell for ad-hoc commands
#   • operations  — venv activated, ready to run the runtime / eval-gate
#
# Usage:
#   ./scripts/control-center.sh          # create + attach (or attach if it exists)
#   ./scripts/control-center.sh kill     # tear the session down

set -euo pipefail

SESSION="mission-control"
# Repo root = parent of this script's directory, resolved regardless of CWD.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed. Install it with:  brew install tmux" >&2
  exit 1
fi

# Teardown mode.
if [[ "${1:-}" == "kill" ]]; then
  tmux kill-session -t "$SESSION" 2>/dev/null && echo "Scrubbed session '$SESSION'." \
    || echo "No session '$SESSION' to scrub."
  exit 0
fi

# If it already exists, just attach (or switch, if we're already inside tmux).
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' already running — attaching."
  if [[ -n "${TMUX:-}" ]]; then
    exec tmux switch-client -t "$SESSION"
  else
    exec tmux attach-session -t "$SESSION"
  fi
fi

# Command that activates the venv if present, then leaves you at an interactive shell.
VENV_ACTIVATE="[ -f '$REPO_ROOT/.venv/bin/activate' ] && source '$REPO_ROOT/.venv/bin/activate'; exec \$SHELL"

# --- Build the session (detached), then attach at the end. ---

# claude session (left). Capture pane IDs (#{pane_id} → stable %N handles) so
# nothing here assumes a pane-base-index; works whether it's 0 or 1.
tmux new-session -d -s "$SESSION" -n control -c "$REPO_ROOT"
claude_pane="$(tmux list-panes -t "$SESSION:control" -F '#{pane_id}' | head -1)"
tmux send-keys -t "$claude_pane" \
  "command -v claude >/dev/null 2>&1 && claude || { echo 'claude CLI not found on PATH'; exec \$SHELL; }" C-m

# Split off the right column → terminal (top-right).
term_pane="$(tmux split-window -h -t "$claude_pane" -c "$REPO_ROOT" -P -F '#{pane_id}')"

# Split the right column vertically → operations (bottom-right).
ops_pane="$(tmux split-window -v -t "$term_pane" -c "$REPO_ROOT" -P -F '#{pane_id}')"
tmux send-keys -t "$ops_pane" "$VENV_ACTIVATE" C-m

# Give the claude pane the most room.
tmux select-layout -t "$SESSION:control" main-vertical
tmux resize-pane -t "$claude_pane" -x 55%

# Label panes (visible when pane borders show titles) and land on the claude pane.
tmux select-pane -t "$claude_pane" -T claude
tmux select-pane -t "$term_pane"   -T terminal
tmux select-pane -t "$ops_pane"    -T operations
tmux set-option -t "$SESSION" pane-border-status top >/dev/null 2>&1 || true
tmux select-pane -t "$claude_pane"

if [[ -n "${TMUX:-}" ]]; then
  exec tmux switch-client -t "$SESSION"
else
  exec tmux attach-session -t "$SESSION"
fi
