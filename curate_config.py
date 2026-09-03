"""Centralised configuration for Wallpaper Curator Pro (REBUILD_PLAN Step 4).

All values are env-overridable and automatically loaded from .env / .env.local files.
"""

import os
from pathlib import Path

# Allow high-resolution 8K/16K wallpapers without Pillow DecompressionBomb warnings
try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
except ImportError:
    pass


def _load_env_file(file_path: Path) -> None:
    """Lightweight zero-dependency .env parser."""
    if not file_path.exists() or not file_path.is_file():
        return
    try:
        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Unquote if quoted
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                # Don't override explicit process environment variables
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


# Base project directory
BASE_DIR = Path(os.environ.get("CURATE_BASE_DIR", str(Path(__file__).resolve().parent)))

# Auto-load .env and .env.local on startup
_load_env_file(BASE_DIR / ".env")
_load_env_file(BASE_DIR / ".env.local")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default
DB_PATH = Path(_env("CURATE_DB_PATH", str(BASE_DIR / "wallpapers.db")))
WALLPAPERS_DIR = Path(_env("CURATE_WALLPAPERS_DIR", str(BASE_DIR / "Wallpapers")))
CURATED_DIR = Path(_env("CURATE_CURATED_DIR", str(BASE_DIR / "Curated")))
INCOMING_DIR = Path(_env("CURATE_INCOMING_DIR", str(BASE_DIR / "incoming")))
WEB_DIR = Path(_env("CURATE_WEB_DIR", str(BASE_DIR / "web")))
README_PATH = Path(_env("CURATE_README_PATH", str(BASE_DIR / "README.md")))
GITHUB_README_PATH = Path(_env("CURATE_GITHUB_README", str(BASE_DIR / ".github" / "README.md")))

# ==============================================================================
# SERVER
# ==============================================================================

HOST = _env("CURATE_HOST", "127.0.0.1")
PORT = _env_int("CURATE_PORT", 8888)
MIN_PIXELS = _env_int("CURATE_MIN_PIXELS", 2560 * 1440)  # 3,686,400

# ==============================================================================
# GIT / PUBLISH
# ==============================================================================

GIT_BRANCH = _env("CURATE_GIT_BRANCH", "main")
GIT_REMOTE = _env("CURATE_GIT_REMOTE", "origin")
GIT_CONFIRM_TOKEN = _env("CURATE_GIT_CONFIRM", "")  # must match for git-push
GIT_MAX_PUSH_MIN = _env_int("CURATE_GIT_MAX_PUSH_MIN", 60)  # min minutes between pushes

# ==============================================================================
# USER-AGENTS
# ==============================================================================

UA_DESKTOP = _env(
    "CURATE_UA_DESKTOP",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 WallpaperCurator/2.0",
)
UA_AGENT = _env("CURATE_UA_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WallpaperAgent/1.0")

# ==============================================================================
# EXTERNAL SOURCES & API KEYS
# ==============================================================================

WALLHAVEN_API_KEY = _env("WALLHAVEN_API_KEY", "")
DEVIANTART_CLIENT_ID = _env("DEVIANTART_CLIENT_ID", "")
DEVIANTART_CLIENT_SECRET = _env("DEVIANTART_CLIENT_SECRET", "")

# ==============================================================================
# S3 / BACKBLAZE B2 STORAGE & CDN
# ==============================================================================

S3_ENDPOINT_URL = _env("CURATE_S3_ENDPOINT_URL", _env("AWS_ENDPOINT_URL", ""))
S3_KEY_ID = _env("CURATE_S3_KEY_ID", _env("AWS_ACCESS_KEY_ID", ""))
S3_APP_KEY = _env("CURATE_S3_APP_KEY", _env("AWS_SECRET_ACCESS_KEY", ""))
S3_BUCKET = _env("CURATE_S3_BUCKET", _env("AWS_BUCKET_NAME", ""))
S3_REGION = _env("CURATE_S3_REGION", _env("AWS_DEFAULT_REGION", "us-east-005"))
CDN_BASE = _env("CURATE_CDN_BASE", "https://cdn.skiddle.id").rstrip("/")
S3_MAX_WORKERS = _env_int("CURATE_S3_MAX_WORKERS", 8)

