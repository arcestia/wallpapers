"""Filesystem storage manager for Wallpapers and incoming directory."""

import os
import shutil
from pathlib import Path
from typing import Tuple

from .config import INCOMING_DIR, WALLPAPERS_DIR


def ensure_storage_structure(base_dir: Path = WALLPAPERS_DIR) -> None:
    """Ensure root library and incoming directories exist without pre-creating empty category folders."""
    base_dir.mkdir(parents=True, exist_ok=True)
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)


def prune_empty_folders(base_dir: Path = WALLPAPERS_DIR) -> None:
    """Remove empty subdirectories in the wallpaper library."""
    if not base_dir.exists():
        return

    # Bottom-up directory walk to clean nested empty folders
    for root, dirs, files in os.walk(base_dir, topdown=False):
        root_path = Path(root)
        if root_path != base_dir and not any(root_path.iterdir()):
            try:
                root_path.rmdir()
            except OSError:
                pass


def get_target_path(
    wallpaper_id: int,
    category_name: str,
    extension: str,
    base_dir: Path = WALLPAPERS_DIR,
    type_name: str = None,  # Kept as optional for backwards compatibility
) -> Path:
    """Get the target filesystem path for a wallpaper with sequential permanent ID."""
    clean_ext = extension.lower()
    if clean_ext == ".jpeg":
        clean_ext = ".jpg"

    return base_dir / category_name / f"{wallpaper_id}{clean_ext}"


def store_wallpaper(
    source_file: Path,
    wallpaper_id: int,
    category_name: str,
    move: bool = False,
    base_dir: Path = WALLPAPERS_DIR,
    type_name: str = None,  # Kept as optional for backwards compatibility
) -> Path:
    """
    Store wallpaper to the library under the permanent ID filename directly in its category folder.
    Creates category folder on-demand and preserves original image file.
    """
    ensure_storage_structure(base_dir)
    target = get_target_path(
        wallpaper_id=wallpaper_id,
        category_name=category_name,
        extension=source_file.suffix,
        base_dir=base_dir,
    )
    # Create parent folder only when an image is saved to it
    target.parent.mkdir(parents=True, exist_ok=True)

    if move:
        shutil.move(str(source_file), str(target))
    else:
        shutil.copy2(str(source_file), str(target))

    return target
