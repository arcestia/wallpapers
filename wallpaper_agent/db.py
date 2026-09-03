"""SQLite Database Manager for Wallpaper Metadata (Delegates to curate_db)."""

from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple
import sqlite3

from curate_db import (
    db_session,
    find_visual_duplicates,
    get_all_wallpapers,
    get_connection,
    get_next_id,
    get_stats,
    get_wallpaper_by_id,
    get_wallpaper_by_sha256,
    init_db,
    insert_wallpaper,
)
from .config import DB_PATH

__all__ = [
    "get_connection",
    "db_session",
    "init_db",
    "get_next_id",
    "insert_wallpaper",
    "get_wallpaper_by_id",
    "get_wallpaper_by_sha256",
    "find_visual_duplicates",
    "get_all_wallpapers",
    "get_stats",
    "DB_PATH",
]
