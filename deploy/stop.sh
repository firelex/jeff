#!/usr/bin/env bash
# Stops the Jeff instance deploy/run.sh started with the same env file, one tmux session at a time:
#   deploy/stop.sh <env file>
# Only `tmux kill-session` of the instance's own sessions, by exact name (the factory worker, the API, then each
# engine); it never looks for processes by pattern. A session that is not running is reported and skipped.
set -euo pipefail
script=deploy/stop.sh
# shellcheck source=deploy/instance.sh
. "$(dirname "${BASH_SOURCE[0]}")/instance.sh"
[ $# -eq 1 ] || fail "usage: deploy/stop.sh <env file>"
load_env "$1"
name_sessions
# The factory session whatever JEFF_FACTORY says now: it may have been on when the instance started.
for session in "$factory_session" "$api_session" "${engine_sessions[@]}"; do
  if has_session "$session"; then
    tmux_clean kill-session -t "=$session"
    echo "stopped $session"
  else
    echo "$session is not running"
  fi
done
