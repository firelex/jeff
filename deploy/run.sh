#!/usr/bin/env bash
# Starts one Jeff instance in tmux, from this checkout's .venv, with the settings in its env file:
#   deploy/run.sh <env file>
# One engine per GPU (or one MLX engine on a Mac), then the HTTP API in front of them, then, with JEFF_FACTORY=owner,
# the factory worker on JEFF_FACTORY_GPU. Sessions: <prefix>-engine-<gpu> ..., <prefix>-api, <prefix>-factory; each
# one logs to ~/<session>.log. It returns once every engine and the API answer. See deploy/README.md and the env
# examples next to this script.
#
# It never stops anything: a session of the instance that already exists, or an engine already answering at one of
# its sockets, is refused. Stop the instance with deploy/stop.sh <env file>.
set -euo pipefail
script=deploy/run.sh
deploy="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/instance.sh
. "$deploy/instance.sh"
[ $# -eq 1 ] || fail "usage: deploy/run.sh <env file>"
repo="$(dirname "$deploy")"
bin=$repo/.venv/bin

load_env "$1"
require JEFF_SESSION_PREFIX JEFF_GPUS JEFF_SOCKET_DIR JEFF_SERVED_FILE_DIR JEFF_ADAPTER_DIRS JEFF_BASE_VERSION \
  JEFF_HOST JEFF_PORT JEFF_WORKERS JEFF_BATCH_WINDOW_MS JEFF_MAX_BATCH_ROWS JEFF_QUEUE_LIMIT_ROWS JEFF_PREPARE_WORKERS \
  JEFF_ENGINE_START_S JEFF_API_START_S
name_sessions
[[ $JEFF_ENGINE_START_S =~ ^[1-9][0-9]*$ ]] ||
  fail "JEFF_ENGINE_START_S=$JEFF_ENGINE_START_S: give the seconds to wait for each engine as a whole number above 0"
[[ $JEFF_API_START_S =~ ^[1-9][0-9]*$ ]] ||
  fail "JEFF_API_START_S=$JEFF_API_START_S: give the seconds to wait for the API as a whole number above 0"
# Every engine (the MLX one too) prepares requests on arrival in a pool of JEFF_PREPARE_WORKERS threads, each with its
# own tokenizer: checking every request is CPU work on every backend, and on the GPU it includes tokenising.
[[ $JEFF_PREPARE_WORKERS =~ ^[1-9][0-9]*$ ]] ||
  fail "JEFF_PREPARE_WORKERS=$JEFF_PREPARE_WORKERS: give how many requests each engine prepares at once, a whole number above 0"
# The engines on NVIDIA GPUs run the fast backend, which allocates room for JEFF_MAX_ADAPTERS adapters and a prefix
# pool of JEFF_PREFIX_POOL_MB megabytes at start; the MLX engine has neither, so there the settings would do nothing
# and are refused. (jeff runtime serve checks that the pool holds JEFF_MAX_BATCH_ROWS prefixes of 8,192 tokens.)
if [ "$JEFF_GPUS" = mlx ]; then
  [ -z "${JEFF_MAX_ADAPTERS:-}" ] ||
    fail "JEFF_MAX_ADAPTERS only applies to engines on NVIDIA GPUs (the fast backend); the MLX engine (JEFF_GPUS=mlx) has no such limit, so remove it"
  [ -z "${JEFF_PREFIX_POOL_MB:-}" ] ||
    fail "JEFF_PREFIX_POOL_MB only applies to engines on NVIDIA GPUs (the fast backend); the MLX engine (JEFF_GPUS=mlx) has no prefix pool, so remove it"
else
  require JEFF_MAX_ADAPTERS JEFF_PREFIX_POOL_MB
  [[ $JEFF_MAX_ADAPTERS =~ ^[1-9][0-9]*$ ]] ||
    fail "JEFF_MAX_ADAPTERS=$JEFF_MAX_ADAPTERS: give the most adapters each engine holds at once, a whole number above 0"
  [[ $JEFF_PREFIX_POOL_MB =~ ^[1-9][0-9]*$ ]] ||
    fail "JEFF_PREFIX_POOL_MB=$JEFF_PREFIX_POOL_MB: give each engine's prefix pool in MB, a whole number (8192 holds 64 rows of 8,192-token prefixes)"
fi
case ${JEFF_FACTORY:-off} in
  owner)
    sessions=("${engine_sessions[@]}" "$api_session" "$factory_session")
    require JEFF_FACTORY_GPU
    if [ "$JEFF_GPUS" = mlx ]; then
      [ "$JEFF_FACTORY_GPU" = mlx ] ||
        fail "JEFF_FACTORY_GPU=$JEFF_FACTORY_GPU: with JEFF_GPUS=mlx there is no CUDA GPU; write mlx (the factory cannot train there)"
    else
      [[ $JEFF_FACTORY_GPU =~ ^[0-9]+$ ]] ||
        fail "JEFF_FACTORY_GPU=$JEFF_FACTORY_GPU: give the one GPU index the factory worker trains on (for example 1)"
    fi
    ;;
  off)
    sessions=("${engine_sessions[@]}" "$api_session")
    [ -z "${JEFF_FACTORY_GPU:-}" ] || fail "JEFF_FACTORY_GPU only applies with JEFF_FACTORY=owner; remove it"
    ;;
  *) fail "JEFF_FACTORY=$JEFF_FACTORY: use owner to run the factory worker, or off" ;;
esac
[ -z "${JEFF_ENGINE_SOCKETS:-}" ] ||
  fail "JEFF_ENGINE_SOCKETS is set in $env_file; run.sh sets it for the API from JEFF_SOCKET_DIR and JEFF_GPUS, so remove it"
start_adapters=()
if [ -n "${JEFF_START_ADAPTERS:-}" ]; then
  [[ $JEFF_START_ADAPTERS =~ ^[^,]+(,[^,]+)*$ ]] ||
    fail "JEFF_START_ADAPTERS=$JEFF_START_ADAPTERS: list adapters separated by single commas"
  IFS=, read -r -a start_adapters <<<"$JEFF_START_ADAPTERS"
fi
# JEFF_BASE_FOLDER (optional): an INT4 base folder (jeff runtime quantize) that each GPU engine serves instead of the
# bfloat16 base. An engine refuses it, naming the adapter and the command, unless the folder's gate.json (jeff runtime
# gate) passes for every adapter it serves. INT4 needs the fast backend, so it is refused with JEFF_GPUS=mlx.
base_folder=()
if [ -n "${JEFF_BASE_FOLDER:-}" ]; then
  [ "$JEFF_GPUS" != mlx ] ||
    fail "JEFF_BASE_FOLDER serves an INT4 base, which needs NVIDIA GPUs (the fast backend); the MLX engine (JEFF_GPUS=mlx) serves the bfloat16 base only, so remove it"
  [ -d "$JEFF_BASE_FOLDER" ] || fail "JEFF_BASE_FOLDER=$JEFF_BASE_FOLDER is not a folder; give an INT4 base folder"
  base_folder=(--base "$JEFF_BASE_FOLDER")
fi
for name in JEFF_SOCKET_DIR JEFF_SERVED_FILE_DIR; do
  [ -d "${!name}" ] || fail "$name=${!name} is not a folder; create it first"
done
IFS=: read -r -a adapter_dirs <<<"$JEFF_ADAPTER_DIRS"
for folder in "${adapter_dirs[@]}"; do
  [ -n "$folder" ] && [ -d "$folder" ] ||
    fail "JEFF_ADAPTER_DIRS=$JEFF_ADAPTER_DIRS names '$folder', which is not a folder; create it or leave it out"
done
for command in "$bin/jeff" "$bin/jeff-server"; do
  [ -x "$command" ] || fail "no $command: run uv sync in $repo first (see deploy/README.md)"
done
command -v tmux >/dev/null || fail "tmux is not installed"
command -v curl >/dev/null || fail "curl is not installed; run.sh checks the API's /health with it"

for session in "${sessions[@]}"; do
  ! has_session "$session" || fail "the tmux session $session exists; stop it first with deploy/stop.sh $1"
done
sockets=()
for gpu in "${gpus[@]}"; do
  sockets+=("$JEFF_SOCKET_DIR/engine-$gpu.sock")
done
for socket in "${sockets[@]}"; do
  if [ -S "$socket" ] && "$bin/jeff" runtime health --socket "$socket" --timeout-s 5 >/dev/null 2>&1; then
    fail "an engine already answers at $socket, outside this instance's sessions; stop it first"
  fi
done

# start SESSION COMMAND...: COMMAND runs in a new detached session, through deploy/session.sh.
start() {
  local session=$1
  shift
  tmux_clean new-session -d -s "$session" -c "$repo" "$deploy/session.sh" "$env_file" "$HOME/$session.log" "$@"
  echo "started $session; log: $HOME/$session.log"
}

for i in "${!gpus[@]}"; do
  gpu=${gpus[$i]}
  served=$JEFF_SERVED_FILE_DIR/served-$gpu.json
  engine=()
  [ "$gpu" = mlx ] || engine=(env "CUDA_VISIBLE_DEVICES=$gpu")
  engine+=("$bin/jeff" runtime serve --socket "${sockets[$i]}" --served-file "$served")
  for folder in "${adapter_dirs[@]}"; do
    engine+=(--adapters-dir "$folder")
  done
  engine+=(--base-version "$JEFF_BASE_VERSION" --batch-window-ms "$JEFF_BATCH_WINDOW_MS"
    --max-batch-rows "$JEFF_MAX_BATCH_ROWS" --queue-limit-rows "$JEFF_QUEUE_LIMIT_ROWS"
    --prepare-workers "$JEFF_PREPARE_WORKERS")
  [ "$gpu" = mlx ] || engine+=(--max-adapters "$JEFF_MAX_ADAPTERS" --prefix-pool-mb "$JEFF_PREFIX_POOL_MB"
    ${base_folder[@]+"${base_folder[@]}"})
  # The served file, once it exists, is the engine's list of adapters to start with; the engine refuses --adapter
  # beside it. Without one (a first start), JEFF_START_ADAPTERS, or no adapter at all.
  if [ -e "$served" ]; then
    [ ${#start_adapters[@]} -eq 0 ] ||
      echo "$served exists: ${engine_sessions[$i]} starts from its list, not from JEFF_START_ADAPTERS"
  else
    for adapter in ${start_adapters[@]+"${start_adapters[@]}"}; do
      engine+=(--adapter "$adapter")
    done
  fi
  start "${engine_sessions[$i]}" "${engine[@]}"
done

stop_hint="the sessions started so far are still running; stop them with deploy/stop.sh $1"
# Each engine must answer before the API starts in front of them: loading the base takes from seconds to minutes.
for i in "${!gpus[@]}"; do
  session=${engine_sessions[$i]}
  log=$HOME/$session.log
  deadline=$((SECONDS + JEFF_ENGINE_START_S))
  while true; do
    has_session "$session" || fail "the engine session $session ended while starting; see $log ($stop_hint)"
    if answer=$("$bin/jeff" runtime health --socket "${sockets[$i]}" --timeout-s 5 2>&1); then
      echo "$session is ready"
      break
    fi
    [ "$SECONDS" -lt "$deadline" ] ||
      fail "the engine $session did not answer at ${sockets[$i]} within ${JEFF_ENGINE_START_S}s ($answer); see $log ($stop_hint)"
    sleep 1
  done
done

engine_sockets=$(IFS=:; echo "${sockets[*]}")
start "$api_session" env "JEFF_ENGINE_SOCKETS=$engine_sockets" "$bin/jeff-server" serve --host "$JEFF_HOST" \
  --port "$JEFF_PORT" --workers "$JEFF_WORKERS"

# The API must answer /health before run.sh reports success. An API listening on every address (0.0.0.0 or ::) is
# asked on this machine's loopback address.
case $JEFF_HOST in
  0.0.0.0) api_host=127.0.0.1 ;;
  :: | "[::]") api_host=::1 ;;
  *) api_host=$JEFF_HOST ;;
esac
case $api_host in
  \[*) ;;
  *:*) api_host="[$api_host]" ;;
esac
api_url=http://$api_host:$JEFF_PORT/health
api_log=$HOME/$api_session.log
deadline=$((SECONDS + JEFF_API_START_S))
while true; do
  has_session "$api_session" || fail "the API session $api_session ended while starting; see $api_log ($stop_hint)"
  if answer=$(curl -fsS --max-time 5 "$api_url" 2>&1); then
    echo "$api_session answers at $api_url"
    break
  fi
  [ "$SECONDS" -lt "$deadline" ] ||
    fail "the API $api_session did not answer at $api_url within ${JEFF_API_START_S}s ($answer); see $api_log ($stop_hint)"
  sleep 1
done

if [ "${JEFF_FACTORY:-off}" = owner ]; then
  worker=()
  [ "$JEFF_FACTORY_GPU" = mlx ] || worker=(env "CUDA_VISIBLE_DEVICES=$JEFF_FACTORY_GPU")
  worker+=("$bin/jeff-server" factory worker)
  for socket in "${sockets[@]}"; do
    worker+=(--engine-socket "$socket")
  done
  start "$factory_session" "${worker[@]}"
fi
