#!/usr/bin/env bash
# Checks that the committed console build (apps/server/src/jeff_server/ui) matches what building the console's
# source produces right now. Installs the pinned node_modules (pnpm install --frozen-lockfile, so this never
# silently builds against an un-pinned dependency), builds into a fresh temporary folder (so the real committed
# build is never touched by this check), and diffs the two trees. Any difference -- a forgotten `pnpm --filter
# jeff-console build` after a source change, or a build that is not reproducible -- fails loudly, naming every
# file that differs, rather than letting the console serve stale assets.
#
# The build bakes in VITE_JEFF_DOCS_URL (the public hub's docs URL, which the rail's "Documentation" item links
# to); vite.config.ts refuses to build without it, so this script requires it too, from the caller's environment,
# and must be given the same URL the committed build was made with, or the diff below will report every file as
# changed.
#
# Run from anywhere; also run by the slow pytest apps/server/tests/test_console_build.py.
set -euo pipefail

: "${VITE_JEFF_DOCS_URL:?Set VITE_JEFF_DOCS_URL to the public hub docs URL used for the committed build, for example VITE_JEFF_DOCS_URL=https://jeff.example.com/docs/guides/introduction scripts/check_console_build.sh}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT/apps/server/src/jeff_server/ui"
FRESH_DIR="$(mktemp -d)"
trap 'rm -rf "$FRESH_DIR"' EXIT

cd "$ROOT"
pnpm install --frozen-lockfile
VITE_JEFF_DOCS_URL="$VITE_JEFF_DOCS_URL" pnpm --filter jeff-console exec vite build --outDir "$FRESH_DIR" --emptyOutDir

if ! diff -r "$FRESH_DIR" "$BUILD_DIR"; then
  echo >&2
  echo "The committed console build ($BUILD_DIR) differs from a fresh build of its source (diff above)." >&2
  echo "Rebuild and commit it: pnpm install --frozen-lockfile && pnpm --filter jeff-console build" >&2
  exit 1
fi

echo "The committed console build matches a fresh build of its source."
