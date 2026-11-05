"""Publish jeff-dev's main, minus everything private, as one new commit on firelex/jeff (the public repository, whose
history and stars stay as they are). jeff-dev stays private and remains what Vercel and the servers build from.

Usage, from a clean checkout of jeff-dev's main:
    uv run python scripts/publish_mirror.py           # build and commit in a fresh clone; print how to push it
    uv run python scripts/publish_mirror.py --push    # the same, then push that commit to firelex/jeff main

What it does, failing loudly at the first problem:
1. Refuses unless jeff-dev is on main with no change to a tracked file (staged or not). Untracked files are only
   listed: the export is the commit, so they never reach it.
2. Exports the files of that commit, minus the rules of scripts/mirror-exclude.txt and every tracked file that
   .gitignore matches, plus the files of scripts/mirror-overlay/ (the hub's empty owner-only set).
3. Clones git@github.com:firelex/jeff.git into a new temporary folder and replaces its whole working tree with the
   export, so files jeff-dev no longer has are deleted too.
4. Scans every file of the result and refuses, naming each file and line, when one holds the name of a private
   adapter (its ID, an alias, a title it has or had on the hub, an owner-only example's id or name, or its data
   folder) or something that looks like a secret.
5. Commits as Mathias Strasser <mstrasser@scissero.com>: "Sync from jeff-dev <short sha>" and a short summary.
6. Prints the clone's path and the push command; pushes only with --push, and never forces.

The private names come only from files the public copy leaves out: deploy/client-names.txt (client names),
deploy/private-adapters.txt (the IDs),
results/v14-manifests/<id>/manifest.yaml (aliases, hub titles, data folders), apps/hub/content/owner-only (titles and
examples), the history of the hub's catalogue text (earlier titles) and catalogue-imports (data folders). This file
names none of them.

An alias that is one plain word (often also an English word, or a label key or domain of an open adapter) counts as a
name only where it is used as one: before a function key (<alias>.<key>), as an owner (<alias>'s), quoted ("<alias>",
'<alias>' or `<alias>`, unless the word is also a domain or an open adapter's label key), as a data folder
(jeff-data/apps/<alias>), an import file (catalogue-imports/<alias>.yaml) or an import's output (import-<alias>).
Every other name is matched whole: jeff.x.name does not match jeff.x.name-open, and a title matches as a phrase."""

import argparse
import fnmatch
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "git@github.com:firelex/jeff.git"
BRANCH = "main"
AUTHOR_NAME = "Mathias Strasser"
AUTHOR_EMAIL = "mstrasser@scissero.com"
EXCLUDE_FILE = "scripts/mirror-exclude.txt"
OVERLAY_DIR = "scripts/mirror-overlay"
PRIVATE_LIST = "deploy/private-adapters.txt"
SERVED_DIR = "results/v14-manifests"
OWNER_ONLY = "apps/hub/content/owner-only"
TEXT_FILES = ("apps/hub/content/catalogue-text.yaml", "apps/hub/content/owner-only/catalogue-text.yaml")
HIDDEN_MODULE = "apps/hub/app/lib/owner-only/hidden-adapters.generated.ts"
HIDDEN_MODULE_ARRAY = "export const HIDDEN_ADAPTERS: readonly unknown[] = "

PLAIN_WORD = re.compile(r"[a-z0-9]+")
CLIENT_NAMES = "deploy/client-names.txt"
DATA_FOLDER = re.compile(r"jeff-(?:data|adapters)(?:@[A-Za-z0-9._-]+)?/(apps/[A-Za-z0-9._/-]+?)/[A-Za-z0-9._-]+\.[a-z]+")

# Things that look like secrets. A test fixture with an obviously fake value is allowed only by its exact text in
# ALLOWED_SECRETS below, with the reason.
SECRET_PATTERNS = {
    "Stripe live key": re.compile(rb"sk_live_[A-Za-z0-9]{20,}"),
    "Stripe test key": re.compile(rb"sk_test_[A-Za-z0-9]{20,}"),
    "SendGrid key": re.compile(rb"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
    "Jeff API key": re.compile(rb"jeff_live_[a-z2-7]{52}"),
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    "private key": re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    # a literal value of 8 or more characters, as a settings line or a URL holds one (not code such as password=None)
    "password": re.compile(rb"password=[\"']?[A-Za-z0-9!@#%^*_+~./-]{8,}"),
}
# (path, the exact matched text): the reason it is not a secret.
ALLOWED_SECRETS: dict[tuple[str, str], str] = {}


class MirrorError(Exception):
    pass


def run(args: list[str], cwd: Path, capture: bool = True) -> str:
    """Run a command, failing with its output when it fails."""
    result = subprocess.run(args, cwd=cwd, capture_output=capture, text=True)
    if result.returncode != 0:
        output = (result.stderr or "") + (result.stdout or "") if capture else ""
        raise MirrorError(f"`{' '.join(args)}` in {cwd} failed ({result.returncode}): {output.strip()}")
    return result.stdout if capture else ""


# --- 1. the source ---------------------------------------------------------------------------------------------------

def check_source() -> str:
    """jeff-dev's main at a clean tree; returns the commit's full SHA."""
    branch = run(["git", "symbolic-ref", "--short", "HEAD"], ROOT).strip()
    if branch != BRANCH:
        raise MirrorError(f"jeff-dev is on {branch}, not {BRANCH}; check out {BRANCH} first")
    changed = [line for line in run(["git", "status", "--porcelain=v1", "--untracked-files=no"], ROOT).splitlines() if line]
    if changed:
        raise MirrorError("jeff-dev has changes to tracked files; commit or stash them first:\n" + "\n".join(changed))
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], ROOT).split()
    if untracked:
        print(f"Untracked files in jeff-dev (not part of the commit, so not exported): {', '.join(untracked)}")
    return run(["git", "rev-parse", "HEAD"], ROOT).strip()


def tracked_files() -> list[str]:
    return sorted(path for path in run(["git", "ls-files", "-z"], ROOT).split("\0") if path)


def open_ids() -> set[str]:
    folder = ROOT / "catalogue"
    ids = {path.name for path in folder.iterdir() if path.is_dir()}
    if not ids:
        raise MirrorError(f"{folder} has no adapter folders")
    return ids


# --- 2. what is left out ---------------------------------------------------------------------------------------------

def read_rules() -> list[tuple[str, str]]:
    """The rules of the exclude file, as (kind, value)."""
    path = ROOT / EXCLUDE_FILE
    if not path.is_file():
        raise MirrorError(f"{path} is missing")
    rules = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, value = line.partition(": ")
        if kind in ("open-adapters-only", "open-imports-only"):
            if not value.endswith("/"):
                raise MirrorError(f"{EXCLUDE_FILE}:{number}: {kind} takes a folder ending with /, not {value!r}")
            rules.append((kind, value))
        elif ": " in line:
            raise MirrorError(f"{EXCLUDE_FILE}:{number}: unknown rule {kind!r}")
        elif line.startswith("**/") and line.endswith("/"):
            rules.append(("any-folder", line[3:]))
        elif line.endswith("/"):
            rules.append(("folder", line))
        elif any(char in line for char in "*?["):
            rules.append(("pattern", line))
        else:
            rules.append(("file", line))
    return rules


def import_id(path: Path) -> str | None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MirrorError(f"{path}: not a YAML mapping")
    adapter_id = data.get("id")
    if adapter_id is not None and not isinstance(adapter_id, str):
        raise MirrorError(f"{path}: id is not a string")
    return adapter_id


def left_out(files: list[str], rules: list[tuple[str, str]], opens: set[str]) -> dict[str, str]:
    """Every tracked file the public copy leaves out, with the rule that leaves it out."""
    out: dict[str, str] = {}
    for kind, value in rules:
        matched = []
        for path in files:
            if kind == "folder":
                hit = path.startswith(value)
            elif kind == "any-folder":
                hit = path.startswith(value) or f"/{value}" in path
            elif kind == "file":
                hit = path == value
            elif kind == "pattern":
                hit = fnmatch.fnmatchcase(path, value)
            elif kind == "open-adapters-only":
                # a loose file of the folder too: its first part is a file name, never an adapter's folder
                hit = path.startswith(value) and path[len(value):].split("/")[0] not in opens
            else:  # open-imports-only: a .yaml file with no id: is kept, as it describes no one adapter
                hit = path.startswith(value) and path.endswith(".yaml") \
                    and import_id(ROOT / path) not in (None, *opens)
            if hit:
                matched.append(path)
                out.setdefault(path, f"{kind}: {value}" if kind.startswith("open-") else
                               f"**/{value}" if kind == "any-folder" else value)
        if not matched and kind != "any-folder":
            raise MirrorError(f"{EXCLUDE_FILE}: the rule {value!r} ({kind}) matches no tracked file; remove or fix it")
    ignored = [path for path in run(["git", "ls-files", "-z", "-ci", "--exclude-standard"], ROOT).split("\0") if path]
    for path in ignored:
        out.setdefault(path, ".gitignore")
    return out


def overlay_files() -> list[str]:
    folder = ROOT / OVERLAY_DIR
    if not folder.is_dir():
        raise MirrorError(f"{folder} is missing")
    files = sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())
    if not files:
        raise MirrorError(f"{folder} has no files")
    return files


def check_overlay_module() -> None:
    """The overlay's empty hidden-adapters module must be what the generator writes for no hidden adapter: the real
    module's text up to its array, then an empty one."""
    real = (ROOT / HIDDEN_MODULE).read_text(encoding="utf-8")
    empty = (ROOT / OVERLAY_DIR / HIDDEN_MODULE).read_text(encoding="utf-8")
    if HIDDEN_MODULE_ARRAY not in real:
        raise MirrorError(f"{HIDDEN_MODULE} has no line starting {HIDDEN_MODULE_ARRAY!r}")
    expected = real[:real.index(HIDDEN_MODULE_ARRAY)] + HIDDEN_MODULE_ARRAY + "[];\n"
    if empty != expected:
        raise MirrorError(f"{OVERLAY_DIR}/{HIDDEN_MODULE} differs from what the generator writes for no hidden adapter; "
                          f"make it {HIDDEN_MODULE}'s text up to its array, then '[];'")


# --- 4. the scan -----------------------------------------------------------------------------------------------------

def private_ids() -> list[str]:
    path = ROOT / PRIVATE_LIST
    if not path.is_file():
        raise MirrorError(f"{path} is missing; it lists the private adapters")
    ids = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    if not ids:
        raise MirrorError(f"{path} lists no adapter")
    return ids


def strings_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in strings_in(item)]
    if isinstance(value, list):
        return [s for item in value for s in strings_in(item)]
    return []


def earlier_titles(ids: set[str]) -> set[str]:
    """Every title a private adapter has had in the hub's catalogue text files, in jeff-dev's history."""
    titles: set[str] = set()
    for file in TEXT_FILES:
        # Every commit that added or changed the file (not one that deleted it, where it has no text to read).
        for sha in run(["git", "log", "--format=%H", "--diff-filter=ACMRT", "--", file], ROOT).split():
            data = yaml.safe_load(run(["git", "show", f"{sha}:{file}"], ROOT))
            if not isinstance(data, dict):
                raise MirrorError(f"{file} at {sha[:8]}: not a YAML mapping")
            for adapter_id, text in data.items():
                if adapter_id in ids:
                    if not isinstance(text, dict) or not isinstance(text.get("title"), str):
                        raise MirrorError(f"{file} at {sha[:8]}: {adapter_id} has no title")
                    titles.add(text["title"])
    return titles


def quoted_words(opens: set[str]) -> set[str]:
    """Words quoted as something other than an adapter name: every adapter ID's domain, and every label key of an
    open adapter."""
    words = {name.split(".")[1] for folder in ("catalogue", SERVED_DIR) for name in
             (path.name for path in (ROOT / folder).iterdir() if path.is_dir()) if name.startswith("jeff.")}
    for adapter_id in opens:
        manifest = yaml.safe_load((ROOT / "catalogue" / adapter_id / "manifest.yaml").read_text(encoding="utf-8"))
        for function in manifest["functions"].values():
            words |= set(function["labels"].get("fixed") or {})
    return words


def name_patterns(opens: set[str]) -> dict[str, re.Pattern[bytes]]:
    """Each private name with the pattern that finds it."""
    ids = private_ids()
    whole: dict[str, list[str]] = {}  # an ID, alias, data folder or example id -> the function keys it may precede
    phrases: set[str] = set()  # titles and example names, matched as phrases
    for adapter_id in ids:
        path = ROOT / SERVED_DIR / adapter_id / "manifest.yaml"
        if not path.is_file():
            raise MirrorError(f"the private adapter {adapter_id} has no manifest {path}")
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        if manifest["id"] != adapter_id:
            raise MirrorError(f"{path} has the ID {manifest['id']}, not {adapter_id}")
        keys = sorted(manifest["functions"])
        for name in [adapter_id, *manifest["aliases"]]:
            whole[name] = keys
        phrases.add(manifest["hub"]["title"])
        for text in strings_in(manifest):
            for folder in DATA_FOLDER.findall(text):
                whole.setdefault(folder, [])
    staged = ROOT / SERVED_DIR / "staged-not-served"
    for path in sorted(staged.glob("*/manifest.yaml")) if staged.is_dir() else []:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        if manifest["id"] not in opens:
            for name in [manifest["id"], *manifest["aliases"]]:
                whole.setdefault(name, sorted(manifest["functions"]))
    for path in sorted((ROOT / "catalogue-imports").rglob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("id") in ids:
            for key in ("path", "adapter_path"):
                if isinstance(data.get(key), str):
                    whole.setdefault(data[key], [])
    titles = yaml.safe_load((ROOT / OWNER_ONLY / "catalogue-text.yaml").read_text(encoding="utf-8"))
    missing = sorted(set(titles) - set(ids))
    if missing:
        raise MirrorError(f"{OWNER_ONLY}/catalogue-text.yaml has text for adapters {PRIVATE_LIST} does not list: {missing}")
    phrases |= {text["title"] for text in titles.values()}
    phrases |= earlier_titles(set(ids))
    examples_dir = ROOT / OWNER_ONLY / "examples"
    for path in sorted(examples_dir.glob("*.yaml")):
        for example in yaml.safe_load(path.read_text(encoding="utf-8")):
            whole.setdefault(example["id"], [])
            if example.get("name"):
                phrases.add(example["name"])
    clients = ROOT / CLIENT_NAMES
    if not clients.is_file():
        raise MirrorError(f"{clients} is missing; it lists the client names the public tree must not hold")
    client_names = [line.strip() for line in clients.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")]
    if not client_names:
        raise MirrorError(f"{clients} lists no name")
    words = quoted_words(opens)
    patterns: dict[str, re.Pattern[bytes]] = {}
    for client in client_names:
        # the name with any spacing, or none, between its words, in any case
        spaced = r"[\s_-]*".join(map(re.escape, client.split()))
        patterns[client] = re.compile(rf"(?i)(?<![A-Za-z]){spaced}(?![a-z])".encode())
    for name, keys in whole.items():
        escaped = re.escape(name)
        if PLAIN_WORD.fullmatch(name):
            forms = [rf"(?<![A-Za-z0-9_.-]){escaped}'s(?![A-Za-z0-9_])",
                     rf"jeff-(?:data|adapters)(?:@[A-Za-z0-9._-]+)?/apps/{escaped}(?![A-Za-z0-9_.-])",
                     rf"catalogue-imports/(?:[A-Za-z0-9._-]+/)?{escaped}\.yaml",
                     rf"(?<![A-Za-z0-9_.-])import-{escaped}(?![A-Za-z0-9_-])"]
            if keys:
                forms.append(rf"(?<![A-Za-z0-9_.-]){escaped}\.(?:{'|'.join(map(re.escape, keys))})(?![A-Za-z0-9_])")
            if name not in words:
                forms.append(rf"([\"'`]){escaped}\1")
            pattern = "|".join(f"(?:{form})" for form in forms)
        else:
            pattern = rf"(?<![A-Za-z0-9_.-]){escaped}(?![A-Za-z0-9_-])"
        patterns[name] = re.compile(pattern.encode())
    for phrase in phrases:
        if phrase not in patterns:
            patterns[phrase] = re.compile(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])".encode())
    return patterns


def scan(tree: Path, patterns: dict[str, re.Pattern[bytes]]) -> list[str]:
    """Every private name and secret in the files of `tree` (outside .git), as path:line: what."""
    found = []
    for path in sorted(tree.rglob("*")):
        if ".git" in path.relative_to(tree).parts or not path.is_file():
            continue
        relative = str(path.relative_to(tree))
        data = path.read_bytes()
        line_of = lambda offset: data.count(b"\n", 0, offset) + 1  # noqa: E731
        for name, pattern in patterns.items():
            for match in pattern.finditer(data):
                found.append(f"{relative}:{line_of(match.start())}: private name {name!r}")
        for what, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(data):
                text = match.group(0).decode("utf-8", "replace")
                if (relative, text) not in ALLOWED_SECRETS:
                    found.append(f"{relative}:{line_of(match.start())}: {what} {text[:40]!r}")
    return found


# --- 3, 5 and 6: the clone, the commit and the push ------------------------------------------------------------------

def export(sha: str, kept: list[str], destination: Path) -> None:
    """The commit's files `kept` into `destination` (git archive of the commit, so the files as committed)."""
    archive = destination.parent / "export.tar"
    with archive.open("wb") as out:
        result = subprocess.run(["git", "archive", "--format=tar", sha], cwd=ROOT, stdout=out, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise MirrorError(f"git archive {sha} failed: {result.stderr.decode().strip()}")
    unpacked = destination.parent / "unpacked"
    unpacked.mkdir()
    run(["tar", "-xf", str(archive), "-C", str(unpacked)], ROOT)
    for path in kept:
        source = unpacked / path
        if not source.is_file() and not source.is_symlink():
            raise MirrorError(f"{path} is tracked but not in the archive of {sha[:8]}")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    shutil.rmtree(unpacked)
    archive.unlink()


def replace_tree(clone: Path, export_dir: Path) -> None:
    for entry in clone.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in export_dir.iterdir():
        shutil.move(str(entry), clone / entry.name)


def changes(clone: Path) -> Counter[str]:
    """The staged changes against the clone's last commit, counted as added, changed and deleted files."""
    fields = run(["git", "diff", "--cached", "--name-status", "--no-renames", "-z"], clone).split("\0")
    names = {"A": "added", "M": "changed", "D": "deleted", "T": "changed"}
    kinds: Counter[str] = Counter()
    for code in fields[0:-1:2]:
        if code not in names:
            raise MirrorError(f"unexpected change kind {code!r} in the clone")
        kinds[names[code]] += 1
    return kinds


def groups(paths: dict[str, str]) -> Counter[str]:
    return Counter(paths.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--push", action="store_true", help="push the new commit to firelex/jeff main (never forced)")
    args = parser.parse_args()

    sha = check_source()
    short = sha[:7]
    files = tracked_files()
    opens = open_ids()
    out = left_out(files, read_rules(), opens)
    overlay = overlay_files()
    check_overlay_module()
    clashes = [path for path in overlay if path in files and path not in out]
    if clashes:
        raise MirrorError(f"{OVERLAY_DIR} has files the export keeps too: {', '.join(clashes)}")
    kept = [path for path in files if path not in out]
    patterns = name_patterns(opens)

    work = Path(tempfile.mkdtemp(prefix="jeff-mirror-"))
    clone = work / "jeff"
    print(f"Cloning {REMOTE} into {clone}")
    run(["git", "clone", "--quiet", "--branch", BRANCH, REMOTE, str(clone)], work)
    before = len([path for path in run(["git", "ls-files", "-z"], clone).split("\0") if path])
    export_dir = work / "export"
    export_dir.mkdir()
    export(sha, kept, export_dir)
    for path in overlay:
        target = export_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / OVERLAY_DIR / path, target)
    replace_tree(clone, export_dir)
    export_dir.rmdir()

    found = scan(clone, patterns)
    if found:
        raise MirrorError(f"the public tree in {clone} holds private names or secrets; nothing was committed:\n"
                          + "\n".join(found))

    run(["git", "add", "--all"], clone)
    kinds = changes(clone)
    if not kinds:
        raise MirrorError(f"firelex/jeff already holds exactly this tree; nothing to commit ({clone})")
    after = len([path for path in run(["git", "ls-files", "-z"], clone).split("\0") if path])
    summary = (f"The public tree of jeff-dev {short}: {len(kept)} of its {len(files)} tracked files, plus "
               f"{len(overlay)} files of the hub's empty owner-only set; {len(out)} files are left out "
               f"(scripts/mirror-exclude.txt).\n\nAgainst the previous commit: "
               + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
               + f" ({before} files before, {after} after).")
    message = f"Sync from jeff-dev {short}\n\n{summary}\n"
    run(["git", "-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}", "commit", "--quiet",
         "--author", f"{AUTHOR_NAME} <{AUTHOR_EMAIL}>", "-m", message], clone)
    commit = run(["git", "rev-parse", "--short", "HEAD"], clone).strip()

    print(f"\nCommitted {commit} in {clone}:\n\n{message}")
    print("Left out, by rule:")
    for rule, count in sorted(groups(out).items()):
        print(f"  {count:5d}  {rule}")
    if args.push:
        run(["git", "push", "origin", f"HEAD:{BRANCH}"], clone, capture=False)
        print(f"\nPushed {commit} to {REMOTE} {BRANCH}.")
    else:
        print(f"\nNot pushed. To publish it:\n  git -C {clone} push origin HEAD:{BRANCH}")


if __name__ == "__main__":
    try:
        main()
    except MirrorError as error:
        print(f"publish_mirror: {error}", file=sys.stderr)
        sys.exit(1)
