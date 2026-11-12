"""BurstBuster: find photo bursts, keep the best, set the rest aside safely."""

__version__ = "1.0.0"

import os
from pathlib import Path

HOLDING_DIR = "_BurstBuster_Holding"
_BASE = Path(os.environ.get("LOCALAPPDATA", Path.home()))
APP_DIR = _BASE / "BurstBuster"  # cache + journal, never photos
if not APP_DIR.exists() and (_BASE / "BurstPhotoBuster").is_dir():
    try:  # app was renamed: carry over the journal, which tracks photos still in holding folders
        os.rename(_BASE / "BurstPhotoBuster", APP_DIR)
    except OSError:
        APP_DIR = _BASE / "BurstPhotoBuster"
