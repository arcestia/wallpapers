"""Deduplication via SHA-256 and Perceptual Hashing."""

import hashlib
from pathlib import Path
from typing import NamedTuple, Optional, Tuple, List, Dict, Any
from PIL import Image
Image.MAX_IMAGE_PIXELS = None  # Allow 8K/16K wallpapers without decompression warning
import imagehash

from .config import DB_PATH, PHASH_SIMILARITY_THRESHOLD
from .db import get_wallpaper_by_sha256, find_visual_duplicates


class DuplicateCheckResult(NamedTuple):
    is_exact_duplicate: bool
    is_visual_duplicate: bool
    exact_match: Optional[Dict[str, Any]] = None
    visual_matches: List[Tuple[Dict[str, Any], int]] = []
    sha256: str = ""
    perceptual_hash: str = ""


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 checksum of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_perceptual_hash(file_path: Path) -> str:
    """Compute pHash (perceptual hash) of an image."""
    try:
        with Image.open(file_path) as img:
            ph = imagehash.phash(img)
            return str(ph)
    except Exception:
        return ""


def check_duplicates(
    file_path: Path,
    db_path: Path = DB_PATH,
    threshold: int = PHASH_SIMILARITY_THRESHOLD
) -> DuplicateCheckResult:
    """
    Check if an image is an exact or visual duplicate in the database.
    """
    sha256 = compute_sha256(file_path)
    phash = compute_perceptual_hash(file_path)

    # 1. Exact duplicate check
    exact_match = get_wallpaper_by_sha256(sha256, db_path=db_path)
    if exact_match:
        return DuplicateCheckResult(
            is_exact_duplicate=True,
            is_visual_duplicate=True,
            exact_match=exact_match,
            visual_matches=[(exact_match, 0)],
            sha256=sha256,
            perceptual_hash=phash,
        )

    # 2. Visual duplicate check
    visual_matches = find_visual_duplicates(phash, max_distance=threshold, db_path=db_path)
    is_visual = len(visual_matches) > 0 and visual_matches[0][1] == 0

    return DuplicateCheckResult(
        is_exact_duplicate=False,
        is_visual_duplicate=is_visual,
        exact_match=None,
        visual_matches=visual_matches,
        sha256=sha256,
        perceptual_hash=phash,
    )
