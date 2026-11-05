"""The repository-boundary grep for the JavaScript workspace.

No `package.json`, `vite.config.ts`, `tsconfig*.json` or `*.css` file in this
repository may name `open_teamsuzie` (a separate, private library this repository
must not depend on) or hold a relative path that resolves outside the repository
root. See .superpowers/sdd/2026-10-23-step2a4-console/global-constraints.md.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

FORBIDDEN_NAME = "open_teamsuzie"

# A quoted string containing a ".." path segment (not just the substring "..", so a
# stray ellipsis or version range never trips this).
OUTSIDE_SEGMENT = re.compile(r"(?:^|/)\.\.(?:/|$)")
QUOTED_STRING = re.compile(r"""['"]([^'"]*)['"]""")

IGNORED_DIR_NAMES = {"node_modules", ".venv", ".git", "dist", "__pycache__"}

PATTERNS = ("package.json", "vite.config.ts", "tsconfig*.json", "*.css")


def _scanned_files() -> list[Path]:
    found: set[Path] = set()
    for pattern in PATTERNS:
        for path in REPO_ROOT.rglob(pattern):
            if any(part in IGNORED_DIR_NAMES for part in path.parts):
                continue
            found.add(path)
    return sorted(found)


def _quoted_strings(text: str) -> list[str]:
    return QUOTED_STRING.findall(text)


def test_scans_at_least_the_brand_and_ui_workspace_files():
    # A canary against the glob silently matching nothing (a typo'd pattern, an
    # empty repository checkout): this task always ships these two files.
    scanned = _scanned_files()
    assert REPO_ROOT / "packages" / "brand" / "package.json" in scanned
    assert REPO_ROOT / "packages" / "ui" / "package.json" in scanned


def test_no_file_names_open_teamsuzie():
    violations = []
    for path in _scanned_files():
        text = path.read_text()
        if FORBIDDEN_NAME in text:
            line = next(i + 1 for i, l in enumerate(text.splitlines()) if FORBIDDEN_NAME in l)
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line} names {FORBIDDEN_NAME!r}")
    assert not violations, "\n".join(violations)


def test_no_relative_path_resolves_outside_the_repository():
    violations = []
    for path in _scanned_files():
        text = path.read_text()
        for candidate in _quoted_strings(text):
            if not OUTSIDE_SEGMENT.search(candidate):
                continue
            resolved = (path.parent / candidate).resolve()
            if resolved != REPO_ROOT and REPO_ROOT not in resolved.parents:
                violations.append(f"{path.relative_to(REPO_ROOT)}: {candidate!r} resolves to {resolved}, outside {REPO_ROOT}")
    assert not violations, "\n".join(violations)
