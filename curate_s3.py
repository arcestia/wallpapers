"""S3-compatible (Backblaze B2) Storage & CDN Sync Engine.

Provides thread-safe file uploads, checksum validation, and batch sync
for the Curated/ wallpaper collection to public CDN (cdn.skiddle.id).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import mimetypes
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from io import BytesIO
from PIL import Image

from curate_config import (
    CDN_BASE,
    CURATED_DIR,
    DB_PATH,
    S3_APP_KEY,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_KEY_ID,
    S3_MAX_WORKERS,
    S3_REGION,
)
from curate_db import (
    db_session,
    get_unsynced_curated_wallpapers,
    update_wallpaper_s3,
    update_wallpaper_s3_thumb,
)

logger = logging.getLogger("curate.s3")

_CLIENT_LOCK = threading.Lock()
_CACHED_CLIENT = None


def is_s3_configured() -> bool:
    """Return True if S3/B2 credentials and bucket are configured."""
    return bool(S3_BUCKET and S3_KEY_ID and S3_APP_KEY and S3_ENDPOINT_URL)


def get_s3_client():
    """Create or return a thread-safe cached boto3 S3 client."""
    global _CACHED_CLIENT
    if _CACHED_CLIENT is not None:
        return _CACHED_CLIENT

    with _CLIENT_LOCK:
        if _CACHED_CLIENT is not None:
            return _CACHED_CLIENT

        if not is_s3_configured():
            raise ValueError(
                "S3/B2 storage is not configured. Please set CURATE_S3_ENDPOINT_URL, "
                "CURATE_S3_KEY_ID, CURATE_S3_APP_KEY, and CURATE_S3_BUCKET in .env"
            )

        boto_config = Config(
            region_name=S3_REGION,
            signature_version="s3v4",
            max_pool_connections=max(25, S3_MAX_WORKERS * 2),
            retries={"max_attempts": 5, "mode": "adaptive"},
        )

        _CACHED_CLIENT = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT_URL,
            aws_access_key_id=S3_KEY_ID,
            aws_secret_access_key=S3_APP_KEY,
            config=boto_config,
        )
        return _CACHED_CLIENT


def generate_s3_key(category: str, filename: str) -> str:
    """Generate canonical S3 object key matching CDN layout (images/wallpapers/<Category>/<filename>)."""
    clean_cat = category.strip("/\\ ")
    clean_fn = filename.strip("/\\ ")
    return f"images/wallpapers/{clean_cat}/{clean_fn}"


def generate_s3_key_with_ext(category: str, filename: str, ext: str) -> str:
    """Generate S3 object key with a specific extension (e.g., 'png', 'jpg')."""
    clean_cat = category.strip("/\\ ")
    stem = Path(filename.strip("/\\ ")).stem
    ext = ext.lstrip(".")
    return f"images/wallpapers/{clean_cat}/{stem}.{ext}"


def generate_s3_thumb_key(category: str, filename: str) -> str:
    """Generate S3 object key for a thumbnail (images/thumbs/<Category>/<stem>.webp)."""
    clean_cat = category.strip("/\\ ")
    stem = Path(filename.strip("/\\ ")).stem
    return f"images/thumbs/{clean_cat}/{stem}.webp"


def generate_cdn_url(s3_key: str) -> str:
    """Generate public CDN URL for an S3 object key."""
    clean_key = s3_key.lstrip("/")
    base = CDN_BASE.rstrip("/")
    return f"{base}/{clean_key}"


def guess_content_type(file_path: Path) -> str:
    """Infer MIME content-type from file extension."""
    ext = file_path.suffix.lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".avif": "image/avif",
    }
    return mapping.get(ext, mimetypes.guess_type(str(file_path))[0] or "application/octet-stream")


def upload_file(
    local_path: Path,
    s3_key: str,
    bucket: Optional[str] = None,
    client=None,
) -> Tuple[bool, str, Optional[str]]:
    """Upload a single file to S3/B2 with public cache headers.

    Returns: (success: bool, cdn_url: str, error_message: Optional[str])
    """
    local_path = Path(local_path)
    if not local_path.is_file():
        return False, "", f"Local file not found: {local_path}"

    target_bucket = bucket or S3_BUCKET
    s3 = client or get_s3_client()
    content_type = guess_content_type(local_path)
    cdn_url = generate_cdn_url(s3_key)

    extra_args = {
        "ContentType": content_type,
        "CacheControl": "public, max-age=31536000, immutable",
    }

    try:
        s3.upload_file(
            str(local_path),
            target_bucket,
            s3_key,
            ExtraArgs=extra_args,
        )
        return True, cdn_url, None
    except (ClientError, BotoCoreError, Exception) as e:
        logger.error(f"Failed to upload {local_path} to {s3_key}: {e}")
        return False, "", str(e)


def verify_remote_file(
    s3_key: str,
    expected_size: Optional[int] = None,
    bucket: Optional[str] = None,
    client=None,
) -> Tuple[bool, Optional[int]]:
    """Verify an object exists in S3 and optionally matches expected size."""
    target_bucket = bucket or S3_BUCKET
    s3 = client or get_s3_client()
    try:
        resp = s3.head_object(Bucket=target_bucket, Key=s3_key)
        remote_size = resp.get("ContentLength")
        if expected_size is not None and remote_size != expected_size:
            return False, remote_size
        return True, remote_size
    except Exception:
        return False, None


def create_optimized_thumbnail(local_path: Path, max_dim: int = 640, quality: int = 80) -> bytes:
    """Create an optimized WebP thumbnail in-memory for fast grid rendering."""
    with Image.open(local_path) as im:
        im.draft("RGB", (max_dim, max_dim))
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="WEBP", quality=quality, method=6)
        return buf.getvalue()


def convert_image_bytes(local_path: Path, target_format: str, quality: int = 95) -> bytes:
    """Convert a local image file to the target format (PNG or JPEG) in-memory."""
    fmt_map = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG"}
    pil_fmt = fmt_map.get(target_format.lower())
    if not pil_fmt:
        raise ValueError(f"Unsupported target format: {target_format}")

    with Image.open(local_path) as im:
        if im.mode in ("RGBA", "LA", "P") and pil_fmt == "JPEG":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            if im.mode == "P":
                im = im.convert("RGBA")
            bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
            im = bg
        elif im.mode not in ("RGB", "L") and pil_fmt == "JPEG":
            im = im.convert("RGB")

        buf = BytesIO()
        if pil_fmt == "JPEG":
            im.save(buf, format="JPEG", quality=quality, optimize=True)
        else:
            im.save(buf, format="PNG")
        return buf.getvalue()


def _original_format(local_path: Path) -> str:
    """Return normalized original format: 'png', 'jpg', or 'other'."""
    ext = local_path.suffix.lower().lstrip(".")
    if ext == "png":
        return "png"
    if ext in ("jpg", "jpeg"):
        return "jpg"
    return "other"


def _ensure_format(
    local_path: Path,
    category: str,
    filename: str,
    target_format: str,
    original_url: str,
    bucket: Optional[str],
    s3_client,
) -> Tuple[Optional[str], Optional[str]]:
    """Return (url, error) for target format, reusing the original upload when it matches."""
    if _original_format(local_path) == target_format:
        return original_url, None
    key = generate_s3_key_with_ext(category, filename, target_format)
    content_type = "image/png" if target_format == "png" else "image/jpeg"
    try:
        data = convert_image_bytes(local_path, target_format)
    except Exception as e:
        return None, f"{target_format.upper()} conversion failed: {e}"
    ok, url, err = upload_bytes(data, key, content_type=content_type, bucket=bucket, client=s3_client)
    if not ok:
        return None, f"{target_format.upper()}: {err}"
    return url, None


def upload_multi_format(
    local_path: Path,
    category: str,
    filename: str,
    original_url: str = "",
    bucket: Optional[str] = None,
    client=None,
) -> Dict[str, Any]:
    """Ensure a wallpaper is published in both PNG and JPG full-size formats.

    If the original file is already PNG (resp. JPG), its URL is reused as the
    PNG (resp. JPG) URL and only the missing format is generated & uploaded.
    For other original formats (webp, etc.), both formats are generated.

    Returns dict with keys:
        success: bool (True if both formats resolve to a URL)
        png_url, jpg_url: CDN URLs (None if that format failed)
        errors: list of error messages
    """
    local_path = Path(local_path)
    if not local_path.is_file():
        return {"success": False, "png_url": None, "jpg_url": None, "errors": [f"File not found: {local_path}"]}

    target_bucket = bucket or S3_BUCKET
    s3 = client or get_s3_client()
    original_url = original_url or generate_cdn_url(generate_s3_key(category, filename))
    errors: List[str] = []

    png_url, png_err = _ensure_format(local_path, category, filename, "png", original_url, target_bucket, s3)
    if png_err:
        errors.append(png_err)

    jpg_url, jpg_err = _ensure_format(local_path, category, filename, "jpg", original_url, target_bucket, s3)
    if jpg_err:
        errors.append(jpg_err)

    return {
        "success": bool(png_url and jpg_url),
        "png_url": png_url,
        "jpg_url": jpg_url,
        "errors": errors,
    }


def upload_bytes(
    data: bytes,
    s3_key: str,
    content_type: str = "image/webp",
    bucket: Optional[str] = None,
    client=None,
) -> Tuple[bool, str, Optional[str]]:
    """Upload raw bytes to S3/B2 with public cache headers.

    Returns: (success: bool, cdn_url: str, error_message: Optional[str])
    """
    target_bucket = bucket or S3_BUCKET
    s3 = client or get_s3_client()
    cdn_url = generate_cdn_url(s3_key)
    extra_args = {
        "ContentType": content_type,
        "CacheControl": "public, max-age=31536000, immutable",
    }
    try:
        s3.upload_fileobj(BytesIO(data), target_bucket, s3_key, ExtraArgs=extra_args)
        return True, cdn_url, None
    except Exception as e:
        logger.error(f"Failed to upload bytes to {s3_key}: {e}")
        return False, "", str(e)


def upload_thumbnail(
    local_path: Path,
    category: str,
    filename: str,
    bucket: Optional[str] = None,
    client=None,
) -> Tuple[bool, str, Optional[str]]:
    """Generate and upload a WebP thumbnail for a wallpaper.

    Returns: (success: bool, thumb_cdn_url: str, error_message: Optional[str])
    """
    local_path = Path(local_path)
    if not local_path.is_file():
        return False, "", f"Local file not found: {local_path}"
    try:
        thumb_bytes = create_optimized_thumbnail(local_path)
    except Exception as e:
        return False, "", f"Thumbnail generation failed: {e}"
    thumb_key = generate_s3_thumb_key(category, filename)
    return upload_bytes(thumb_bytes, thumb_key, bucket=bucket, client=client)


def _upload_single_task(item: Dict[str, Any], curated_dir: Path, s3_bucket: str) -> Dict[str, Any]:
    """Worker task to upload one wallpaper and update database."""
    w_id = item["id"]
    category = item.get("category", "")
    curated_fn = item.get("curated_filename") or item.get("filename") or ""

    if not curated_fn:
        return {"id": w_id, "success": False, "error": "Missing filename", "bytes": 0}

    local_path = curated_dir / category / curated_fn
    if not local_path.is_file():
        # Fallback: check Wallpapers/
        fallback_path = curated_dir.parent / "Wallpapers" / category / item.get("filename", "")
        if fallback_path.is_file():
            local_path = fallback_path
        else:
            return {
                "id": w_id,
                "success": False,
                "error": f"Local file not found at {local_path}",
                "bytes": 0,
            }

    s3_key = generate_s3_key(category, curated_fn)
    filesize = local_path.stat().st_size

    success, cdn_url, err = upload_file(local_path, s3_key, bucket=s3_bucket)
    if not success:
        return {"id": w_id, "success": False, "error": err, "bytes": 0}

    # Generate and upload WebP thumbnail
    thumb_success, thumb_url, thumb_err = upload_thumbnail(
        local_path, category, curated_fn, bucket=s3_bucket
    )
    final_thumb_url = thumb_url if thumb_success else cdn_url

    # Generate and upload PNG + JPG full-size variants
    fmt_result = upload_multi_format(local_path, category, curated_fn, bucket=s3_bucket)
    png_url = fmt_result.get("png_url") or cdn_url
    jpg_url = fmt_result.get("jpg_url") or cdn_url

    # Update database record
    update_wallpaper_s3(
        w_id, s3_key, cdn_url, s3_thumb_url=final_thumb_url,
        s3_png_url=png_url, s3_jpg_url=jpg_url,
    )

    return {
        "id": w_id,
        "success": True,
        "s3_key": s3_key,
        "cdn_url": cdn_url,
        "thumb_url": final_thumb_url,
        "png_url": png_url,
        "jpg_url": jpg_url,
        "bytes": filesize,
        "error": None,
    }


def sync_curated_collection(
    workers: int = S3_MAX_WORKERS,
    force_all: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    curated_dir: Path = CURATED_DIR,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """Upload all pending (or all if force_all) curated wallpapers to S3/B2.

    Returns summary metrics dict: {
        "total": int,
        "uploaded": int,
        "skipped": int,
        "failed": int,
        "total_bytes": int,
        "errors": list,
        "elapsed_seconds": float
    }
    """
    if not is_s3_configured():
        raise ValueError("S3/B2 credentials are not configured in environment/.env")

    start_time = time.time()
    curated_dir = Path(curated_dir)

    # 1. Gather candidates from database
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        if force_all:
            cursor.execute(
                """
                SELECT id, category, curated_id, curated_filename, filename, filesize, s3_key
                FROM wallpapers
                WHERE is_curated = 1
                ORDER BY id ASC
                """
            )
        else:
            cursor.execute(
                """
                SELECT id, category, curated_id, curated_filename, filename, filesize, s3_key
                FROM wallpapers
                WHERE is_curated = 1
                  AND (s3_key IS NULL OR s3_key = '')
                ORDER BY id ASC
                """
            )
        candidates = [dict(r) for r in cursor.fetchall()]

    total = len(candidates)
    if total == 0:
        return {
            "total": 0,
            "uploaded": 0,
            "skipped": 0,
            "failed": 0,
            "total_bytes": 0,
            "errors": [],
            "elapsed_seconds": round(time.time() - start_time, 2),
        }

    uploaded = 0
    failed = 0
    total_bytes = 0
    errors: List[str] = []

    # Prime S3 client
    get_s3_client()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(_upload_single_task, item, curated_dir, S3_BUCKET): item
            for item in candidates
        }

        completed = 0
        for fut in as_completed(futures):
            completed += 1
            res = fut.result()
            if res["success"]:
                uploaded += 1
                total_bytes += res["bytes"]
            else:
                failed += 1
                errors.append(f"ID #{res['id']}: {res['error']}")

            if progress_callback:
                progress_callback(
                    completed,
                    total,
                    f"Uploaded {uploaded}/{total} ({round(total_bytes / (1024*1024), 1)} MB)"
                )

    elapsed = round(time.time() - start_time, 2)
    return {
        "total": total,
        "uploaded": uploaded,
        "skipped": total - (uploaded + failed),
        "failed": failed,
        "total_bytes": total_bytes,
        "errors": errors[:50],  # cap returned errors
        "elapsed_seconds": elapsed,
    }


def _sync_thumbnail_task(item: Dict[str, Any], curated_dir: Path, s3_bucket: str) -> Dict[str, Any]:
    """Worker task to generate and upload a thumbnail for one wallpaper."""
    w_id = item["id"]
    category = item.get("category", "")
    curated_fn = item.get("curated_filename") or item.get("filename") or ""
    if not curated_fn:
        return {"id": w_id, "success": False, "error": "Missing filename"}

    local_path = curated_dir / category / curated_fn
    if not local_path.is_file():
        fallback_path = curated_dir.parent / "Wallpapers" / category / item.get("filename", "")
        if fallback_path.is_file():
            local_path = fallback_path
        else:
            return {"id": w_id, "success": False, "error": f"File not found: {local_path}"}

    success, thumb_url, err = upload_thumbnail(local_path, category, curated_fn, bucket=s3_bucket)
    if not success:
        return {"id": w_id, "success": False, "error": err}

    update_wallpaper_s3_thumb(w_id, thumb_url)
    return {"id": w_id, "success": True, "thumb_url": thumb_url}


def sync_thumbnails(
    workers: int = S3_MAX_WORKERS,
    force_all: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    curated_dir: Path = CURATED_DIR,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """Generate and upload WebP thumbnails for curated wallpapers missing them.

    Returns summary dict matching sync_curated_collection format.
    """
    if not is_s3_configured():
        raise ValueError("S3/B2 credentials are not configured in environment/.env")

    start_time = time.time()
    curated_dir = Path(curated_dir)

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        if force_all:
            cursor.execute(
                """
                SELECT id, category, curated_filename, filename
                FROM wallpapers
                WHERE is_curated = 1 AND s3_key IS NOT NULL AND s3_key != ''
                ORDER BY id ASC
                """
            )
        else:
            cursor.execute(
                """
                SELECT id, category, curated_filename, filename
                FROM wallpapers
                WHERE is_curated = 1 AND s3_key IS NOT NULL AND s3_key != ''
                  AND (s3_thumb_url IS NULL OR s3_thumb_url = '' OR s3_thumb_url = s3_url)
                ORDER BY id ASC
                """
            )
        candidates = [dict(r) for r in cursor.fetchall()]

    total = len(candidates)
    if total == 0:
        return {
            "total": 0, "uploaded": 0, "skipped": 0, "failed": 0,
            "total_bytes": 0, "errors": [],
            "elapsed_seconds": round(time.time() - start_time, 2),
        }

    uploaded = 0
    failed = 0
    errors: List[str] = []

    get_s3_client()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(_sync_thumbnail_task, item, curated_dir, S3_BUCKET): item
            for item in candidates
        }
        completed = 0
        for fut in as_completed(futures):
            completed += 1
            res = fut.result()
            if res["success"]:
                uploaded += 1
            else:
                failed += 1
                errors.append(f"ID #{res['id']}: {res['error']}")
            if progress_callback:
                progress_callback(completed, total, f"Thumbnails {uploaded}/{total}")

    return {
        "total": total,
        "uploaded": uploaded,
        "skipped": total - (uploaded + failed),
        "failed": failed,
        "total_bytes": 0,
        "errors": errors[:50],
        "elapsed_seconds": round(time.time() - start_time, 2),
    }


def _sync_multi_format_task(item: Dict[str, Any], curated_dir: Path, s3_bucket: str) -> Dict[str, Any]:
    """Worker task to generate and upload missing PNG and JPG formats for one wallpaper."""
    w_id = item["id"]
    category = item.get("category", "")
    curated_fn = item.get("curated_filename") or item.get("filename") or ""
    if not curated_fn:
        return {"id": w_id, "success": False, "error": "Missing filename"}

    local_path = curated_dir / category / curated_fn
    if not local_path.is_file():
        fallback_path = curated_dir.parent / "Wallpapers" / category / item.get("filename", "")
        if fallback_path.is_file():
            local_path = fallback_path
        else:
            return {"id": w_id, "success": False, "error": f"File not found: {local_path}"}

    original_url = item.get("s3_url") or generate_cdn_url(generate_s3_key(category, curated_fn))
    res = upload_multi_format(local_path, category, curated_fn, original_url=original_url, bucket=s3_bucket)
    if not res["success"]:
        return {"id": w_id, "success": False, "error": "; ".join(res.get("errors", []))}

    update_wallpaper_s3(
        w_id,
        item.get("s3_key") or generate_s3_key(category, curated_fn),
        item.get("s3_url") or generate_cdn_url(generate_s3_key(category, curated_fn)),
        s3_thumb_url=item.get("s3_thumb_url"),
        s3_png_url=res.get("png_url"),
        s3_jpg_url=res.get("jpg_url"),
    )
    return {"id": w_id, "success": True, "png_url": res.get("png_url"), "jpg_url": res.get("jpg_url")}


def sync_multi_formats(
    workers: int = S3_MAX_WORKERS,
    force_all: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    curated_dir: Path = CURATED_DIR,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """Upload missing PNG and JPG full-size formats for curated wallpapers to S3/B2."""
    if not is_s3_configured():
        raise ValueError("S3/B2 credentials are not configured in environment/.env")

    start_time = time.time()
    curated_dir = Path(curated_dir)

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        if force_all:
            cursor.execute(
                """
                SELECT id, category, curated_filename, filename, s3_key, s3_url, s3_thumb_url
                FROM wallpapers
                WHERE is_curated = 1 AND s3_key IS NOT NULL AND s3_key != ''
                ORDER BY id ASC
                """
            )
        else:
            cursor.execute(
                """
                SELECT id, category, curated_filename, filename, s3_key, s3_url, s3_thumb_url
                FROM wallpapers
                WHERE is_curated = 1 AND s3_key IS NOT NULL AND s3_key != ''
                  AND (s3_png_url IS NULL OR s3_png_url = '' OR s3_jpg_url IS NULL OR s3_jpg_url = '')
                ORDER BY id ASC
                """
            )
        candidates = [dict(r) for r in cursor.fetchall()]

    total = len(candidates)
    if total == 0:
        return {
            "total": 0, "uploaded": 0, "skipped": 0, "failed": 0,
            "total_bytes": 0, "errors": [],
            "elapsed_seconds": round(time.time() - start_time, 2),
        }

    uploaded = 0
    failed = 0
    errors: List[str] = []

    get_s3_client()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(_sync_multi_format_task, item, curated_dir, S3_BUCKET): item
            for item in candidates
        }
        completed = 0
        for fut in as_completed(futures):
            completed += 1
            res = fut.result()
            if res["success"]:
                uploaded += 1
            else:
                failed += 1
                errors.append(f"ID #{res['id']}: {res['error']}")
            if progress_callback:
                progress_callback(completed, total, f"Multi-format (PNG+JPG) {uploaded}/{total}")

    return {
        "total": total,
        "uploaded": uploaded,
        "skipped": total - (uploaded + failed),
        "failed": failed,
        "total_bytes": 0,
        "errors": errors[:50],
        "elapsed_seconds": round(time.time() - start_time, 2),
    }
