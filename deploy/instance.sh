# shellcheck shell=bash
# The callers set `script` and use the session names set here.
# shellcheck disable=SC2154,SC2034
# Sourced by deploy/run.sh and deploy/stop.sh (set `script` to the caller's name first): reads an instance's env file
# and names its tmux sessions.

fail() {
  echo "$script: $*" >&2
  exit 1
}

# Every JEFF_* variable exported here, so one inherited from the caller's shell can be dropped: an instance's settings
# come from its env file alone, as they do inside its sessions (deploy/session.sh).
drop_jeff_variables() {
  local name
  for name in $(compgen -e); do
    case $name in JEFF_*) unset "$name" ;; esac
  done
}

# load_env FILE: the settings in FILE, exported; env_file is its absolute path, which the sessions read again.
load_env() {
  [ -f "$1" ] || fail "no env file at $1"
  env_file="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
  drop_jeff_variables
  set -a
  # shellcheck disable=SC1090
  . "$env_file"
  set +a
}

# require NAME...: each must be set to a non-empty value in the env file.
require() {
  local name
  for name in "$@"; do
    [ -n "${!name:-}" ] || fail "set $name in $env_file"
  done
}

# tmux_clean ARGUMENT...: tmux with no JEFF_* variable and no Hugging Face token in its environment. run.sh exports
# the env file's settings, keys included, and the first tmux command starts the tmux server, which keeps the
# environment it started with for as long as it runs; so every tmux command goes through here, and the keys stay in
# the env file, which each session reads for itself (deploy/session.sh).
tmux_clean() {
  local name
  local drop=()
  for name in $(compgen -e); do
    case $name in JEFF_* | HF_TOKEN | HUGGING_FACE_HUB_TOKEN) drop+=(-u "$name") ;; esac
  done
  env ${drop[@]+"${drop[@]}"} tmux "$@"
}

# has_session NAME: the session exists. The '=' asks tmux for exactly this name; without it, tmux would also accept
# a session whose name merely starts with NAME.
has_session() {
  tmux_clean has-session -t "=$1" 2>/dev/null
}

# The instance's sessions, from JEFF_SESSION_PREFIX and JEFF_GPUS: gpus (the engines' names: GPU indices, or mlx),
# engine_sessions (one per entry of gpus, in the same order), api_session and factory_session.
name_sessions() {
  require JEFF_SESSION_PREFIX JEFF_GPUS
  [[ $JEFF_SESSION_PREFIX =~ ^[A-Za-z0-9_-]+$ ]] ||
    fail "JEFF_SESSION_PREFIX=$JEFF_SESSION_PREFIX: use letters, digits, - and _ only (tmux renames sessions with . or :)"
  if [ "$JEFF_GPUS" = mlx ]; then
    gpus=(mlx)
  elif [[ $JEFF_GPUS =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    IFS=, read -r -a gpus <<<"$JEFF_GPUS"
    [ -z "$(printf '%s\n' "${gpus[@]}" | sort | uniq -d)" ] || fail "JEFF_GPUS=$JEFF_GPUS names a GPU twice"
  else
    fail "JEFF_GPUS=$JEFF_GPUS: list GPU indices separated by commas (for example 0,1), or write mlx on a Mac"
  fi
  engine_sessions=()
  local gpu
  for gpu in "${gpus[@]}"; do
    engine_sessions+=("$JEFF_SESSION_PREFIX-engine-$gpu")
  done
  api_session=$JEFF_SESSION_PREFIX-api
  factory_session=$JEFF_SESSION_PREFIX-factory
}
