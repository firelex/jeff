#!/usr/bin/env bash
# Copies this Mac working tree to the RTX machine (roli79@datigator:~/jeff-dev-next/) so CUDA tests and benchmarks run
# on exactly the code being edited here. Code is edited only on the Mac; the copy on datigator is a mirror.
#
# - rsync -a --delete: files deleted here are deleted there. Never copied (and never deleted there): .git, .venv,
#   node_modules, __pycache__, .mypy_cache, .pytest_cache and results/ (results produced on datigator are copied
#   back explicitly, by name).
# - Prints the commit the tree is synced from, and `git status --short` when the tree has uncommitted changes.
# - Needs the Mac's SSH agent: it refuses to run when the agent socket is missing.
#
# After a sync that changed pyproject.toml or uv.lock, run `~/.local/bin/uv sync` in ~/jeff-dev-next on datigator.
#
# Usage: scripts/sync_rtx.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="roli79@datigator:~/jeff-dev-next/"

# Shells started by tools often lack SSH_AUTH_SOCK; the Mac's launchd agent socket is then the one to use.
if [[ -z "${SSH_AUTH_SOCK:-}" ]]; then
  SSH_AUTH_SOCK="$(ls -d /private/tmp/com.apple.launchd.*/Listeners 2>/dev/null | head -1)"
  if [[ -z "$SSH_AUTH_SOCK" ]]; then
    echo "No SSH agent socket: SSH_AUTH_SOCK is unset and no /private/tmp/com.apple.launchd.*/Listeners exists." >&2
    echo "Start the Mac's SSH agent with the datigator key loaded, then run this again." >&2
    exit 1
  fi
  export SSH_AUTH_SOCK
fi
if [[ ! -S "$SSH_AUTH_SOCK" ]]; then
  echo "No SSH agent socket at SSH_AUTH_SOCK=$SSH_AUTH_SOCK (missing or not a socket)." >&2
  echo "Start the Mac's SSH agent with the datigator key loaded, then run this again." >&2
  exit 1
fi

cd "$ROOT"
echo "Syncing $ROOT to $DEST from commit $(git rev-parse --short HEAD) ($(git log -1 --format=%s))"
dirty="$(git status --short)"
if [[ -n "$dirty" ]]; then
  echo "The tree has uncommitted changes, synced as they are:"
  echo "$dirty"
fi

rsync -a --delete \
  --exclude .git \
  --exclude .venv \
  --exclude node_modules \
  --exclude __pycache__ \
  --exclude .mypy_cache \
  --exclude .pytest_cache \
  --exclude /results/ \
  "$ROOT/" "$DEST"
echo "Synced."
