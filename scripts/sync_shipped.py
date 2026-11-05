"""Copy contract/ and catalogue/ into the jeff package (packages/python/src/jeff/_shipped/), so an installed jeff
carries the schemas, the interface catalogue and the adapter manifests. Run after changing either folder; a test fails
when the copy is out of date."""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "packages/python/src/jeff/_shipped"


def main() -> None:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    for name in ("contract", "catalogue"):
        source = ROOT / name
        if not source.is_dir():
            raise SystemExit(f"{source} is missing")
        shutil.copytree(source, TARGET / name)
    print(f"Copied contract/ and catalogue/ to {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
