"""End-to-end processing pipeline for wallpapers."""

import os
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional
import urllib.request

from curate_db import ID_ALLOCATION_LOCK

from .classifier import classify_image
from .config import DB_PATH, INCOMING_DIR, WALLPAPERS_DIR
from .db import get_next_id, init_db, insert_wallpaper
from .deduplicator import check_duplicates
from .storage import ensure_storage_structure, store_wallpaper
from .validator import validate_image


class ProcessingResult(NamedTuple):
    status: str  # "COMPLETED", "REJECTED", "DUPLICATE", "FAILED"
    reason: str
    wallpaper_id: Optional[int] = None
    target_path: Optional[Path] = None
    metadata: Optional[Dict[str, Any]] = None


def download_image(url: str, dest_dir: Path = INCOMING_DIR) -> Path:
    """Download an image URL into the incoming folder."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Extract filename from URL or default
    filename = url.split("?")[0].split("/")[-1] or "downloaded_wallpaper.jpg"
    dest_path = dest_dir / filename

    # Avoid overwriting existing incoming file
    counter = 1
    stem = dest_path.stem
    ext = dest_path.suffix or ".jpg"
    while dest_path.exists():
        dest_path = dest_dir / f"{stem}_{counter}{ext}"
        counter += 1

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WallpaperAgent/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as response, open(dest_path, "wb") as out_file:
        out_file.write(response.read())

    return dest_path


def process_image(
    file_path: Path,
    source: Optional[str] = None,
    source_url: Optional[str] = None,
    category_hint: Optional[str] = None,
    type_hint: Optional[str] = None,
    move: bool = False,
    db_path: Path = DB_PATH,
    wallpapers_dir: Path = WALLPAPERS_DIR,
) -> ProcessingResult:
    """
    Execute the core 9-stage pipeline on a single image file:
    1. Validation
    2. Minimum 2K resolution check
    3. Exact & visual duplicate check
    4. AI / NON-AI / UNKNOWN classification
    5. Categorization
    6. Sequential permanent ID assignment
    7. Storage in Wallpapers/<type>/<category>/<id>.<ext>
    8. Database metadata record persistence
    """
    init_db(db_path)
    ensure_storage_structure(wallpapers_dir)

    path = Path(file_path)
    if not path.exists():
        return ProcessingResult("FAILED", f"Source file does not exist: {path}")

    # Stage 1 & 2: Validation & Resolution Check (>= 2K pixels)
    val_res = validate_image(path)
    if not val_res.is_valid:
        return ProcessingResult("REJECTED", val_res.reason)

    # Stage 3: Duplicate Check
    dup_res = check_duplicates(path, db_path=db_path)
    if dup_res.is_exact_duplicate:
        match_id = dup_res.exact_match.get("id") if dup_res.exact_match else "unknown"
        return ProcessingResult(
            "DUPLICATE",
            f"Exact duplicate of wallpaper ID {match_id} (SHA256: {dup_res.sha256[:12]}...)"
        )

    # Stage 4 & 5: AI & Category Classification
    metadata_hint = {"type": type_hint} if type_hint else None
    classification = classify_image(
        path,
        source=source,
        source_url=source_url,
        category_hint=category_hint,
        metadata_hint=metadata_hint,
    )

    # Stages 6-8: Allocate ID, save image, and insert metadata atomically.
    # The lock spans all three stages so concurrent pipelines cannot collide
    # on MAX(id)+1 before either INSERT commits.
    with ID_ALLOCATION_LOCK:
        wallpaper_id = get_next_id(db_path=db_path)

        # Stage 7: Save image to destination
        try:
            target_path = store_wallpaper(
                source_file=path,
                wallpaper_id=wallpaper_id,
                category_name=classification.category,
                move=move,
                base_dir=wallpapers_dir,
            )
        except Exception as e:
            return ProcessingResult("FAILED", f"Failed to store wallpaper file: {e}")

        # Stage 8: Save metadata to database
        metadata = {
            "id": wallpaper_id,
            "filename": target_path.name,
            "type": classification.type,
            "category": classification.category,
            "width": val_res.width,
            "height": val_res.height,
            "format": val_res.format,
            "filesize": val_res.filesize,
            "sha256": dup_res.sha256,
            "perceptual_hash": dup_res.perceptual_hash,
            "source": source or "Local Import",
            "source_url": source_url,
            "ai_confidence": classification.ai_confidence,
            "duplicate_of": None,
            "aspect_ratio": val_res.aspect_ratio,
            "orientation": val_res.orientation,
            "original_filename": path.name,
        }

        try:
            insert_wallpaper(metadata, db_path=db_path)
        except Exception as e:
            # If DB insert fails, clean up the copied file to prevent orphaned files
            if target_path.exists() and not move:
                target_path.unlink()
            return ProcessingResult("FAILED", f"Failed to save metadata to database: {e}")

    return ProcessingResult(
        "COMPLETED",
        f"Saved as ID {wallpaper_id} in {classification.category} ({classification.type})",
        wallpaper_id=wallpaper_id,
        target_path=target_path,
        metadata=metadata,
    )


def process_incoming(
    incoming_dir: Path = INCOMING_DIR,
    db_path: Path = DB_PATH,
    wallpapers_dir: Path = WALLPAPERS_DIR,
) -> Dict[str, Any]:
    """Process all wallpaper images in the incoming directory."""
    incoming_dir.mkdir(parents=True, exist_ok=True)
    files = [f for f in incoming_dir.iterdir() if f.is_file()]

    results = {
        "total": len(files),
        "completed": 0,
        "rejected": 0,
        "duplicate": 0,
        "failed": 0,
        "details": [],
    }

    for file_path in sorted(files):
        res = process_image(
            file_path=file_path,
            move=True,
            db_path=db_path,
            wallpapers_dir=wallpapers_dir,
        )

        results["details"].append({"file": file_path.name, "status": res.status, "reason": res.reason})
        if res.status == "COMPLETED":
            results["completed"] += 1
        elif res.status == "REJECTED":
            results["rejected"] += 1
            # Remove rejected files from incoming to keep workspace clean
            if file_path.exists():
                file_path.unlink()
        elif res.status == "DUPLICATE":
            results["duplicate"] += 1
            if file_path.exists():
                file_path.unlink()
        else:
            results["failed"] += 1

    return results
