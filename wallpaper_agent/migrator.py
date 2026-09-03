"""Migration utility to import legacy wallpapers into the new system."""

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import BASE_DIR, DB_PATH, WALLPAPERS_DIR
from .pipeline import process_image


# Mapping of legacy folder names to category hints and type hints
LEGACY_FOLDER_MAPPING: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    "Anime": ("Anime", "NON-AI"),
    "Calm": (None, None),  # Auto-classify based on filename keywords (Landscape/Nature/Ocean)
    "Games": ("Gaming", "NON-AI"),
    "Nature": ("Nature", None),
    "Pokémon": ("Anime", "NON-AI"),
    "Skiddle Generated": (None, "AI"),
    "Unsorted": (None, None),
}


def migrate_legacy_collection(
    base_dir: Path = BASE_DIR,
    db_path: Path = DB_PATH,
    wallpapers_dir: Path = WALLPAPERS_DIR,
    clean_old: bool = False,
) -> Dict[str, Any]:
    """
    Migrate all legacy wallpaper folders into the new 2K+ standardized archive.
    """
    summary = {
        "scanned": 0,
        "completed": 0,
        "rejected_under_2k": 0,
        "duplicates": 0,
        "failed": 0,
        "by_folder": {},
    }

    for folder_name, (cat_hint, type_hint) in LEGACY_FOLDER_MAPPING.items():
        folder_path = base_dir / folder_name
        if not folder_path.exists() or not folder_path.is_dir():
            continue

        folder_stat = {
            "total": 0,
            "completed": 0,
            "rejected": 0,
            "duplicate": 0,
            "failed": 0,
        }

        # Gather image files (skip markdown files)
        image_files = [
            f for f in folder_path.iterdir()
            if f.is_file() and not f.name.endswith(".md")
        ]

        folder_stat["total"] = len(image_files)
        summary["scanned"] += len(image_files)

        for img_file in sorted(image_files):
            result = process_image(
                file_path=img_file,
                source=f"Legacy Import ({folder_name})",
                category_hint=cat_hint,
                type_hint=type_hint,
                move=False,
                db_path=db_path,
                wallpapers_dir=wallpapers_dir,
            )

            if result.status == "COMPLETED":
                folder_stat["completed"] += 1
                summary["completed"] += 1
            elif result.status == "REJECTED":
                folder_stat["rejected"] += 1
                summary["rejected_under_2k"] += 1
            elif result.status == "DUPLICATE":
                folder_stat["duplicate"] += 1
                summary["duplicates"] += 1
            else:
                folder_stat["failed"] += 1
                summary["failed"] += 1

        summary["by_folder"][folder_name] = folder_stat

        if clean_old:
            shutil.rmtree(folder_path, ignore_errors=True)

    return summary
