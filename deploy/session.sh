#!/usr/bin/env bash
# Runs one process of a Jeff instance inside its tmux session (deploy/run.sh starts each session with it):
#   deploy/session.sh <env file> <log file> <command> [argument ...]
# The command gets the env file's settings and no other JEFF_* variable (the tmux server's environment may hold some
# from whichever shell started it). Its output is appended to the log and shown in the session. Secrets stay in the
# env file: they never appear on a command line.
set -uo pipefail
script=deploy/session.sh
# shellcheck source=deploy/instance.sh
. "$(dirname "${BASH_SOURCE[0]}")/instance.sh"
env_file=$1
log=$2
shift 2
drop_jeff_variables
set -a
# shellcheck disable=SC1090
. "$env_file"
set +a
echo "=== $(date '+%Y-%m-%d %H:%M:%S') $*" >>"$log"
"$@" 2>&1 | tee -a "$log"
