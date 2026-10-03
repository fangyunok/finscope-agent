"""Public fixture paths for source checkouts and installed wheels."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGED_DATA_DIR = PACKAGE_DIR / "data"
PROJECT_ROOT = PACKAGE_DIR.parents[1]
DATA_DIR = PROJECT_ROOT / "data" if (PROJECT_ROOT / "pyproject.toml").is_file() else PACKAGED_DATA_DIR
DEFAULT_DB_PATH = Path(os.environ.get("FINSCOPE_DB_PATH", str(Path.cwd() / "runs" / "finscope.sqlite"))).expanduser()


def data_path(name: str) -> Path:
    """Return a bundled fixture, rejecting filenames that escape the folder."""
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("Fixture name must be a single filename")
    path = DATA_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"Bundled fixture is missing: {name}")
    return path
