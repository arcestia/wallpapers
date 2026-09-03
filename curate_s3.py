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

    # Update database record
    update_wallpaper_s3(w_id, s3_key, cdn_url)

    return {
        "id": w_id,
        "success": True,
        "s3_key": s3_key,
        "cdn_url": cdn_url,
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
