"""The committed console build (apps/server/src/jeff_server/ui) against its source (apps/server/ui): Review Focus
3, a drifted build that was never rebuilt and recommitted after a source change. scripts/check_console_build.sh
does the actual work (install the pinned node_modules, build the console's source into a fresh temporary folder,
diff it against the committed build); this test only runs that script and fails on anything but a clean exit.

Marked slow (a `pnpm install` and a full console build, same reason the model tests are slow: not something to run
on every `uv run pytest`) -- run explicitly with `uv run pytest -m slow apps/server/tests/test_console_build.py`.
test_console.py's test_the_committed_build_references_only_assets_it_has_and_serves_them is the fast counterpart
(every asset the committed index.html references exists and is served), so that check is not duplicated here."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_console_build.sh"


@pytest.mark.slow
def test_the_committed_console_build_matches_a_fresh_build_of_its_source() -> None:
    result = subprocess.run([str(SCRIPT)], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
