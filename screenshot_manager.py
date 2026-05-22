"""Screenshot file management — saving, folder creation, cleanup."""

import os
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
MAX_AGE_MINUTES = 30


def ensure_dir() -> Path:
    """Create screenshots directory if needed, return path."""
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    return SCREENSHOTS_DIR


def cleanup(max_age_minutes: int = MAX_AGE_MINUTES) -> int:
    """Delete screenshot files and empty burst folders older than max_age_minutes.
    Returns count of deleted items."""
    if not SCREENSHOTS_DIR.exists():
        return 0

    cutoff = time.time() - max_age_minutes * 60
    deleted = 0

    for item in SCREENSHOTS_DIR.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                deleted += 1
            elif item.is_dir():
                # Delete old files inside burst folders
                sub_files = list(item.iterdir())
                for f in sub_files:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        deleted += 1
                # Remove folder if now empty
                if not any(item.iterdir()):
                    item.rmdir()
                    deleted += 1
        except OSError:
            continue

    return deleted


def save_jpeg(image: Image.Image, quality: int = 30, prefix: str = "screen") -> Path:
    """Save PIL Image as JPEG with timestamp filename. Returns file path."""
    ensure_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{timestamp}.jpg"
    path = SCREENSHOTS_DIR / filename

    # JPEG doesn't support alpha
    if image.mode in ("RGBA", "LA", "PA"):
        image = image.convert("RGB")

    image.save(path, format="JPEG", quality=quality)
    return path


def create_burst_dir() -> Path:
    """Create a timestamped burst subfolder. Returns folder path."""
    ensure_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    burst_dir = SCREENSHOTS_DIR / f"burst_{timestamp}"
    burst_dir.mkdir(exist_ok=True)
    return burst_dir
