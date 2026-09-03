"""Unified Ingestion Pipeline & Network Utilities.

Consolidates download, validation, deduplication, classification, and
storage into a single authoritative pipeline used by all ingest runners.
Merges the inline logic from curate.py with wallpaper_agent primitives.
"""

from collections import Counter
import json
import logging
from math import gcd
import os
from pathlib import Path
import re
import shutil
import struct
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

from curate_db import ID_ALLOCATION_LOCK, db_session, get_wallpaper_by_sha256
from curate_categories import CATEGORY_PATTERNS
from wallpaper_agent.classifier import classify_image as agent_classify_image
from wallpaper_agent.config import (
    CATEGORIES,
    DB_PATH,
    INCOMING_DIR,
    MIN_PIXELS,
    SUPPORTED_EXTENSIONS,
    WALLPAPERS_DIR,
)
from wallpaper_agent.deduplicator import check_duplicates as agent_check_duplicates
from wallpaper_agent.storage import ensure_storage_structure, store_wallpaper
from wallpaper_agent.validator import (
    ValidationResult,
    compute_aspect_ratio,
    compute_orientation,
    validate_image as agent_validate_image,
)

logger = logging.getLogger("curate.ingest")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 WallpaperCurator/2.0"
)


# ==============================================================================
# SAFE STREAMING DOWNLOAD
# ==============================================================================

def safe_download_url(
    url: str,
    dest_path: Path,
    timeout: int = 15,
    user_agent: str = DEFAULT_USER_AGENT,
    max_bytes: int = 50 * 1024 * 1024,  # 50 MB safety ceiling
) -> bool:
    """Download a URL safely with strict chunked streaming to prevent hangs.

    - Cleans up partial files on failure
    - Enforces timeout per socket read, not just connection
    - Enforces max_bytes cap to guard against infinite streams
    """
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_target = dest_path.with_suffix(dest_path.suffix + f".tmp_{os.getpid()}_{int(time.time()*1000)}")

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        total_read = 0
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(temp_target, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > max_bytes:
                    raise ValueError(f"Download exceeded max size of {max_bytes} bytes")
                out.write(chunk)

        if total_read == 0:
            raise ValueError("Downloaded 0 bytes")

        # Atomic rename into final destination
        if dest_path.exists():
            dest_path.unlink()
        temp_target.rename(dest_path)
        return True

    except Exception as e:
        logger.debug(f"Download failed for {url}: {e}")
        if temp_target.exists():
            try:
                temp_target.unlink()
            except Exception:
                pass
        return False


# ==============================================================================
# PURE-PYTHON IMAGE HEADER PARSER (ZERO-DEPENDENCY FALLBACK)
# ==============================================================================

def get_image_info_pure(file_path: Path) -> Optional[Tuple[int, int, str, int]]:
    """Extract (width, height, format, filesize) from binary headers without PIL."""
    file_path = Path(file_path)
    if not file_path.exists() or not file_path.is_file():
        return None

    filesize = file_path.stat().st_size
    try:
        with open(file_path, "rb") as f:
            head = f.read(64)

            # PNG
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                w, h = struct.unpack(">II", head[16:24])
                return w, h, "PNG", filesize

            # JPEG
            if head.startswith(b"\xff\xd8"):
                f.seek(0)
                data = f.read()
                idx = 2
                while idx < len(data) - 9:
                    if data[idx] == 0xFF and data[idx + 1] in (
                        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
                    ):
                        h, w = struct.unpack(">HH", data[idx + 5 : idx + 9])
                        return w, h, "JPEG", filesize
                    idx += 1

            # WEBP
            if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
                if head[12:16] == b"VP8X":
                    w = int.from_bytes(head[24:27], "little") + 1
                    h = int.from_bytes(head[27:30], "little") + 1
                    return w, h, "WEBP", filesize
                elif head[12:16] == b"VP8 ":
                    w = int.from_bytes(head[26:28], "little") & 0x3FFF
                    h = int.from_bytes(head[28:30], "little") & 0x3FFF
                    return w, h, "WEBP", filesize

    except Exception:
        pass
    return None


# ==============================================================================
# VALIDATION WITH ZERO-DEPENDENCY FALLBACK
# ==============================================================================

def validate_image_unified(file_path: Path) -> ValidationResult:
    """Validate image format, corruption, and 2K resolution requirement.

    Tries wallpaper_agent.validator first (PIL-based), falls back to pure header parser.
    """
    path = Path(file_path)
    if not path.exists():
        return ValidationResult(False, f"File does not exist: {path}")
    if path.stat().st_size == 0:
        return ValidationResult(False, "Empty file (0 bytes)")

    try:
        return agent_validate_image(path)
    except Exception:
        # Fallback path if PIL is unavailable or throws
        info = get_image_info_pure(path)
        if not info:
            return ValidationResult(False, "Failed to parse image headers")

        width, height, fmt, filesize = info
        pixel_count = width * height
        ratio = width / height if height > 0 else 1.77
        aspect_ratio = f"{round(ratio, 2)}:1"
        orientation = compute_orientation(width, height)

        if pixel_count < MIN_PIXELS:
            return ValidationResult(
                False,
                f"Sub-2K resolution: {width}x{height} = {pixel_count:,} px (< {MIN_PIXELS:,} required)",
                width=width,
                height=height,
                pixel_count=pixel_count,
                format=fmt,
                filesize=filesize,
                aspect_ratio=aspect_ratio,
                orientation=orientation,
            )

        return ValidationResult(
            True,
            "Valid 2K+ wallpaper",
            width=width,
            height=height,
            pixel_count=pixel_count,
            format=fmt,
            filesize=filesize,
            aspect_ratio=aspect_ratio,
            orientation=orientation,
        )


# ==============================================================================
# THE ONE PIPELINE: VALIDATE + DEDUP + CLASSIFY + STORE + DB INSERT
# ==============================================================================

class IngestResult(NamedTuple):
    status: str  # "COMPLETED", "REJECTED", "DUPLICATE", "FAILED"
    reason: str
    id: Optional[int] = None
    category: Optional[str] = None
    filename: Optional[str] = None
    resolution: Optional[str] = None
    signals: Optional[str] = None
    duplicate_id: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"status": self.status, "reason": self.reason}
        if self.id is not None:
            d["id"] = self.id
        if self.category:
            d["category"] = self.category
        if self.filename:
            d["filename"] = self.filename
        if self.resolution:
            d["resolution"] = self.resolution
        if self.signals:
            d["signals"] = self.signals
        if self.duplicate_id is not None:
            d["duplicate_id"] = self.duplicate_id
        return d


def validate_and_ingest_image_file(
    file_path: Path,
    source: str = "Import",
    source_url: str = "",
    category_hint: Optional[str] = None,
    move: bool = False,
    title: Optional[str] = None,
    author: Optional[str] = None,
    license: Optional[str] = None,
    tags: Optional[List[str]] = None,
    db_path: Path = DB_PATH,
    wallpapers_dir: Path = WALLPAPERS_DIR,
) -> Dict[str, Any]:
    """Authoritative ingestion function for all sources (Wallhaven, Alpha Coders, DeviantArt, etc.).

    Steps:
    1. Validation (PIL + pure header fallback; >= 2K check)
    2. Exact duplicate check (SHA-256 vs wallpapers.sha256)
    3. Classification (metadata + keyword scoring + vision fallback)
    4. ID allocation (thread-safe MAX(id)+1 inside connection transaction)
    5. Storage into Wallpapers/<Category>/<id>.<ext>
    6. DB row insertion with complete provenance (author, title, license)
    """
    path = Path(file_path)
    if not path.exists():
        return IngestResult("FAILED", f"File does not exist: {path}").as_dict()

    # 1. Validation
    val = validate_image_unified(path)
    if not val.is_valid:
        return IngestResult("REJECTED", val.reason).as_dict()

    # 2. Exact Duplicate Check via SHA-256
    import hashlib
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    sha256 = hasher.hexdigest()

    # Compute pHash if imagehash is available (non-blocking)
    phash = ""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None  # Allow 8K/16K wallpapers
        import imagehash
        with Image.open(path) as img:
            phash = str(imagehash.phash(img))
    except Exception:
        pass

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, category, filename FROM wallpapers WHERE sha256 = ?", (sha256,))
        dup = cursor.fetchone()
        if dup:
            return IngestResult(
                "DUPLICATE",
                f"Exact duplicate of ID #{dup['id']} ({dup['category']}/{dup['filename']})",
                duplicate_id=dup["id"],
            ).as_dict()

    # 3. Classification
    if category_hint and category_hint in CATEGORIES:
        category = category_hint
        cls_type = "UNKNOWN"
        ai_conf = 0.5
        sig = f"Assigned category hint: {category_hint}"
    else:
        try:
            cls_res = agent_classify_image(
                path,
                source=source,
                source_url=source_url,
                category_hint=category_hint,
                tags=tags,
            )
            category = cls_res.category
            cls_type = cls_res.type
            ai_conf = cls_res.ai_confidence
            sig = cls_res.detected_signals or f"Classified as {category}"
        except Exception:
            # Fallback keyword classifier
            corpus = f"{path.name} {title or ''} {source} {source_url} {' '.join(tags or [])}".lower()
            best_cat = "Other"
            best_score = 0
            for cat, patterns in CATEGORY_PATTERNS.items():
                score = sum(1 for pat in patterns if re.search(pat, corpus, re.IGNORECASE))
                if score > best_score:
                    best_score = score
                    best_cat = cat
            category = best_cat
            cls_type = "UNKNOWN"
            ai_conf = 0.5 if best_score > 0 else 0.2
            sig = f"Keyword match: {best_score} hits" if best_score > 0 else "Fallback default"

    # 4. Storage & ID allocation serialized across ingestion threads
    ext = path.suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"

    with ID_ALLOCATION_LOCK:
        with db_session(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(id) FROM wallpapers")
            max_row = cursor.fetchone()
            w_id = (max_row[0] + 1) if (max_row and max_row[0] is not None) else 1

            dest_filename = f"{w_id}{ext}"
            dest_dir = Path(wallpapers_dir) / category
            dest_dir.mkdir(parents=True, exist_ok=True)
            target_path = dest_dir / dest_filename

            try:
                if move:
                    shutil.move(str(path), str(target_path))
                else:
                    shutil.copy2(str(path), str(target_path))
            except Exception as e:
                return IngestResult("FAILED", f"File storage failed: {e}").as_dict()

            # 5. Insert row with full metadata
            try:
                cursor.execute(
                    """
                    INSERT INTO wallpapers (
                        id, filename, type, category, width, height, format, filesize,
                        sha256, perceptual_hash, source, source_url, ai_confidence,
                        aspect_ratio, orientation, title, author, license, original_filename, is_curated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        w_id,
                        dest_filename,
                        cls_type,
                        category,
                        val.width,
                        val.height,
                        val.format,
                        val.filesize,
                        sha256,
                        phash or None,
                        source,
                        source_url,
                        ai_conf,
                        val.aspect_ratio,
                        val.orientation,
                        title,
                        author,
                        license,
                        path.name,
                    ),
                )
            except Exception as e:
                # Roll back copied file
                if target_path.exists() and not move:
                    try:
                        target_path.unlink()
                    except Exception:
                        pass
                return IngestResult("FAILED", f"DB insert failed: {e}").as_dict()

    return IngestResult(
        status="COMPLETED",
        reason=f"Saved as ID {w_id} in {category}",
        id=w_id,
        category=category,
        filename=dest_filename,
        resolution=f"{val.width}×{val.height}",
        signals=sig,
    ).as_dict()
