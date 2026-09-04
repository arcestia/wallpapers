"""Unified SQLite Database Manager for Wallpaper Curator Pro & Agent.

Features:
- WAL mode (Write-Ahead Logging) for concurrent reads & writes
- busy_timeout=5000ms to eliminate "database is locked" errors
- Connection context manager with clean commit/rollback/close
- Auto-migration ensuring all columns & indexes exist across legacy schemas
"""

from contextlib import contextmanager
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, Generator, List, Optional, Tuple

logger = logging.getLogger("curate.db")

# Serializes "SELECT MAX(id) + 1 -> store file -> INSERT" across threads.
# SQLite assigns rowids atomically on INSERT, but the filename is derived from
# the ID *before* the row exists, so allocation and insert must not interleave.
ID_ALLOCATION_LOCK = threading.Lock()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "wallpapers.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Create a configured SQLite connection with WAL mode and row factory."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Configure concurrency pragmas
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def db_session(db_path: Path = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Thread-safe context manager ensuring automatic commit or rollback."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==============================================================================
# SCHEMA & MIGRATIONS
# ==============================================================================

EXPECTED_COLUMNS = {
    "id": "INTEGER PRIMARY KEY",
    "filename": "TEXT NOT NULL",
    "type": "TEXT DEFAULT 'UNKNOWN'",
    "category": "TEXT NOT NULL",
    "width": "INTEGER",
    "height": "INTEGER",
    "format": "TEXT",
    "filesize": "INTEGER",
    "sha256": "TEXT UNIQUE NOT NULL",
    "perceptual_hash": "TEXT",
    "source": "TEXT",
    "source_url": "TEXT",
    "ai_confidence": "REAL",
    "duplicate_of": "INTEGER",
    "aspect_ratio": "TEXT",
    "orientation": "TEXT",
    "license": "TEXT",
    "author": "TEXT",
    "title": "TEXT",
    "original_filename": "TEXT",
    "is_curated": "INTEGER DEFAULT 0",
    "curated_id": "INTEGER",
    "curated_filename": "TEXT",
    "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "s3_key": "TEXT",
    "s3_url": "TEXT",
    "s3_thumb_url": "TEXT",
    "s3_png_url": "TEXT",
    "s3_jpg_url": "TEXT",
    "s3_uploaded_at": "TIMESTAMP",
}


def init_db(db_path: Path = DB_PATH) -> None:
    """Initialize database and auto-migrate all columns and indexes."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()

        # 1. Create base table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wallpapers (
                id INTEGER PRIMARY KEY,
                filename TEXT NOT NULL,
                type TEXT DEFAULT 'UNKNOWN',
                category TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                format TEXT NOT NULL,
                filesize INTEGER NOT NULL,
                sha256 TEXT UNIQUE NOT NULL,
                perceptual_hash TEXT,
                source TEXT,
                source_url TEXT,
                ai_confidence REAL,
                duplicate_of INTEGER,
                aspect_ratio TEXT,
                orientation TEXT,
                license TEXT,
                author TEXT,
                title TEXT,
                original_filename TEXT,
                is_curated INTEGER DEFAULT 0,
                curated_id INTEGER,
                curated_filename TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Check and migrate missing columns
        cursor.execute("PRAGMA table_info(wallpapers)")
        existing_cols = {row["name"] for row in cursor.fetchall()}

        for col_name, col_def in EXPECTED_COLUMNS.items():
            if col_name not in existing_cols:
                # Add column dynamically (strip PRIMARY KEY / UNIQUE for ALTER TABLE)
                clean_def = col_def.replace("PRIMARY KEY", "").replace("UNIQUE", "").strip()
                if "NOT NULL" in clean_def and "DEFAULT" not in clean_def:
                    clean_def = clean_def.replace("NOT NULL", "").strip()
                try:
                    cursor.execute(f"ALTER TABLE wallpapers ADD COLUMN {col_name} {clean_def}")
                    logger.info(f"Added missing column '{col_name}' to wallpapers")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Could not add column {col_name}: {e}")

        # 3. Create all standard indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wallpapers_sha256 ON wallpapers(sha256)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wallpapers_type ON wallpapers(type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wallpapers_category ON wallpapers(category)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wallpapers_phash ON wallpapers(perceptual_hash)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wallpapers_curated ON wallpapers(is_curated)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wallpapers_s3_key ON wallpapers(s3_key)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wallpapers_curated_unique ON wallpapers(category, curated_filename) WHERE is_curated = 1 AND curated_filename IS NOT NULL")


def sync_curated_folder(curated_dir: Path, db_path: Path = DB_PATH) -> int:
    """Sync Curated/ folder to DB by SHA-256 hash only.

    Matches each file on disk to a wallpaper row by SHA-256.
    If a file is found, the row is marked as curated with the correct
    curated_filename. Rows without a matching file are left untouched.
    """
    import hashlib
    curated_dir = Path(curated_dir)
    if not curated_dir.exists():
        return 0

    with db_session(db_path) as conn:
        cursor = conn.cursor()

        # Build hash -> row id map
        cursor.execute("SELECT id, sha256 FROM wallpapers")
        sha_to_row = {row["sha256"]: row["id"] for row in cursor.fetchall()}

        updated = 0
        for cat_dir in curated_dir.iterdir():
            if cat_dir.is_dir():
                for img in cat_dir.iterdir():
                    if img.is_file() and img.name != ".gitkeep":
                        h = hashlib.sha256(img.read_bytes()).hexdigest()
                        row_id = sha_to_row.get(h)
                        if row_id is not None:
                            cursor.execute(
                                "UPDATE wallpapers SET is_curated = 1, curated_filename = ? WHERE id = ?",
                                (img.name, row_id),
                            )
                            updated += cursor.rowcount
        return updated


# ==============================================================================
# CRUD & QUERY HELPERS
# ==============================================================================

def get_next_id(db_path: Path = DB_PATH) -> int:
    """Get the next sequential permanent database ID.

    Must be called while holding ID_ALLOCATION_LOCK, and the lock must be
    held until the corresponding INSERT commits (see pipeline.process_image).
    """
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(id) FROM wallpapers")
        row = cursor.fetchone()
        max_id = row[0]
        return 1 if max_id is None else max_id + 1


def get_wallpaper_by_id(wallpaper_id: int, db_path: Path = DB_PATH) -> Optional[Dict[str, Any]]:
    """Retrieve a single wallpaper record by ID."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM wallpapers WHERE id = ?", (wallpaper_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_wallpaper_by_sha256(sha256_hash: str, db_path: Path = DB_PATH) -> Optional[Dict[str, Any]]:
    """Retrieve a single wallpaper record by SHA-256 hash."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM wallpapers WHERE sha256 = ?", (sha256_hash,))
        row = cursor.fetchone()
        return dict(row) if row else None


def insert_wallpaper(metadata: Dict[str, Any], db_path: Path = DB_PATH) -> int:
    """Insert a new wallpaper record, filtering to known columns."""
    insert_data = {k: v for k, v in metadata.items() if k in EXPECTED_COLUMNS and k != "created_at"}
    columns = ", ".join(insert_data.keys())
    placeholders = ", ".join("?" for _ in insert_data)
    values = list(insert_data.values())

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"INSERT INTO wallpapers ({columns}) VALUES ({placeholders})",
            values,
        )
        return insert_data.get("id", cursor.lastrowid)


def find_visual_duplicates(
    target_phash: str,
    max_distance: int = 5,
    db_path: Path = DB_PATH,
) -> List[Tuple[Dict[str, Any], int]]:
    """Find wallpapers with perceptual hash within hamming distance threshold."""
    if not target_phash:
        return []

    try:
        import imagehash
        target_hash_obj = imagehash.hex_to_hash(target_phash)
    except Exception:
        return []

    matches = []
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM wallpapers WHERE perceptual_hash IS NOT NULL AND perceptual_hash != ''"
        )
        for row in cursor.fetchall():
            row_dict = dict(row)
            phash_str = row_dict.get("perceptual_hash")
            if not phash_str:
                continue
            try:
                row_hash_obj = imagehash.hex_to_hash(phash_str)
                dist = target_hash_obj - row_hash_obj
                if dist <= max_distance:
                    matches.append((row_dict, dist))
            except Exception:
                continue

    matches.sort(key=lambda item: item[1])
    return matches


def get_all_wallpapers(db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    """Retrieve all wallpaper records."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM wallpapers ORDER BY id ASC")
        return [dict(row) for row in cursor.fetchall()]


def get_stats(db_path: Path = DB_PATH) -> Dict[str, Any]:
    """Retrieve summary counts and metrics for the library."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), SUM(filesize) FROM wallpapers")
        r = cursor.fetchone()
        total_count = r[0] or 0
        total_size = r[1] or 0

        cursor.execute("SELECT COUNT(*) FROM wallpapers WHERE is_curated = 1")
        curated_count = cursor.fetchone()[0] or 0

        return {
            "total_wallpapers": total_count,
            "total_size_bytes": total_size,
            "curated_wallpapers": curated_count,
        }


def update_wallpaper_s3(
    wallpaper_id: int,
    s3_key: str,
    s3_url: str,
    s3_thumb_url: Optional[str] = None,
    s3_png_url: Optional[str] = None,
    s3_jpg_url: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> bool:
    """Record S3 object key, CDN URLs, and optional format-specific URLs on a wallpaper row."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        fields = ["s3_key = ?", "s3_url = ?", "s3_uploaded_at = CURRENT_TIMESTAMP"]
        params: List[Any] = [s3_key, s3_url]
        if s3_thumb_url:
            fields.append("s3_thumb_url = ?")
            params.append(s3_thumb_url)
        if s3_png_url:
            fields.append("s3_png_url = ?")
            params.append(s3_png_url)
        if s3_jpg_url:
            fields.append("s3_jpg_url = ?")
            params.append(s3_jpg_url)
        params.append(wallpaper_id)
        cursor.execute(
            f"UPDATE wallpapers SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        return cursor.rowcount > 0


def update_wallpaper_s3_formats(
    wallpaper_id: int,
    s3_png_url: Optional[str] = None,
    s3_jpg_url: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> bool:
    """Record format-specific CDN URLs for a wallpaper."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        fields = []
        params: List[Any] = []
        if s3_png_url:
            fields.append("s3_png_url = ?")
            params.append(s3_png_url)
        if s3_jpg_url:
            fields.append("s3_jpg_url = ?")
            params.append(s3_jpg_url)
        if not fields:
            return False
        params.append(wallpaper_id)
        cursor.execute(
            f"UPDATE wallpapers SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        return cursor.rowcount > 0


def update_wallpaper_s3_thumb(
    wallpaper_id: int,
    s3_thumb_url: str,
    db_path: Path = DB_PATH,
) -> bool:
    """Record CDN thumbnail URL for a wallpaper."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE wallpapers SET s3_thumb_url = ? WHERE id = ?",
            (s3_thumb_url, wallpaper_id),
        )
        return cursor.rowcount > 0


def get_unsynced_curated_wallpapers(db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    """Return curated wallpapers that have not yet been uploaded to S3/B2."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, category, curated_id, curated_filename, filename, filesize
            FROM wallpapers
            WHERE is_curated = 1
              AND (s3_key IS NULL OR s3_key = '')
            ORDER BY id ASC
            """
        )
        return [dict(row) for row in cursor.fetchall()]
