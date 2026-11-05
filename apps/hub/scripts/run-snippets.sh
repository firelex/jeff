#!/usr/bin/env bash
# Runs every SDK docs snippet (content/snippets/sdk/*.py) with `uv run python`, from a copy of that folder, so a snippet
# can read a file beside it (graphs-message-check.yaml) and anything it writes is thrown away. The ones whose first
# line is `# offline` also run in the ordinary test run (test/sdk-coverage.test.ts); the others call a Jeff server,
# so this script needs JEFF_HUB_TEST_URL (the server's URL) and JEFF_API_KEY (a token for it). Each snippet shows the API origin as {{origin}}, as the docs pages do; it is replaced here with
# JEFF_HUB_TEST_URL in the copy, which is what runs. Stops at the first snippet that fails, naming it.
set -euo pipefail

: "${JEFF_HUB_TEST_URL:?set JEFF_HUB_TEST_URL to the URL of the Jeff server the snippets call}"
: "${JEFF_API_KEY:?set JEFF_API_KEY to a token for that server}"

HUB="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$HUB/../.." && pwd)"
SNIPPETS="$HUB/content/snippets/sdk"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cp -R "$SNIPPETS/." "$WORK/"
cd "$WORK"
for snippet in *.py; do
  echo "== $snippet"
  sed -i.orig "s|{{origin}}|$JEFF_HUB_TEST_URL|g" "$snippet"
  if ! uv run --project "$REPO" python "$snippet"; then
    echo "run-snippets: $snippet failed" >&2
    exit 1
  fi
done
echo "run-snippets: every snippet ran"
