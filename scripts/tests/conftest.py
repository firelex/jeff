"""The scripts are plain files, not a package: their folder goes on the import path so tests can import them."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
