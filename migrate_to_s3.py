"""Standalone Migration CLI: Upload Curated/ wallpapers to Backblaze B2 (S3-compatible).

Usage:
    py migrate_to_s3.py --dry-run                 # List pending uploads without uploading
    py migrate_to_s3.py --workers 8 --verify      # Upload + verify all files
    py migrate_to_s3.py --force --workers 4       # Force re-upload even if s3_key exists
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys
import time
from typing import Dict, List

from curate_config import CURATED_DIR, S3_BUCKET, S3_MAX_WORKERS
from curate_db import db_session, init_db, update_wallpaper_s3
from curate_s3 import (
    generate_cdn_url,
    generate_s3_key,
    is_s3_configured,
    upload_file,
    verify_remote_file,
)


def get_migration_candidates(force_all: bool) -> List[Dict]:
    """Return list of curated wallpapers needing upload."""
    with db_session() as conn:
        cursor = conn.cursor()
        query = """
            SELECT id, category, curated_filename, filename, filesize, s3_key
            FROM wallpapers
            WHERE is_curated = 1
        """
        if not force_all:
            query += " AND (s3_key IS NULL OR s3_key = '')"
        query += " ORDER BY id ASC"
        cursor.execute(query)
        return [dict(r) for r in cursor.fetchall()]


def upload_single(item: Dict, curated_dir: Path, verify: bool) -> Dict:
    """Upload one file, optionally verify remote size, update DB."""
    w_id = item["id"]
    category = item.get("category", "")
    curated_fn = item.get("curated_filename") or item.get("filename") or ""
    local_path = curated_dir / category / curated_fn

    if not local_path.is_file():
        # Fallback to Wallpapers/ directory
        fallback = curated_dir.parent / "Wallpapers" / category / item.get("filename", "")
        if fallback.is_file():
            local_path = fallback
        else:
            return {
                "id": w_id,
                "success": False,
                "error": f"File not found: {local_path}",
                "bytes": 0,
            }

    s3_key = generate_s3_key(category, curated_fn)
    filesize = local_path.stat().st_size

    success, cdn_url, err = upload_file(local_path, s3_key)
    if not success:
        return {"id": w_id, "success": False, "error": err, "bytes": 0}

    if verify:
        ok, remote_size = verify_remote_file(s3_key, expected_size=filesize)
        if not ok:
            return {
                "id": w_id,
                "success": False,
                "error": f"Verification failed (expected {filesize} bytes, got {remote_size})",
                "bytes": 0,
            }

    update_wallpaper_s3(w_id, s3_key, cdn_url)
    return {"id": w_id, "success": True, "s3_key": s3_key, "cdn_url": cdn_url, "bytes": filesize}


def format_size(bytes_val: int) -> str:
    """Human readable bytes."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} TB"


def main():
    parser = argparse.ArgumentParser(
        description="Migrate curated wallpapers to Backblaze B2 (S3-compatible) + CDN."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=S3_MAX_WORKERS,
        help=f"Number of concurrent upload threads (default: {S3_MAX_WORKERS})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-upload all files (ignore existing s3_key records)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files and sizes that would be uploaded without uploading",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify remote file sizes match local after upload",
    )
    parser.add_argument(
        "--thumbs-only",
        action="store_true",
        help="Generate and upload WebP thumbnails only (skip full-res upload)",
    )
    args = parser.parse_args()

    # Initialize DB schema
    init_db()

    # Check S3 config
    if not is_s3_configured():
        print("ERROR: S3/B2 is not configured. Please set the following in .env:")
        print("  CURATE_S3_ENDPOINT_URL, CURATE_S3_KEY_ID, CURATE_S3_APP_KEY, CURATE_S3_BUCKET")
        sys.exit(1)

    if args.thumbs_only:
        from curate_s3 import sync_thumbnails
        print("Generating and uploading WebP thumbnails...")
        result = sync_thumbnails(workers=args.workers, force_all=args.force)
        print(f"Thumbnail sync complete: {result['uploaded']} uploaded, {result['failed']} failed ({result['elapsed_seconds']}s)")
        if result['errors']:
            print("\nERRORS:")
            for e in result['errors'][:10]:
                print(f"  {e}")
        sys.exit(1 if result['failed'] > 0 else 0)

    if args.dry_run:
        candidates = get_migration_candidates(force_all=args.force)
        total_bytes = sum(c.get("filesize", 0) for c in candidates)
        print(f"DRY RUN: {len(candidates)} files ({format_size(total_bytes)}) would be uploaded.\n")
        for c in candidates[:20]:
            print(f"  [{c['id']:>4}] {c['category']}/{c['curated_filename'] or c['filename']} ({format_size(c.get('filesize', 0))})")
        if len(candidates) > 20:
            print(f"  ... and {len(candidates) - 20} more files")
        return

    candidates = get_migration_candidates(force_all=args.force)
    total = len(candidates)
    if total == 0:
        print("Nothing to upload. All curated wallpapers are already synced.")
        return

    total_bytes = sum(c.get("filesize", 0) for c in candidates)
    print(f"Starting migration of {total} files ({format_size(total_bytes)}) to bucket '{S3_BUCKET}'...\n")

    start_time = time.time()
    uploaded = 0
    failed = 0
    total_uploaded_bytes = 0
    errors: List[str] = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(upload_single, item, CURATED_DIR, args.verify): item
            for item in candidates
        }

        for fut in as_completed(futures):
            res = fut.result()
            if res["success"]:
                uploaded += 1
                total_uploaded_bytes += res["bytes"]
            else:
                failed += 1
                errors.append(f"ID #{res['id']}: {res['error']}")

            # Progress bar
            elapsed = time.time() - start_time
            speed = total_uploaded_bytes / elapsed if elapsed > 0 else 0
            eta = (total - (uploaded + failed)) / (uploaded / elapsed) if uploaded > 0 and elapsed > 0 else 0
            progress = (uploaded + failed) / total * 100

            bar_len = 40
            filled = int(bar_len * progress / 100)
            bar = "█" * filled + "-" * (bar_len - filled)

            print(
                f"\r[{bar}] {progress:5.1f}% | {uploaded + failed}/{total} | "
                f"{format_size(total_uploaded_bytes)} @ {format_size(speed)}/s | "
                f"ETA: {int(eta)}s | Failed: {failed}",
                end="",
                flush=True,
            )

    elapsed = time.time() - start_time
    print("\n")
    print("=" * 60)
    print(f"MIGRATION COMPLETE in {elapsed:.1f}s")
    print(f"  Uploaded: {uploaded}/{total} ({format_size(total_uploaded_bytes)})")
    print(f"  Failed:   {failed}")
    print(f"  Speed:    {format_size(total_uploaded_bytes / elapsed if elapsed > 0 else 0)}/s")

    if errors:
        print("\nERRORS:")
        for e in errors[:10]:
            print(f"  {e}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
