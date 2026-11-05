"""Scan built SDK packages for private names and secrets before they go to a public registry.

Usage: uv run python scripts/scan_sdk_artifacts.py FILE [FILE ...]

Each FILE is a package as a registry receives it: a wheel (.whl), a source distribution (.tar.gz), an npm tarball
(.tgz), a crate (.crate), a NuGet package (.nupkg) or a Maven Central bundle (.zip). Every file is unpacked into a
temporary folder (archives inside it, such as the jars of a Maven bundle, are unpacked too) and scanned with the rules
of scripts/publish_mirror.py: the name of a private adapter, or something that looks like a secret. It prints each
finding as archive: path:line: what, and exits 1 when there is any; a package that names a private adapter must never
be published, since a registry never deletes a version."""

import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_mirror  # noqa: E402

ZIP_SUFFIXES = (".whl", ".nupkg", ".zip", ".jar")
TAR_SUFFIXES = (".tar.gz", ".tgz", ".crate")


def unpack(archive: Path, into: Path) -> None:
    """Unpack `archive` into `into`, then every archive found inside it, each into a folder next to it."""
    name = archive.name
    if name.endswith(ZIP_SUFFIXES):
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
    elif name.endswith(TAR_SUFFIXES):
        with tarfile.open(archive) as t:
            t.extractall(into, filter="data")
    else:
        raise SystemExit(f"{archive}: not a package this scan knows (.whl, .tar.gz, .tgz, .crate, .nupkg, .zip)")
    for inner in sorted(into.rglob("*")):
        if inner.is_file() and inner.name.endswith(ZIP_SUFFIXES + TAR_SUFFIXES):
            target = inner.with_name(inner.name + ".unpacked")
            target.mkdir()
            unpack(inner, target)


def main() -> None:
    files = [Path(arg) for arg in sys.argv[1:]]
    if not files:
        raise SystemExit(__doc__)
    patterns = publish_mirror.name_patterns(publish_mirror.open_ids())
    findings = []
    for file in files:
        if not file.is_file():
            raise SystemExit(f"{file} is missing")
        with tempfile.TemporaryDirectory(prefix="jeff-scan-") as work:
            tree = Path(work)
            unpack(file, tree)
            findings += [f"{file.name}: {finding}" for finding in publish_mirror.scan(tree, patterns)]
    if findings:
        print("\n".join(findings))
        raise SystemExit(f"{len(findings)} private names or secrets in the packages; nothing may be published")
    print(f"Scanned {len(files)} packages: no private name and no secret")


if __name__ == "__main__":
    main()
