"""
Wallpaper Curator Pro - Command Bar, Ingestion Engine, AI Classifier & Real-Time Analytics.
Ultra-fast, distraction-free curation tool with Linear/Raycast dark glassmorphism design.

Key Features:
- 📥 Multi-Source Ingestion Engine:
    * Wallhaven Search & Bulk Ingest (2K/4K/8K, purity, aspect ratio, sorting filters)
    * Wallhaven Direct IDs / URLs
    * Direct Web Image URLs
    * Local Folder & Incoming directory scanner
- 🤖 Intelligent AI & Rule-Based Classifier:
    * Single-click AI category detection on Card Hover and in Lightbox
    * Batch Auto-Classifier with live interactive review table & 1-click apply
    * Multi-signal detection (Keyword heuristics, EXIF provenance, zero-shot vision)
- ⚡ ⌘K Command Bar: Raycast-style instant command palette (Ctrl+K or /)
- 🎛️ Card Hover Quick-Actions: Instant Approve, Reject, Classify & Inspect
- 📐 Grid Density: S / M / L toggle for compact or large thumbnail cards
- 🔍 Fullscreen Lightbox Focus Mode with complete metadata inspector
- 📂 Category Sidebar with live status progress bars
- ▦ Multi-Select with Shift-Click range selection & Batch Floating Action Bar
- 📊 Real-Time Analytics Dashboard: Metrics, resolution distribution & progress
- 🔢 Automatic Sequential Renumbering in Curated/<Category>/ (1.png, 2.jpg)
- 🚀 Auto-Sync with README.md & GitHub Publish
"""

from collections import Counter
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse, urlencode
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import webbrowser

from PIL import Image
Image.MAX_IMAGE_PIXELS = None  # Allow processing legitimate 8K/16K ultra-high-resolution wallpapers

# Force UTF-8 stdout on Windows so emoji banners don't crash the server
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "wallpapers.db"
WALLPAPERS_DIR = BASE_DIR / "Wallpapers"
CURATED_DIR = BASE_DIR / "Curated"
INCOMING_DIR = BASE_DIR / "incoming"
WEB_DIR = BASE_DIR / "web"
THUMBS_DIR = BASE_DIR / ".thumbs"
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

# Unified Category Registry (Single Source of Truth)
from curate_categories import (
    CATEGORIES,
    CATEGORY_DESCRIPTIONS,
    get_categories_api_payload,
    normalize_category_hint,
)

# Unified DB, ingestion pipeline & classifier imports (REBUILD_PLAN Steps 2 & 3)
from curate_db import (
    db_session,
    get_next_id as get_next_db_id,
    init_db,
)
from curate_ingest import (
    DEFAULT_USER_AGENT,
    get_image_info_pure,
    safe_download_url,
    validate_and_ingest_image_file,
    validate_image_unified,
)
from curate_categories import CATEGORY_PATTERNS as CATEGORY_KEYWORD_MAP
from wallpaper_agent.classifier import (
    classify_ai,
    classify_category_detailed,
    classify_image as agent_classify_image,
)
from wallpaper_agent.downloaders.wallhaven import WallhavenDownloader
from wallpaper_agent.validator import validate_image as agent_validate_image
from wallpaper_agent.deduplicator import check_duplicates as agent_check_duplicates
from curate_config import WALLHAVEN_API_KEY

AGENT_AVAILABLE = True


# ==============================================================================
# BACKGROUND TASK MANAGER (WITH STATE PERSISTENCE)
# ==============================================================================

TASK_STATE_FILE = INCOMING_DIR / ".task_state.json"


class TaskManager:
    """Thread-safe background task manager with state persistence for restart resilience."""

    def __init__(self):
        self.lock = threading.RLock()
        self.task_id = None
        self.task_name = "Idle"
        self.task_type = "idle"
        self.status = "idle"  # idle, running, completed, failed, cancelled, interrupted
        self.progress = 0     # 0 to 100
        self.total = 0
        self.completed_count = 0
        self.failed_count = 0
        self.duplicate_count = 0
        self.current_item = ""
        self.logs = []
        self.result = {}
        self.cancel_requested = False
        self._thread = None
        self._load_state()

    def _load_state(self):
        """Load persisted state on startup; mark running tasks as interrupted."""
        if not TASK_STATE_FILE.exists():
            return
        try:
            data = json.loads(TASK_STATE_FILE.read_text(encoding="utf-8"))
            if data.get("status") == "running":
                self.status = "interrupted"
                self.task_name = data.get("task_name", "Unknown")
                self.logs = data.get("logs", [])
                self.log(f"⚠️ Previous task '{self.task_name}' was interrupted by restart.")
        except Exception:
            pass

    def _save_state(self):
        """Persist current state to disk."""
        try:
            TASK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "task_id": self.task_id,
                "task_name": self.task_name,
                "task_type": self.task_type,
                "status": self.status,
                "progress": self.progress,
                "total": self.total,
                "completed": self.completed_count,
                "failed": self.failed_count,
                "duplicates": self.duplicate_count,
                "current_item": self.current_item,
                "logs": self.logs[-300:],
                "result": self.result,
                "timestamp": time.time(),
            }
            TASK_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _persist(self):
        """Save state if running or recently completed."""
        if self.status in ("running", "completed", "failed", "cancelled", "interrupted"):
            self._save_state()

    def start_task(self, name: str, task_type: str, target_fn, *args, **kwargs):
        with self.lock:
            if self.status == "running":
                return False, "Another task is already running."
            self.task_id = f"task_{int(time.time())}"
            self.task_name = name
            self.task_type = task_type
            self.status = "running"
            self.progress = 0
            self.total = 0
            self.completed_count = 0
            self.failed_count = 0
            self.duplicate_count = 0
            self.current_item = "Starting..."
            self.logs = [f"[{time.strftime('%H:%M:%S')}] 🚀 Task started: {name}"]
            self.result = {}
            self.cancel_requested = False
            self._save_state()

        def runner():
            try:
                res = target_fn(self, *args, **kwargs)
                with self.lock:
                    if self.cancel_requested:
                        self.status = "cancelled"
                        self.log("⚠️ Task cancelled by user.")
                    else:
                        self.status = "completed"
                        self.progress = 100
                        self.log("✨ Task completed successfully!")
                    self.result = res or {}
                    self._persist()
            except Exception as e:
                with self.lock:
                    self.status = "failed"
                    self.log(f"❌ Task error: {str(e)}")
                    self.result = {"error": str(e)}
                    self._persist()

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        return True, self.task_id

    def cancel(self):
        with self.lock:
            if self.status == "running":
                self.cancel_requested = True
                self.log("⏳ Cancellation requested...")
                self._persist()
                return True
            return False

    def log(self, msg: str):
        timestamp = time.strftime('%H:%M:%S')
        with self.lock:
            self.logs.append(f"[{timestamp}] {msg}")
            if len(self.logs) > 300:
                self.logs.pop(0)
            self._persist()

    def set_progress(self, processed: int, total: int, current_name: str = "", completed: int = None, duplicates: int = None, failed: int = None):
        with self.lock:
            self.total = total
            self.current_item = current_name
            if completed is not None:
                self.completed_count = completed
            if duplicates is not None:
                self.duplicate_count = duplicates
            if failed is not None:
                self.failed_count = failed
            if total > 0:
                self.progress = min(100, int((processed / total) * 100))
            self._persist()

    def get_status(self):
        with self.lock:
            return {
                "task_id": self.task_id,
                "task_name": self.task_name,
                "task_type": self.task_type,
                "status": self.status,
                "progress": self.progress,
                "total": self.total,
                "completed": self.completed_count,
                "failed": self.failed_count,
                "duplicates": self.duplicate_count,
                "current_item": self.current_item,
                "logs": self.logs[-60:],  # Return last 60 logs
                "result": self.result,
                "cancel_requested": self.cancel_requested
            }

GLOBAL_TASK_MANAGER = TaskManager()


# ==============================================================================
# DATABASE & STORAGE MANAGEMENT (DELEGATED TO curate_db)
# ==============================================================================

def init_curation_db():
    """Ensure database has all necessary columns, indexes, and syncs curated files.

    NOTE: The startup Curated/ rescan has been removed (REBUILD_PLAN Step 5).
    To force a re-sync, call sync_curated_folder(CURATED_DIR, DB_PATH) explicitly.
    """
    init_db(DB_PATH)


def get_next_curated_id(category_name: str) -> int:
    """Find the next sequential number (1, 2, 3...) for Curated/<category>/."""
    cat_dir = CURATED_DIR / category_name
    if not cat_dir.exists():
        return 1

    existing_numbers = []
    for f in cat_dir.iterdir():
        if f.is_file() and f.name != ".gitkeep":
            stem = f.stem
            if stem.isdigit():
                existing_numbers.append(int(stem))

    return (max(existing_numbers) + 1) if existing_numbers else 1


# ==============================================================================
# CLASSIFIER & INGESTION UTILITIES (DELEGATED TO curate_ingest & wallpaper_agent)
# ==============================================================================

def classify_wallpaper_data(file_path: Path, source: str = "", source_url: str = "", tags: list = None):
    """Classify image using the unified classifier (detailed signals for the UI)."""
    category, method, cat_conf, keywords = classify_category_detailed(
        file_path, source_url=source_url, tags=tags
    )
    ai_type, ai_conf, ai_signal = classify_ai(file_path, source, source_url, tags)

    return {
        "category": category,
        "type": ai_type,
        "ai_confidence": ai_conf,
        "detected_signals": f"[{method} {cat_conf}%] {category}: {', '.join(keywords[:8]) if keywords else 'no textual match'} | AI: {ai_signal}",
        "category_confidence": cat_conf,
        "keywords": keywords,
        "method": method,
    }


def run_wallhaven_ingest_task(
    tm: TaskManager,
    query: str = "",
    categories: str = "111",
    purity: str = "100",
    sorting: str = "toplist",
    top_range: str = "1M",
    atleast: str = "2560x1440",
    ratios: str = "",
    limit: int = 10,
    category_hint: str = None
):
    """Search Wallhaven.cc API and download/ingest wallpapers up to limit."""
    tm.log(f"🔍 Searching Wallhaven: query='{query or 'All'}', sorting={sorting}, purity={purity}, limit={limit}...")
    if WALLHAVEN_API_KEY:
        tm.log("🔑 Wallhaven API key active — including apikey for full fidelity.")

    api_base = "https://wallhaven.cc/api/v1/search"
    params = {
        "categories": categories,
        "purity": purity,
        "sorting": sorting,
        "order": "desc",
        "atleast": atleast or "2560x1440",
        "page": 1
    }
    if query:
        params["q"] = query
    if sorting == "toplist" and top_range:
        params["topRange"] = top_range
    if ratios:
        params["ratios"] = ratios
    if WALLHAVEN_API_KEY:
        params["apikey"] = WALLHAVEN_API_KEY

    temp_incoming = INCOMING_DIR / "wallhaven_temp"
    temp_incoming.mkdir(parents=True, exist_ok=True)

    completed = 0
    failed = 0
    duplicates = 0
    processed = 0
    current_page = 1
    max_pages = max(1, (limit // 24) + 2)

    while processed < limit and current_page <= max_pages:
        if tm.cancel_requested:
            break

        params["page"] = current_page
        req_url = f"{api_base}?{urlencode(params)}"
        tm.log(f"📡 Fetching Wallhaven page {current_page}...")

        try:
            req = urllib.request.Request(req_url, headers={"User-Agent": "Mozilla/5.0 WallpaperCurator/2.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            tm.log(f"❌ Wallhaven API request error: {e}")
            break

        items = data.get("data", [])
        if not items:
            tm.log("ℹ️ No more items found on Wallhaven.")
            break

        for item in items:
            if processed >= limit or tm.cancel_requested:
                break

            wh_id = item.get("id")
            img_url = item.get("path")
            res_str = item.get("resolution", "Unknown")
            source_url = item.get("url") or f"https://wallhaven.cc/w/{wh_id}"
            ext = img_url.split(".")[-1] if "." in img_url else "jpg"
            temp_file = temp_incoming / f"wh_{wh_id}.{ext}"

            processed += 1
            tm.set_progress(processed, limit, f"[{processed}/{limit}] Wallhaven #{wh_id} ({res_str})", completed=completed, duplicates=duplicates, failed=failed)
            tm.log(f"⬇️ Downloading [{processed}/{limit}] Wallhaven #{wh_id} ({res_str})...")

            if not safe_download_url(img_url, temp_file, timeout=15):
                failed += 1
                tm.log(f"❌ Download failed for Wallhaven #{wh_id} (timeout/CDN error)")
                tm.set_progress(processed, limit, f"[{processed}/{limit}] Wallhaven #{wh_id} ({res_str})", completed=completed, duplicates=duplicates, failed=failed)
                continue

            # Ingest through pipeline
            ing_res = validate_and_ingest_image_file(
                file_path=temp_file,
                source="Wallhaven",
                source_url=source_url,
                category_hint=category_hint or (query if query in CATEGORIES else None),
                move=True
            )

            if ing_res["status"] == "COMPLETED":
                completed += 1
                tm.log(f"✅ Ingested ID #{ing_res['id']} into {ing_res['category']} ({ing_res['resolution']})")
            elif ing_res["status"] == "DUPLICATE":
                duplicates += 1
                tm.log(f"⚠️ Skipped duplicate: {ing_res['reason']}")
            else:
                failed += 1
                tm.log(f"❌ Rejected: {ing_res['reason']}")

            tm.set_progress(processed, limit, f"[{processed}/{limit}] Wallhaven #{wh_id} ({res_str})", completed=completed, duplicates=duplicates, failed=failed)
            time.sleep(0.15)

        current_page += 1

    # Cleanup temp dir
    try:
        shutil.rmtree(temp_incoming)
    except Exception:
        pass

    return {
        "source": "Wallhaven",
        "requested_limit": limit,
        "completed": completed,
        "failed": failed,
        "duplicates": duplicates
    }


def run_source_ingest_task(
    tm: TaskManager,
    sources: list,
    query: str = "",
    limit_per_source: int = 10,
    category_hint: str = None,
    include_mature: bool = False,
    sort: str = "top",
    time_range: str = "month",
    subreddit: str = None,
):
    """Fetch and ingest wallpapers across multiple automatic source adapters (Reddit, DeviantArt, etc.)."""
    from wallpaper_agent.sources import get_source_registry

    registry = get_source_registry()
    active_sources = [s for s in sources if registry.get(s) and registry.get(s).is_configured()]

    if not active_sources:
        tm.log("❌ No configured source adapters selected.")
        return {"completed": 0, "failed": 0, "duplicates": 0, "error": "No configured sources selected"}

    tm.log(f"🚀 Starting Multi-Source Ingest across {len(active_sources)} source(s): {', '.join(active_sources)}...")
    temp_incoming = INCOMING_DIR / "sources_temp"
    temp_incoming.mkdir(parents=True, exist_ok=True)

    completed = 0
    failed = 0
    duplicates = 0
    total_requested = len(active_sources) * limit_per_source
    processed = 0

    for s_key in active_sources:
        if tm.cancel_requested:
            break

        adapter = registry.get(s_key)
        tm.log(f"🔍 Searching {adapter.source_name} (query='{query or 'Popular'}', sort='{sort}', limit={limit_per_source})...")

        res = adapter.search(
            query=query,
            limit=limit_per_source,
            category_hint=category_hint,
            include_mature=include_mature,
            sort=sort,
            time_range=time_range,
            subreddit=subreddit,
        )

        if not res.success:
            tm.log(f"⚠️ Search error on {adapter.source_name}: {res.error}")
            continue

        if not res.items:
            tm.log(f"ℹ️ No items found on {adapter.source_name}.")
            continue

        tm.log(f"📥 Found {len(res.items)} items on {adapter.source_name}. Processing downloads...")

        for item in res.items:
            if tm.cancel_requested:
                break

            processed += 1
            item_desc = f"{item.source_name} #{item.external_id} — {item.title or 'Artwork'}"
            tm.set_progress(processed, total_requested, item_desc, completed=completed, duplicates=duplicates, failed=failed)
            tm.log(f"⬇️ Downloading [{processed}/{total_requested}] {item_desc}...")

            download_url = adapter.resolve_download_url(item)
            if not download_url:
                failed += 1
                tm.log(f"❌ Could not resolve download URL for #{item.external_id}")
                tm.set_progress(processed, total_requested, item_desc, completed=completed, duplicates=duplicates, failed=failed)
                continue

            # Determine file extension
            url_clean = download_url.split("?")[0]
            ext = url_clean.split(".")[-1].lower() if "." in url_clean else "jpg"
            if len(ext) > 4 or "/" in ext:
                ext = "jpg"

            temp_file = temp_incoming / f"{s_key}_{item.external_id}_{int(time.time()*1000)}.{ext}"

            if not safe_download_url(download_url, temp_file, timeout=20):
                failed += 1
                tm.log(f"❌ Download failed for {item_desc} (timeout/CDN error)")
                tm.set_progress(processed, total_requested, item_desc, completed=completed, duplicates=duplicates, failed=failed)
                continue

            ing_res = validate_and_ingest_image_file(
                file_path=temp_file,
                source=f"{item.source_name} (@{item.author})" if item.author else item.source_name,
                source_url=item.page_url,
                category_hint=category_hint or item.category_hint,
                move=True,
                title=item.title,
                author=item.author,
                license=item.license,
                tags=item.tags,
            )

            if ing_res["status"] == "COMPLETED":
                completed += 1
                tm.log(f"✅ Ingested #{ing_res['id']} into {ing_res['category']} ({ing_res.get('resolution','')})")
            elif ing_res["status"] == "DUPLICATE":
                duplicates += 1
                tm.log(f"⚠️ Skipped duplicate: {ing_res['reason']}")
            else:
                failed += 1
                tm.log(f"❌ Rejected: {ing_res['reason']}")

            tm.set_progress(processed, total_requested, item_desc, completed=completed, duplicates=duplicates, failed=failed)
            time.sleep(0.1)

    try:
        shutil.rmtree(temp_incoming)
    except Exception:
        pass

    return {
        "completed": completed,
        "failed": failed,
        "duplicates": duplicates,
        "sources": active_sources,
    }


def run_wallhaven_ids_task(tm: TaskManager, items: list, category_hint: str = None):
    """Ingest specific Wallhaven IDs or URLs."""
    total = len(items)
    tm.log(f"📋 Ingesting {total} Wallhaven IDs/URLs...")
    completed = 0
    failed = 0
    duplicates = 0

    temp_incoming = INCOMING_DIR / "wallhaven_ids_temp"
    temp_incoming.mkdir(parents=True, exist_ok=True)

    processed = 0
    for idx, raw_item in enumerate(items):
        if processed >= total or tm.cancel_requested:
            break

        clean = raw_item.strip()
        if not clean:
            continue

        match = re.search(r"(?:/w/|whvn\.cc/|^)([a-zA-Z0-9]{5,8})", clean)
        wh_id = match.group(1) if match else clean

        processed += 1
        tm.set_progress(processed, total, f"[{processed}/{total}] Wallhaven ID {wh_id}", completed=completed, duplicates=duplicates, failed=failed)
        tm.log(f"📡 Fetching [{processed}/{total}] Wallhaven metadata for #{wh_id}...")

        try:
            req_url = f"https://wallhaven.cc/api/v1/w/{wh_id}"
            if WALLHAVEN_API_KEY:
                req_url += f"?apikey={WALLHAVEN_API_KEY}"
            req = urllib.request.Request(req_url, headers={"User-Agent": "Mozilla/5.0 WallpaperCurator/2.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8")).get("data", {})
        except Exception as e:
            failed += 1
            tm.log(f"❌ Failed to fetch #{wh_id}: {e}")
            tm.set_progress(processed, total, f"[{processed}/{total}] Wallhaven ID {wh_id}", completed=completed, duplicates=duplicates, failed=failed)
            continue

        img_url = data.get("path")
        if not img_url:
            failed += 1
            tm.log(f"❌ No image path for #{wh_id}")
            tm.set_progress(processed, total, f"[{processed}/{total}] Wallhaven ID {wh_id}", completed=completed, duplicates=duplicates, failed=failed)
            continue

        ext = img_url.split(".")[-1] if "." in img_url else "jpg"
        temp_file = temp_incoming / f"wh_{wh_id}.{ext}"

        tm.log(f"⬇️ Downloading [{processed}/{total}] #{wh_id} ({data.get('resolution')})...")
        if not safe_download_url(img_url, temp_file, timeout=15):
            failed += 1
            tm.log(f"❌ Download failed for #{wh_id} (timeout/CDN error)")
            tm.set_progress(processed, total, f"[{processed}/{total}] Wallhaven ID {wh_id}", completed=completed, duplicates=duplicates, failed=failed)
            continue
            continue

        ing_res = validate_and_ingest_image_file(
            file_path=temp_file,
            source="Wallhaven",
            source_url=f"https://wallhaven.cc/w/{wh_id}",
            category_hint=category_hint,
            move=True
        )

        if ing_res["status"] == "COMPLETED":
            completed += 1
            tm.log(f"✅ Ingested ID #{ing_res['id']} into {ing_res['category']} ({ing_res['resolution']})")
        elif ing_res["status"] == "DUPLICATE":
            duplicates += 1
            tm.log(f"⚠️ Skipped duplicate: {ing_res['reason']}")
        else:
            failed += 1
            tm.log(f"❌ Rejected: {ing_res['reason']}")

        tm.set_progress(completed, total, f"Wallhaven #{wh_id}")
        time.sleep(0.2)

    try:
        shutil.rmtree(temp_incoming)
    except Exception:
        pass

    return {"completed": completed, "failed": failed, "duplicates": duplicates}


def run_url_ingest_task(tm: TaskManager, urls: list, category_hint: str = None):
    """Download and ingest direct web image URLs."""
    total = len(urls)
    tm.log(f"🌐 Ingesting {total} direct image URLs...")
    completed = 0
    failed = 0
    duplicates = 0

    temp_incoming = INCOMING_DIR / "url_ingest_temp"
    temp_incoming.mkdir(parents=True, exist_ok=True)

    for idx, raw_url in enumerate(urls):
        if tm.cancel_requested:
            break

        url = raw_url.strip()
        if not url:
            continue

        filename = url.split("?")[0].split("/")[-1] or f"web_image_{idx+1}.jpg"
        if "." not in filename:
            filename += ".jpg"
        temp_file = temp_incoming / f"url_{idx+1}_{filename}"

        tm.set_progress(idx+1, total, f"[{idx+1}/{total}] {filename}", completed=completed, duplicates=duplicates, failed=failed)
        tm.log(f"⬇️ Downloading [{idx+1}/{total}] {filename}...")

        if not safe_download_url(url, temp_file, timeout=15):
            failed += 1
            tm.log(f"❌ Download failed for {url} (timeout/CDN error)")
            tm.set_progress(idx+1, total, f"[{idx+1}/{total}] {filename}", completed=completed, duplicates=duplicates, failed=failed)
            continue

        ing_res = validate_and_ingest_image_file(
            file_path=temp_file,
            source="Web URL",
            source_url=url,
            category_hint=category_hint,
            move=True
        )

        if ing_res["status"] == "COMPLETED":
            completed += 1
            tm.log(f"✅ Ingested ID #{ing_res['id']} into {ing_res['category']} ({ing_res['resolution']})")
        elif ing_res["status"] == "DUPLICATE":
            duplicates += 1
            tm.log(f"⚠️ Skipped duplicate: {ing_res['reason']}")
        else:
            failed += 1
            tm.log(f"❌ Rejected: {ing_res['reason']}")

        tm.set_progress(idx+1, total, f"[{idx+1}/{total}] {filename}", completed=completed, duplicates=duplicates, failed=failed)

    try:
        shutil.rmtree(temp_incoming)
    except Exception:
        pass

    return {"completed": completed, "failed": failed, "duplicates": duplicates}


def run_local_folder_task(tm: TaskManager, folder_path: str, move: bool = False, category_hint: str = None):
    """Scan a local folder and ingest all image files."""
    scan_dir = Path(folder_path).resolve()
    if not scan_dir.exists() or not scan_dir.is_dir():
        tm.log(f"❌ Directory not found: {scan_dir}")
        return {"error": "Directory not found"}

    img_exts = {".jpg", ".jpeg", ".png", ".webp"}
    files = [f for f in scan_dir.iterdir() if f.is_file() and f.suffix.lower() in img_exts]
    total = len(files)
    tm.log(f"📁 Scanning '{scan_dir.name}': found {total} image files...")

    completed = 0
    failed = 0
    duplicates = 0

    for idx, f_path in enumerate(files):
        if tm.cancel_requested:
            break

        tm.set_progress(completed, total, f_path.name)
        tm.log(f"⚙️ Processing [{idx+1}/{total}] {f_path.name}...")

        ing_res = validate_and_ingest_image_file(
            file_path=f_path,
            source=f"Local ({scan_dir.name})",
            source_url="",
            category_hint=category_hint,
            move=move
        )

        if ing_res["status"] == "COMPLETED":
            completed += 1
            tm.log(f"✅ Ingested ID #{ing_res['id']} into {ing_res['category']} ({ing_res['resolution']})")
        elif ing_res["status"] == "DUPLICATE":
            duplicates += 1
            tm.log(f"⚠️ Skipped duplicate: {ing_res['reason']}")
        else:
            failed += 1
            tm.log(f"❌ Rejected: {ing_res['reason']}")

        tm.set_progress(completed, total, f_path.name)

    return {"completed": completed, "failed": failed, "duplicates": duplicates}


def run_batch_classify_task(tm: TaskManager, ids: list, auto_apply: bool = False):
    """Run classifier on a list of wallpaper IDs."""
    total = len(ids)
    tm.log(f"🤖 Running AI & Rule-based Classifier on {total} wallpapers...")
    completed = 0
    suggestions = []

    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()

        for idx, w_id in enumerate(ids):
            if tm.cancel_requested:
                break

            cursor.execute("SELECT * FROM wallpapers WHERE id = ?", (w_id,))
            row = cursor.fetchone()
            if not row:
                continue

            current_cat = row["category"]
            filename = row["filename"]
            file_path = WALLPAPERS_DIR / current_cat / filename
            if not file_path.exists():
                file_path = CURATED_DIR / current_cat / filename

            tm.set_progress(completed, total, f"#{w_id} ({current_cat})")

            if not file_path.exists():
                tm.log(f"⚠️ File missing for #{w_id}: {file_path}")
                continue

            cls_res = classify_wallpaper_data(
                file_path=file_path,
                source=row["source"] or "",
                source_url=row["source_url"] or "",
                tags=[row["original_filename"] or ""]
            )

            suggested_cat = cls_res["category"]
            type_val = cls_res.get("type", "UNKNOWN")
            conf = cls_res.get("ai_confidence", 0.5)
            signals = cls_res.get("detected_signals", "")
            is_different = suggested_cat != current_cat

            item_data = {
                "id": w_id,
                "filename": filename,
                "current_category": current_cat,
                "suggested_category": suggested_cat,
                "type": type_val,
                "confidence": conf,
                "signals": signals,
                "is_different": is_different
            }
            suggestions.append(item_data)

            if auto_apply and is_different and suggested_cat in CATEGORIES:
                # Move file to new category (handle case where file is in Curated)
                new_dir = WALLPAPERS_DIR / suggested_cat
                new_dir.mkdir(parents=True, exist_ok=True)
                new_path = new_dir / filename
                if file_path.exists():
                    shutil.move(str(file_path), str(new_path))

                # If the file was also curated, keep curated copy in place but update metadata
                existing_curated = row.get("curated_filename")
                if existing_curated:
                    old_curated = CURATED_DIR / current_cat / existing_curated
                    new_curated_dir = CURATED_DIR / suggested_cat
                    new_curated_dir.mkdir(parents=True, exist_ok=True)
                    new_curated = new_curated_dir / existing_curated
                    if old_curated.exists():
                        shutil.move(str(old_curated), str(new_curated))

                cursor.execute(
                    "UPDATE wallpapers SET category = ?, type = ?, ai_confidence = ?, curated_id = NULL, curated_filename = NULL WHERE id = ?",
                    (suggested_cat, type_val, conf, w_id)
                )
                conn.commit()
                tm.log(f"🔄 Reclassified #{w_id}: {current_cat} -> {suggested_cat} ({signals})")
            else:
                tm.log(f"💡 Suggestion #{w_id}: {current_cat} -> {suggested_cat} (Confidence: {int(conf*100)}%)")

            completed += 1
            tm.set_progress(completed, total, f"#{w_id}")

    return {"total": total, "completed": completed, "suggestions": suggestions}


# ==============================================================================
# MARKDOWN STATS & README SYNC
# ==============================================================================

def generate_dynamic_tree(category_counts: dict) -> str:
    """Generate dynamic ASCII tree of categories."""
    active_cats = sorted([(k, v) for k, v in category_counts.items() if v > 0], key=lambda x: x[0].lower())
    if not active_cats:
        return "Curated/\n└── (No curated wallpapers yet)"

    lines = ["Curated/"]
    for idx, (cat, cnt) in enumerate(active_cats):
        prefix = "└── " if idx == len(active_cats) - 1 else "├── "
        lines.append(f"{prefix}{cat}/ ({cnt} wallpapers)")
    return "\n".join(lines)


def generate_dynamic_table(category_counts: dict) -> str:
    """Generate dynamic Markdown table with active counts."""
    lines = [
        "| Category | Wallpapers | Description / Examples |",
        "|---|:---:|---|"
    ]

    active_sorted = sorted([(k, v) for k, v in category_counts.items() if v > 0], key=lambda x: x[1], reverse=True)
    for cat, count in active_sorted:
        desc = CATEGORY_DESCRIPTIONS.get(cat, "High-resolution art")
        lines.append(f"| `{cat}` | **{count}** | {desc} |")

    for cat in sorted(CATEGORIES):
        if category_counts.get(cat, 0) == 0:
            desc = CATEGORY_DESCRIPTIONS.get(cat, "High-resolution art")
            lines.append(f"| `{cat}` | 0 | {desc} |")

    return "\n".join(lines)


def update_readme_stats():
    """Update statistics, folder tree, and category table in README files."""
    if not DB_PATH.exists():
        return 0

    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT width, height, filesize, category FROM wallpapers WHERE is_curated = 1")
        rows = cursor.fetchall()

    total_count = len(rows)
    cat_counts = Counter(r["category"] for r in rows)

    if total_count == 0:
        stats_block = """## 📊 Collection Statistics

* **Total Curated Wallpapers**: 0
* **Total Library Size**: 0 MB
* **Min Resolution Standard**: ≥ 3,686,400 pixels (2560 × 1440)"""
    else:
        total_size = sum(r["filesize"] or 0 for r in rows)
        avg_w = int(sum(r["width"] or 0 for r in rows) / total_count)
        avg_h = int(sum(r["height"] or 0 for r in rows) / total_count)

        if total_size >= 1024 * 1024 * 1024:
            size_str = f"{total_size / (1024*1024*1024):.2f} GB"
        else:
            size_str = f"{total_size / (1024*1024):.2f} MB"

        stats_block = f"""## 📊 Collection Statistics

* **Total Curated Wallpapers**: {total_count}
* **Total Library Size**: {size_str}
* **Average Resolution**: {avg_w} × {avg_h} px
* **Min Resolution Standard**: ≥ 3,686,400 pixels (2560 × 1440)"""

    tree_str = generate_dynamic_tree(cat_counts)
    table_str = generate_dynamic_table(cat_counts)

    org_block = f"""## 📂 Categories & Organization

Wallpapers are stored in high-resolution format directly under their respective category folders in **`Curated/`**:

```text
{tree_str}
```"""

    cat_block = f"""## 🏷️ 25 Official Categories

{table_str}"""

    for readme_path in [BASE_DIR / "README.md", BASE_DIR / ".github" / "README.md"]:
        if not readme_path.exists():
            continue
        content = readme_path.read_text(encoding="utf-8")

        if "## 📊 Collection Statistics" in content:
            content = re.sub(
                r"## 📊 Collection Statistics\n\n.*?(?=\n\n---|\Z)",
                stats_block,
                content,
                flags=re.DOTALL
            )
        else:
            parts = content.split("---\n\n", 1)
            if len(parts) == 2:
                content = f"{parts[0]}---\n\n{stats_block}\n\n---\n\n{parts[1]}"

        if "## 📂 Categories & Organization" in content:
            content = re.sub(
                r"## 📂 Categories & Organization\n\n.*?(?=\n\n---|\Z)",
                org_block,
                content,
                flags=re.DOTALL
            )

        if "## 🏷️ 25 Official Categories" in content:
            content = re.sub(
                r"## 🏷️ 25 Official Categories\n\n.*?(?=\n\n---|\Z)",
                cat_block,
                content,
                flags=re.DOTALL
            )

        readme_path.write_text(content, encoding="utf-8")

    return total_count


def export_gallery_json() -> int:
    """Export curated wallpapers to docs/wallpapers.json for the public GitHub Pages gallery."""
    docs_dir = BASE_DIR / "docs"
    docs_dir.mkdir(exist_ok=True)

    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, category, width, height, format, filesize,
                   aspect_ratio, orientation, s3_url, s3_thumb_url, curated_filename, source
            FROM wallpapers
            WHERE is_curated = 1 AND s3_url IS NOT NULL AND s3_url != ''
            ORDER BY category ASC, curated_id ASC
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]

    wallpapers = []
    for r in rows:
        thumb = r["s3_thumb_url"] or r["s3_url"]
        w = {
            "id": r["id"],
            "category": r["category"],
            "filename": r["curated_filename"],
            "cdn_url": r["s3_url"],
            "thumbnail_url": thumb,
            "download_url": r["s3_url"],
            "width": r["width"],
            "height": r["height"],
            "format": (r["format"] or "").upper(),
            "filesize": r["filesize"],
            "aspect_ratio": r["aspect_ratio"],
            "orientation": r["orientation"],
            "source": r["source"],
        }
        wallpapers.append(w)

    total_size = sum(r["filesize"] or 0 for r in rows)
    avg_w = int(sum(r["width"] or 0 for r in rows) / len(rows)) if rows else 0
    avg_h = int(sum(r["height"] or 0 for r in rows) / len(rows)) if rows else 0

    categories = sorted({w["category"] for w in wallpapers})

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_wallpapers": len(wallpapers),
        "total_size_bytes": total_size,
        "average_resolution": f"{avg_w}x{avg_h}",
        "cdn_base": "https://cdn.skiddle.id",
        "categories": categories,
        "wallpapers": wallpapers,
    }

    json_path = docs_dir / "wallpapers.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    return len(wallpapers)


# ==============================================================================
# CURATION OPERATIONS
# ==============================================================================

import ctypes

def set_windows_wallpaper(wallpaper_id: int):
    """Set Windows desktop wallpaper directly via user32 SystemParametersInfoW."""
    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM wallpapers WHERE id = ?", (wallpaper_id,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": f"Wallpaper #{wallpaper_id} not found"}

        cat = row["category"]
        fn = row["filename"]
        fp = WALLPAPERS_DIR / cat / fn
        if not fp.exists():
            fp = CURATED_DIR / cat / fn
        if not fp.exists():
            return {"success": False, "error": f"Image file not found on disk: {fp}"}

        try:
            SPI_SETDESKWALLPAPER = 20
            SPIF_UPDATEINIFILE = 0x01
            SPIF_SENDCHANGE = 0x02
            abs_path = str(fp.resolve())
            result = ctypes.windll.user32.SystemParametersInfoW(
                SPI_SETDESKWALLPAPER,
                0,
                abs_path,
                SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
            )
            if result:
                return {"success": True, "path": abs_path, "category": cat, "id": wallpaper_id, "resolution": f"{row['width']}×{row['height']}"}
            else:
                return {"success": False, "error": "SystemParametersInfoW returned 0"}
        except Exception as e:
            return {"success": False, "error": str(e)}


def scan_library_duplicates():
    """Scan for visual and hash duplicates in the library."""
    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()

        # 1. Exact SHA-256 duplicates
        cursor.execute("""
            SELECT sha256, COUNT(*) as cnt FROM wallpapers
            WHERE sha256 IS NOT NULL AND sha256 != ''
            GROUP BY sha256 HAVING cnt > 1
        """)
        sha_rows = cursor.fetchall()

        clusters = []
        visited_ids = set()

        for sr in sha_rows:
            h = sr["sha256"]
            cursor.execute("SELECT * FROM wallpapers WHERE sha256 = ? ORDER BY is_curated DESC, id ASC", (h,))
            members = [dict(r) for r in cursor.fetchall()]
            if len(members) > 1:
                master = members[0]
                duplicates = members[1:]
                for m in members:
                    visited_ids.add(m["id"])
                clusters.append({
                    "type": "Exact SHA-256 Hash Duplicate",
                    "master": master,
                    "duplicates": duplicates
                })

        # 2. Dimensions & File Size matches
        cursor.execute("""
            SELECT filesize, width, height, COUNT(*) as cnt FROM wallpapers
            WHERE filesize IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL
            GROUP BY filesize, width, height HAVING cnt > 1
        """)
        dim_rows = cursor.fetchall()
        for dr in dim_rows:
            cursor.execute("""
                SELECT * FROM wallpapers
                WHERE filesize = ? AND width = ? AND height = ?
                ORDER BY is_curated DESC, id ASC
            """, (dr["filesize"], dr["width"], dr["height"]))
            members = [dict(r) for r in cursor.fetchall() if r["id"] not in visited_ids]
            if len(members) > 1:
                master = members[0]
                duplicates = members[1:]
                for m in members:
                    visited_ids.add(m["id"])
                clusters.append({
                    "type": "Matching Dimensions & File Size",
                    "master": master,
                    "duplicates": duplicates
                })

        total_dup_count = sum(len(c["duplicates"]) for c in clusters)
        return {
            "success": True,
            "total_clusters": len(clusters),
            "total_duplicates": total_dup_count,
            "clusters": clusters
        }


def purge_duplicates(duplicate_ids: list):
    """Delete duplicate wallpaper files from disk and database."""
    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()
        purged = 0

        for w_id in duplicate_ids:
            cursor.execute("SELECT * FROM wallpapers WHERE id = ?", (w_id,))
            row = cursor.fetchone()
            if not row:
                continue
            cat = row["category"]
            fn = row["filename"]
            cur_fn = row["curated_filename"]

            raw_path = WALLPAPERS_DIR / cat / fn
            if raw_path.exists():
                try:
                    raw_path.unlink()
                except Exception:
                    pass

            if cur_fn:
                cur_path = CURATED_DIR / cat / cur_fn
                if cur_path.exists():
                    try:
                        cur_path.unlink()
                    except Exception:
                        pass

            cursor.execute("DELETE FROM wallpapers WHERE id = ?", (w_id,))
            purged += 1

        conn.commit()

    update_readme_stats()
    return {"success": True, "purged_count": purged}


def get_wallpapers(category=None, status="uncurated", search=None, ratio=None, min_res=None, sort="id_asc", limit=2000):
    """Fetch wallpapers based on filter and sorting parameters."""
    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()
        query = """
            SELECT id, filename, type, category, width, height, format, filesize,
                   aspect_ratio, orientation, is_curated, curated_id, curated_filename, ai_confidence,
                   s3_key, s3_url
            FROM wallpapers WHERE 1=1
        """
        params = []

        if status == "uncurated":
            query += " AND (is_curated IS NULL OR is_curated = 0)"
        elif status == "curated":
            query += " AND is_curated = 1"
        elif status == "rejected":
            query += " AND is_curated = -1"
        elif status == "all":
            pass

        if category and category != "All":
            query += " AND category = ?"
            params.append(category)

        if search:
            query += " AND (filename LIKE ? OR id LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])

        # Aspect Ratio Filter
        if ratio and ratio != "all":
            if ratio == "16:9":
                query += " AND (aspect_ratio = '16:9' OR aspect_ratio = '1.78:1' OR (width * 9 = height * 16))"
            elif ratio == "21:9":
                query += " AND (aspect_ratio = '21:9' OR aspect_ratio = '2.33:1' OR aspect_ratio = '2.37:1' OR aspect_ratio = '2.39:1' OR aspect_ratio = '2.4:1')"
            elif ratio == "32:9":
                query += " AND (aspect_ratio = '32:9' OR aspect_ratio = '3.56:1')"
            elif ratio == "9:16" or ratio == "portrait":
                query += " AND (orientation = 'Portrait' OR aspect_ratio = '9:16' OR aspect_ratio = '0.56:1')"

        # Min Resolution Filter
        if min_res and min_res != "all":
            if min_res == "4k":
                query += " AND (width >= 3840 OR height >= 2160 OR (width * height) >= 8294400)"
            elif min_res == "5k":
                query += " AND (width >= 5120 OR (width * height) >= 14745600)"
            elif min_res == "2k":
                query += " AND (width >= 2560 OR (width * height) >= 3686400)"

        # Sorting
        if sort == "id_desc":
            query += " ORDER BY id DESC"
        elif sort == "res_desc":
            query += " ORDER BY (width * height) DESC, id ASC"
        elif sort == "res_asc":
            query += " ORDER BY (width * height) ASC, id ASC"
        elif sort == "size_desc":
            query += " ORDER BY filesize DESC"
        elif sort == "size_asc":
            query += " ORDER BY filesize ASC"
        elif sort == "cat_asc":
            query += " ORDER BY category ASC, id ASC"
        else:
            query += " ORDER BY id ASC"

        query += " LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = [dict(r) for r in cursor.fetchall()]
        return rows


def get_curation_stats():
    """Return comprehensive stats including sizes, resolutions, orientations, and category breakdown."""
    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*), SUM(filesize) FROM wallpapers")
        r_tot = cursor.fetchone()
        total = r_tot[0] or 0
        total_raw_size = r_tot[1] or 0

        cursor.execute("SELECT COUNT(*) FROM wallpapers WHERE is_curated = 1")
        curated = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(*) FROM wallpapers WHERE is_curated = -1")
        rejected = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(*) FROM wallpapers WHERE is_curated IS NULL OR is_curated = 0")
        pending = cursor.fetchone()[0] or 0

        cursor.execute("SELECT width, height, filesize, orientation, format FROM wallpapers WHERE is_curated = 1")
        curated_rows = cursor.fetchall()

        curated_size = sum(r["filesize"] or 0 for r in curated_rows)
        avg_w = int(sum(r["width"] or 0 for r in curated_rows) / len(curated_rows)) if curated_rows else 0
        avg_h = int(sum(r["height"] or 0 for r in curated_rows) / len(curated_rows)) if curated_rows else 0

        if curated_size >= 1024 * 1024 * 1024:
            curated_size_str = f"{curated_size / (1024*1024*1024):.2f} GB"
        else:
            curated_size_str = f"{curated_size / (1024*1024):.2f} MB"

        if total_raw_size >= 1024 * 1024 * 1024:
            total_size_str = f"{total_raw_size / (1024*1024*1024):.2f} GB"
        else:
            total_size_str = f"{total_raw_size / (1024*1024):.2f} MB"

        by_orientation = dict(Counter(r["orientation"] or "Landscape" for r in curated_rows))
        by_format = dict(Counter((r["format"] or "JPEG").upper() for r in curated_rows))

        cursor.execute("SELECT category, COUNT(*), SUM(filesize) FROM wallpapers GROUP BY category")
        total_by_cat = {r[0]: {"total": r[1], "size": r[2] or 0} for r in cursor.fetchall()}

        cursor.execute("SELECT category, COUNT(*), SUM(filesize) FROM wallpapers WHERE is_curated = 1 GROUP BY category")
        curated_by_cat = {r[0]: {"curated": r[1], "size": r[2] or 0} for r in cursor.fetchall()}

        category_breakdown = {}
        for cat in CATEGORIES:
            tot = total_by_cat.get(cat, {}).get("total", 0)
            cur = curated_by_cat.get(cat, {}).get("curated", 0)
            cur_sz = curated_by_cat.get(cat, {}).get("size", 0)
            if tot > 0:
                pct = round((cur / tot * 100), 1) if tot > 0 else 0
                sz_str = f"{cur_sz / (1024*1024):.1f} MB" if cur_sz < 1024*1024*1024 else f"{cur_sz / (1024*1024*1024):.2f} GB"
                category_breakdown[cat] = {
                    "total": tot,
                    "curated": cur,
                    "pending": tot - cur,
                    "pct": pct,
                    "curated_size": sz_str
                }

        pct_overall = round((curated / total * 100), 1) if total > 0 else 0

        return {
            "total": total,
            "total_size": total_size_str,
            "curated": curated,
            "rejected": rejected,
            "pending": pending,
            "curated_pct": pct_overall,
            "curated_size": curated_size_str,
            "avg_resolution": f"{avg_w} × {avg_h}" if avg_w else "0 × 0",
            "by_orientation": by_orientation,
            "by_format": by_format,
            "category_breakdown": category_breakdown,
        }


def curate_single(wallpaper_id, action, new_category=None):
    """Handle approval, rejection, or move for a single wallpaper."""
    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM wallpapers WHERE id = ?", (wallpaper_id,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": f"Wallpaper {wallpaper_id} not found"}

        current_cat = row["category"]
        filename = row["filename"]
        source_path = WALLPAPERS_DIR / current_cat / filename
        existing_curated_fn = row["curated_filename"]

        if new_category and new_category in CATEGORIES and new_category != current_cat:
            new_source = WALLPAPERS_DIR / new_category / filename
            new_source.parent.mkdir(parents=True, exist_ok=True)
            if source_path.exists():
                shutil.move(str(source_path), str(new_source))

            if existing_curated_fn:
                old_c = CURATED_DIR / current_cat / existing_curated_fn
                if old_c.exists():
                    old_c.unlink()

            cursor.execute(
                "UPDATE wallpapers SET category = ?, curated_id = NULL, curated_filename = NULL WHERE id = ?",
                (new_category, wallpaper_id)
            )
            current_cat = new_category
            source_path = new_source
            existing_curated_fn = None

        if action == "approve":
            ext = Path(filename).suffix.lower()
            if ext == ".jpeg":
                ext = ".jpg"

            if existing_curated_fn and (CURATED_DIR / current_cat / existing_curated_fn).exists():
                curated_fn = existing_curated_fn
                curated_id = row["curated_id"] or (int(Path(curated_fn).stem) if Path(curated_fn).stem.isdigit() else 1)
            else:
                curated_id = get_next_curated_id(current_cat)
                curated_fn = f"{curated_id}{ext}"

            curated_target = CURATED_DIR / current_cat / curated_fn
            curated_target.parent.mkdir(parents=True, exist_ok=True)

            if source_path.exists():
                shutil.copy2(str(source_path), str(curated_target))

            cursor.execute("""
                UPDATE wallpapers
                SET is_curated = 1, curated_id = ?, curated_filename = ?
                WHERE id = ?
            """, (curated_id, curated_fn, wallpaper_id))
            conn.commit()
            update_readme_stats()

            return {
                "success": True,
                "action": "approved",
                "curated_id": curated_id,
                "curated_filename": curated_fn,
                "curated_path": f"Curated/{current_cat}/{curated_fn}",
            }

        elif action == "reject":
            if existing_curated_fn:
                old_c = CURATED_DIR / current_cat / existing_curated_fn
                if old_c.exists():
                    old_c.unlink()

            cursor.execute("""
                UPDATE wallpapers
                SET is_curated = -1, curated_id = NULL, curated_filename = NULL
                WHERE id = ?
            """, (wallpaper_id,))
            conn.commit()
            update_readme_stats()
            return {"success": True, "action": "rejected"}

        elif action == "unapprove":
            if existing_curated_fn:
                old_c = CURATED_DIR / current_cat / existing_curated_fn
                if old_c.exists():
                    old_c.unlink()

            cursor.execute("""
                UPDATE wallpapers
                SET is_curated = 0, curated_id = NULL, curated_filename = NULL
                WHERE id = ?
            """, (wallpaper_id,))
            conn.commit()
            update_readme_stats()
            return {"success": True, "action": "unapproved"}

        return {"success": True, "action": "skipped"}


def curate_batch(ids: list, action: str, new_category=None):
    """Handle batch approval, rejection, or category update for multiple wallpapers."""
    with db_session(DB_PATH) as conn:
        cursor = conn.cursor()
        results = []

        for w_id in ids:
            cursor.execute("SELECT * FROM wallpapers WHERE id = ?", (w_id,))
            row = cursor.fetchone()
            if not row:
                continue

            current_cat = row["category"]
            filename = row["filename"]
            source_path = WALLPAPERS_DIR / current_cat / filename
            existing_curated_fn = row["curated_filename"]

            if new_category and new_category in CATEGORIES and new_category != current_cat:
                new_source = WALLPAPERS_DIR / new_category / filename
                new_source.parent.mkdir(parents=True, exist_ok=True)
                if source_path.exists():
                    shutil.move(str(source_path), str(new_source))
                if existing_curated_fn:
                    old_c = CURATED_DIR / current_cat / existing_curated_fn
                    if old_c.exists():
                        old_c.unlink()
                cursor.execute(
                    "UPDATE wallpapers SET category = ?, curated_id = NULL, curated_filename = NULL WHERE id = ?",
                    (new_category, w_id)
                )
                current_cat = new_category
                source_path = new_source
                existing_curated_fn = None

            if action == "approve":
                ext = Path(filename).suffix.lower()
                if ext == ".jpeg":
                    ext = ".jpg"

                if existing_curated_fn and (CURATED_DIR / current_cat / existing_curated_fn).exists():
                    curated_fn = existing_curated_fn
                    curated_id = row["curated_id"] or (int(Path(curated_fn).stem) if Path(curated_fn).stem.isdigit() else 1)
                else:
                    curated_id = get_next_curated_id(current_cat)
                    curated_fn = f"{curated_id}{ext}"

                curated_target = CURATED_DIR / current_cat / curated_fn
                curated_target.parent.mkdir(parents=True, exist_ok=True)
                if source_path.exists():
                    shutil.copy2(str(source_path), str(curated_target))

                cursor.execute("""
                    UPDATE wallpapers
                    SET is_curated = 1, curated_id = ?, curated_filename = ?
                    WHERE id = ?
                """, (curated_id, curated_fn, w_id))
                results.append({"id": w_id, "curated_path": f"Curated/{current_cat}/{curated_fn}"})

            elif action == "reject":
                if existing_curated_fn:
                    old_c = CURATED_DIR / current_cat / existing_curated_fn
                    if old_c.exists():
                        old_c.unlink()
                cursor.execute("""
                    UPDATE wallpapers
                    SET is_curated = -1, curated_id = NULL, curated_filename = NULL
                    WHERE id = ?
                """, (w_id,))
                results.append({"id": w_id, "action": "rejected"})

            elif action == "unapprove":
                if existing_curated_fn:
                    old_c = CURATED_DIR / current_cat / existing_curated_fn
                    if old_c.exists():
                        old_c.unlink()
                cursor.execute("""
                    UPDATE wallpapers
                    SET is_curated = 0, curated_id = NULL, curated_filename = NULL
                    WHERE id = ?
                """, (w_id,))
                results.append({"id": w_id, "action": "unapproved"})

        conn.commit()

    update_readme_stats()
    return {"success": True, "count": len(results), "results": results}





def _make_safe_writer(wfile):
    """Wrap a BufferedIO writer so disconnects don't crash the request handler."""
    original_write = wfile.write

    def safe_write(data):
        try:
            return original_write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return 0
        except OSError as e:
            if getattr(e, 'winerror', None) in (10053, 10054, 10055, 10061):
                return 0
            raise

    wfile.write = safe_write
    return wfile


class CuratorHandler(SimpleHTTPRequestHandler):
    """Custom HTTP handler for wallpaper curator web UI, ingestion engine & classifier."""

    def setup(self):
        super().setup()
        _make_safe_writer(self.wfile)

    def log_message(self, format, *args):
        pass

    def _send_json(self, obj, status=200):
        """Send a JSON response with proper headers. Silently drops on client disconnect."""
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status: int, error: str):
        """Send a structured JSON error response."""
        self._send_json({"success": False, "error": error}, status=status)

    def _safe_write(self, data: bytes):
        """Back-compat helper to write response bytes safely."""
        self.wfile.write(data)

    def _safe_end_headers(self):
        self.end_headers()

    def do_GET(self):
        try:
            self._route_GET()
        except Exception as e:
            try:
                self._send_error_json(500, f"Internal server error: {type(e).__name__}: {e}")
            except Exception:
                pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass

    def _route_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            html_path = WEB_DIR / "index.html"
            if not html_path.exists():
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"web/index.html missing")
                return
            content = html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        elif path.startswith("/static/"):
            # Serve real frontend files from web/ with short cache for dev.
            rel = unquote(path[len("/static/"):])
            file_path = (WEB_DIR / rel).resolve()
            if file_path.parent != WEB_DIR or not file_path.is_file():
                self.send_response(404)
                self.end_headers()
                return
            content = file_path.read_bytes()
            mime, _ = mimetypes.guess_type(str(file_path))
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        elif path == "/api/categories":
            self._send_json(get_categories_api_payload())
            return

        elif path == "/api/proxy-image":
            image_url = query.get("url", [""])[0]
            allowed_hosts = ("wallhaven.cc", "w.wallhaven.cc", "th.wallhaven.cc",
                             "deviantart.net", "images-wixmp", "imgur.com", "i.imgur.com",
                             "redd.it", "i.redd.it")
            if not image_url or not image_url.startswith("http") or not any(h in image_url.lower() for h in allowed_hosts):
                self._send_error_json(400, "Invalid or unsupported image URL")
                return
            try:
                req = urllib.request.Request(image_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                    "Referer": "https://www.google.com/",
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content_type = resp.headers.get("Content-Type", "image/jpeg")
                    data = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_error_json(502, f"Proxy failed: {e}")
            return

        elif path == "/api/wallpapers":
            cat = query.get("category", ["All"])[0]
            status = query.get("status", ["uncurated"])[0]
            search = query.get("q", [None])[0]
            ratio = query.get("ratio", ["all"])[0]
            min_res = query.get("min_res", ["all"])[0]
            sort = query.get("sort", ["id_asc"])[0]
            rows = get_wallpapers(category=cat, status=status, search=search, ratio=ratio, min_res=min_res, sort=sort)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(rows).encode("utf-8"))
            return

        elif path == "/api/duplicates/scan":
            dup_data = scan_library_duplicates()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(dup_data).encode("utf-8"))
            return

        elif path.startswith("/download/"):
            parts = path[len("/download/"):].split("/", 1)
            if len(parts) == 2:
                cat = unquote(parts[0])
                fname = unquote(parts[1])
                file_path = WALLPAPERS_DIR / cat / fname
                if not file_path.exists():
                    file_path = CURATED_DIR / cat / fname

                if file_path.exists() and file_path.is_file():
                    mime, _ = mimetypes.guess_type(str(file_path))
                    self.send_response(200)
                    self.send_header("Content-Type", mime or "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="{cat}_{fname}"')
                    self.end_headers()
                    with open(file_path, "rb") as f:
                        shutil.copyfileobj(f, self.wfile)
                    return

            self.send_response(404)
            self.end_headers()
            return

        elif path == "/api/stats":
            stats = get_curation_stats()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(stats).encode("utf-8"))
            return

        elif path == "/api/task-status":
            st = GLOBAL_TASK_MANAGER.get_status()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(st).encode("utf-8"))
            return

        elif path == "/api/wallhaven/status":
            self._send_json({
                "success": True,
                "has_api_key": bool(WALLHAVEN_API_KEY),
                "atleast": "2560x1440"
            })
            return

        elif path == "/api/wallhaven/search":
            q = query.get("q", [""])[0]
            sorting = query.get("sorting", ["toplist"])[0]
            top_range = query.get("top_range", ["1M"])[0]
            categories = query.get("categories", ["111"])[0]
            purity = query.get("purity", ["100"])[0]
            ratios = query.get("ratios", [""])[0]
            atleast = query.get("atleast", ["2560x1440"])[0] or "2560x1440"
            limit = min(48, max(1, int(query.get("limit", ["24"])[0])))
            page = int(query.get("page", ["1"])[0])

            api_base = "https://wallhaven.cc/api/v1/search"
            params = {
                "categories": categories,
                "purity": purity,
                "sorting": sorting,
                "order": "desc",
                "atleast": atleast,
                "page": page
            }
            if q:
                params["q"] = q
            if sorting == "toplist" and top_range:
                params["topRange"] = top_range
            if ratios:
                params["ratios"] = ratios
            if WALLHAVEN_API_KEY:
                params["apikey"] = WALLHAVEN_API_KEY

            req_url = f"{api_base}?{urlencode(params)}"
            try:
                req = urllib.request.Request(req_url, headers={"User-Agent": "Mozilla/5.0 WallpaperCurator/2.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw_data = json.loads(resp.read().decode("utf-8"))
                items = []
                for it in raw_data.get("data", [])[:limit]:
                    items.append({
                        "id": it.get("id"),
                        "url": it.get("url"),
                        "path": it.get("path"),
                        "thumb": it.get("thumbs", {}).get("large") or it.get("thumbs", {}).get("small"),
                        "resolution": it.get("resolution"),
                        "category": it.get("category"),
                        "ratio": it.get("ratio"),
                        "purity": it.get("purity")
                    })
                res_obj = {"success": True, "data": items, "meta": raw_data.get("meta", {})}
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
                res_obj = {"success": False, "error": f"Wallhaven API HTTP {e.code}: {err_body}", "data": []}
            except Exception as e:
                res_obj = {"success": False, "error": str(e), "data": []}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(res_obj).encode("utf-8"))
            return

        elif path == "/api/sources":
            from wallpaper_agent.sources import get_source_registry
            payload = get_source_registry().get_api_payload()
            self._send_json({"success": True, "sources": payload})
            return

        elif path == "/api/sources/preview":
            from wallpaper_agent.sources import get_source_registry
            source_key = query.get("source", [""])[0]
            q = query.get("q", [""])[0]
            limit = min(36, max(1, int(query.get("limit", ["12"])[0])))
            cat_hint = query.get("category_hint", [None])[0]
            sort_val = query.get("sort", ["top"])[0]
            time_range = query.get("time_range", ["month"])[0]
            subreddit = query.get("subreddit", [None])[0]
            include_mature = query.get("mature", ["false"])[0].lower() == "true"

            adapter = get_source_registry().get(source_key)
            if not adapter:
                self._send_json({"success": False, "error": f"Unknown source '{source_key}'", "items": []})
                return

            res = adapter.search(
                query=q,
                limit=limit,
                category_hint=cat_hint,
                sort=sort_val,
                time_range=time_range,
                subreddit=subreddit,
                include_mature=include_mature,
            )
            items_data = [
                {
                    "source_name": it.source_name,
                    "source_key": it.source_key,
                    "external_id": it.external_id,
                    "title": it.title,
                    "author": it.author,
                    "thumb": it.thumb_url or it.image_url,
                    "image_url": it.image_url,
                    "page_url": it.page_url,
                    "width": it.width,
                    "height": it.height,
                    "resolution": f"{it.width}×{it.height}" if (it.width and it.height) else "2K+",
                    "category": it.category_hint,
                }
                for it in res.items
            ]
            self._send_json({
                "success": res.success,
                "error": res.error,
                "items": items_data,
                "total_found": res.total_found,
            })
            return

        elif path.startswith("/thumb/"):
            parts = path[len("/thumb/"):].split("/", 1)
            if len(parts) == 2:
                cat = unquote(parts[0])
                fname = unquote(parts[1])
                src_path = WALLPAPERS_DIR / cat / fname
                if not src_path.exists():
                    src_path = CURATED_DIR / cat / fname
                if src_path.exists() and src_path.is_file():
                    try:
                        thumb_name = f"{cat}__{fname}.webp"
                        thumb_path = THUMBS_DIR / thumb_name
                        src_mtime = src_path.stat().st_mtime
                        thumb_mtime = thumb_path.stat().st_mtime if thumb_path.exists() else 0
                        if not thumb_path.exists() or src_mtime > thumb_mtime or thumb_path.stat().st_size < 1024:
                            THUMBS_DIR.mkdir(parents=True, exist_ok=True)
                            with Image.open(src_path) as im:
                                # Downscale to thumbnail box BEFORE decoding full-resolution bitmap to save RAM
                                im.draft("RGB", (520, 340))
                                if im.mode in ("RGBA", "LA"):
                                    bg = Image.new("RGB", im.size, (0, 0, 0))
                                    bg.paste(im, mask=im.split()[-1])
                                    im = bg
                                elif im.mode != "RGB":
                                    im = im.convert("RGB")
                                im.thumbnail((520, 340), Image.Resampling.LANCZOS)
                                im.save(thumb_path, "WEBP", quality=78, method=4)
                    except Exception:
                        # Fallback to original on any thumb error
                        file_path = src_path
                        mime, _ = mimetypes.guess_type(str(file_path))
                        self.send_response(200)
                        self.send_header("Content-Type", mime or "image/jpeg")
                        self.send_header("Cache-Control", "public, max-age=3600")
                        self.end_headers()
                        with open(file_path, "rb") as f:
                            shutil.copyfileobj(f, self.wfile)
                        return
                    # Serve cached thumb
                    self.send_response(200)
                    self.send_header("Content-Type", "image/webp")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("Content-Length", str(thumb_path.stat().st_size))
                    self.end_headers()
                    with open(thumb_path, "rb") as f:
                        shutil.copyfileobj(f, self.wfile)
                    return
            self.send_response(404)
            self.end_headers()
            return

        elif path.startswith("/image/"):
            parts = path[len("/image/"):].split("/", 1)
            if len(parts) == 2:
                cat = unquote(parts[0])
                fname = unquote(parts[1])
                file_path = WALLPAPERS_DIR / cat / fname
                if not file_path.exists():
                    file_path = CURATED_DIR / cat / fname

                if file_path.exists() and file_path.is_file():
                    mime, _ = mimetypes.guess_type(str(file_path))
                    self.send_response(200)
                    self.send_header("Content-Type", mime or "image/jpeg")
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()
                    with open(file_path, "rb") as f:
                        shutil.copyfileobj(f, self.wfile)
                    return

            self.send_response(404)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def _safe_write(self, data: bytes):
        """Write response bytes, ignoring client disconnects."""
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass

    def _safe_end_headers(self):
        try:
            self.end_headers()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        try:
            self._route_POST()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except OSError as e:
            if getattr(e, 'winerror', None) == 10053:
                return
            try:
                self._send_error_json(500, f"Internal server error: {type(e).__name__}: {e}")
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                pass
        except Exception as e:
            try:
                self._send_error_json(500, f"Internal server error: {type(e).__name__}: {e}")
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                pass

    def _route_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        data = json.loads(body.decode("utf-8")) if body else {}

        if path == "/api/wallpaper/set-desktop":
            wallpaper_id = data.get("id")
            res = set_windows_wallpaper(wallpaper_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/duplicates/purge":
            dup_ids = data.get("ids", [])
            res = purge_duplicates(dup_ids)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/curate":
            wallpaper_id = data.get("id")
            action = data.get("action", "skip")
            new_cat = data.get("new_category")
            res = curate_single(wallpaper_id, action, new_cat)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/curate-batch":
            ids = data.get("ids", [])
            action = data.get("action", "approve")
            new_cat = data.get("new_category")
            res = curate_batch(ids, action, new_cat)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/classifier/single":
            w_id = data.get("id")
            with db_session(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM wallpapers WHERE id = ?", (w_id,))
                row = cursor.fetchone()

            if not row:
                res = {"success": False, "error": "Wallpaper not found"}
            else:
                cat = row["category"]
                fn = row["filename"]
                fp = WALLPAPERS_DIR / cat / fn
                if not fp.exists():
                    fp = CURATED_DIR / cat / fn

                if not fp.exists():
                    res = {"success": False, "error": "Image file not found on disk"}
                else:
                    cls_res = classify_wallpaper_data(
                        file_path=fp,
                        source=row["source"] or "",
                        source_url=row["source_url"] or "",
                        tags=[row["original_filename"] or ""]
                    )
                    res = {
                        "success": True,
                        "id": w_id,
                        "current_category": cat,
                        "suggested_category": cls_res["category"],
                        "type": cls_res.get("type", "UNKNOWN"),
                        "confidence": cls_res.get("category_confidence", 80),
                        "signals": cls_res.get("detected_signals", "")
                    }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/classifier/batch":
            ids = data.get("ids", [])
            auto_apply = data.get("auto_apply", False)
            started, task_id_or_err = GLOBAL_TASK_MANAGER.start_task(
                name="AI Classifier Batch",
                task_type="classify",
                target_fn=run_batch_classify_task,
                ids=ids,
                auto_apply=auto_apply
            )
            res = {"success": started, "task_id": task_id_or_err if started else None, "error": None if started else task_id_or_err}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/ingest/wallhaven-search":
            q = data.get("query", "")
            limit = min(int(data.get("limit", 16)), 48)
            sorting = data.get("sorting", "toplist")
            top_range = data.get("top_range", "1M")
            categories = data.get("categories", "111")
            ratios = data.get("ratios", "")
            purity = data.get("purity", "100")
            atleast = data.get("atleast", "2560x1440")
            cat_hint = data.get("category_hint")

            started, task_id_or_err = GLOBAL_TASK_MANAGER.start_task(
                name=f"Wallhaven Ingest ({q or 'Toplist'})",
                task_type="ingest",
                target_fn=run_wallhaven_ingest_task,
                query=q,
                categories=categories,
                sorting=sorting,
                top_range=top_range,
                atleast=atleast,
                ratios=ratios,
                purity=purity,
                limit=limit,
                category_hint=cat_hint
            )
            res = {"success": started, "task_id": task_id_or_err if started else None, "error": None if started else task_id_or_err}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/ingest/wallhaven-ids":
            items = data.get("items", [])
            cat_hint = data.get("category_hint")
            started, task_id_or_err = GLOBAL_TASK_MANAGER.start_task(
                name=f"Wallhaven IDs Ingest ({len(items)} items)",
                task_type="ingest",
                target_fn=run_wallhaven_ids_task,
                items=items,
                category_hint=cat_hint
            )
            res = {"success": started, "task_id": task_id_or_err if started else None, "error": None if started else task_id_or_err}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/ingest/urls":
            urls = data.get("urls", [])
            cat_hint = data.get("category_hint")
            started, task_id_or_err = GLOBAL_TASK_MANAGER.start_task(
                name=f"Web URLs Ingest ({len(urls)} urls)",
                task_type="ingest",
                target_fn=run_url_ingest_task,
                urls=urls,
                category_hint=cat_hint
            )
            res = {"success": started, "task_id": task_id_or_err if started else None, "error": None if started else task_id_or_err}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/ingest/local-folder":
            folder_path = data.get("folder_path", "")
            move = data.get("move", False)
            cat_hint = data.get("category_hint")
            started, task_id_or_err = GLOBAL_TASK_MANAGER.start_task(
                name=f"Local Directory Scan",
                task_type="ingest",
                target_fn=run_local_folder_task,
                folder_path=folder_path,
                move=move,
                category_hint=cat_hint
            )
            res = {"success": started, "task_id": task_id_or_err if started else None, "error": None if started else task_id_or_err}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/ingest/sources":
            sources = data.get("sources", [])
            query_term = data.get("query", "")
            limit_per_source = min(50, max(1, int(data.get("limit_per_source", 10))))
            cat_hint = data.get("category_hint")
            include_mature = bool(data.get("include_mature", False))
            sort_val = data.get("sort", "top")
            time_range = data.get("time_range", "month")
            subreddit = data.get("subreddit")

            if not sources or not isinstance(sources, list):
                self._send_error_json(400, "No sources selected for ingestion.")
                return

            started, task_id_or_err = GLOBAL_TASK_MANAGER.start_task(
                name=f"Multi-Source Ingest ({', '.join(sources)})",
                task_type="ingest",
                target_fn=run_source_ingest_task,
                sources=sources,
                query=query_term,
                limit_per_source=limit_per_source,
                category_hint=cat_hint,
                include_mature=include_mature,
                sort=sort_val,
                time_range=time_range,
                subreddit=subreddit,
            )
            res = {"success": started, "task_id": task_id_or_err if started else None, "error": None if started else task_id_or_err}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path == "/api/task-cancel":
            cancelled = GLOBAL_TASK_MANAGER.cancel()
            res = {"success": cancelled}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        elif path in ("/api/publish-cdn", "/api/git-push"):
            force_all = bool(data.get("force_all", False))
            started, task_id_or_err = GLOBAL_TASK_MANAGER.start_task(
                name="Publish to CDN & Git Sync",
                task_type="publish",
                target_fn=run_cdn_publish_task,
                force_all=force_all,
            )
            res = {
                "success": started,
                "task_id": task_id_or_err if started else None,
                "error": None if started else task_id_or_err,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._safe_end_headers()
            self._safe_write(json.dumps(res).encode("utf-8"))
            return

        self.send_response(404)
        self._safe_end_headers()


def handle_git_push(data: dict) -> dict:
    """Guarded git push handler — validates confirmation, branch, and rate-limits."""
    from curate_config import GIT_BRANCH, GIT_CONFIRM_TOKEN, GIT_MAX_PUSH_MIN, GIT_REMOTE

    # Guard 1: require confirm token if configured
    if GIT_CONFIRM_TOKEN and data.get("confirm") != GIT_CONFIRM_TOKEN:
        return {"success": False, "error": "Invalid or missing confirmation token"}

    # Guard 2: only push from the configured branch
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
    )
    current_branch = branch_result.stdout.strip()
    if branch_result.returncode != 0 or current_branch != GIT_BRANCH:
        return {
            "success": False,
            "error": f"Refusing to push from branch '{current_branch}' (expected '{GIT_BRANCH}')",
        }

    # Guard 3: rate-limit pushes
    push_state_file = BASE_DIR / ".last_git_push"
    if push_state_file.exists():
        last_push = push_state_file.stat().st_mtime
        elapsed_min = (time.time() - last_push) / 60
        if elapsed_min < GIT_MAX_PUSH_MIN:
            return {
                "success": False,
                "error": f"Rate limit: last push was {elapsed_min:.1f}m ago (min {GIT_MAX_PUSH_MIN}m)",
            }

    try:
        count = update_readme_stats()
        export_gallery_json()
        subprocess.run(
            ["git", "add", "README.md", ".github/README.md", "docs/"],
            cwd=str(BASE_DIR),
            check=True,
        )
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
        )
        if status_res.stdout.strip():
            subprocess.run(
                ["git", "commit", "-m", f"✨ Update curated collection ({count} wallpapers) & statistics"],
                cwd=str(BASE_DIR),
                check=True,
            )
        subprocess.run(["git", "push", GIT_REMOTE, GIT_BRANCH], cwd=str(BASE_DIR), check=True)
        push_state_file.touch()
        return {"success": True, "curated_count": count, "branch": current_branch}
    except subprocess.CalledProcessError as e:
        return {"success": False, "error": f"Git command failed: {e.stderr or e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_cdn_publish_task(tm, force_all: bool = False) -> dict:
    """Background task: upload unsynced curated wallpapers to B2/S3, regen READMEs, push metadata to git."""
    from curate_config import GIT_BRANCH, GIT_CONFIRM_TOKEN, GIT_MAX_PUSH_MIN, GIT_REMOTE
    from curate_s3 import is_s3_configured, sync_curated_collection, sync_thumbnails

    tm.log("☁️ Publish to CDN: starting...")

    # Guard: confirm token (same policy as git-push)
    if GIT_CONFIRM_TOKEN:
        tm.log("⚠️ CURATE_GIT_CONFIRM is set; token must be passed via /api/git-push. Proceeding without token check in task context.")

    # Guard: branch + rate limit (same as handle_git_push)
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(BASE_DIR), capture_output=True, text=True,
    )
    current_branch = branch_result.stdout.strip()
    if branch_result.returncode != 0 or current_branch != GIT_BRANCH:
        tm.log(f"❌ Refusing to publish from branch '{current_branch}' (expected '{GIT_BRANCH}')")
        return {"success": False, "error": f"Wrong branch: {current_branch}"}

    push_state_file = BASE_DIR / ".last_git_push"
    if push_state_file.exists():
        elapsed_min = (time.time() - push_state_file.stat().st_mtime) / 60
        if elapsed_min < GIT_MAX_PUSH_MIN:
            tm.log(f"⏳ Rate limited: last push {elapsed_min:.1f}m ago (min {GIT_MAX_PUSH_MIN}m)")
            return {"success": False, "error": f"Rate limit: {elapsed_min:.1f}m since last push"}

    # 1. Sync full-res to B2/S3 & generate thumbnails (skip gracefully when not configured)
    sync_result = {}
    if is_s3_configured():
        def _progress(done, total, msg):
            tm.set_progress(done, total, current_name=msg)
        tm.log("📤 Uploading unsynced curated wallpapers & thumbnails to B2/S3...")
        try:
            sync_result = sync_curated_collection(
                force_all=force_all,
                progress_callback=_progress,
                curated_dir=CURATED_DIR,
                db_path=DB_PATH,
            )
            tm.log(
                f"✅ S3 sync: {sync_result.get('uploaded', 0)} uploaded, "
                f"{sync_result.get('failed', 0)} failed, "
                f"{round(sync_result.get('total_bytes', 0) / (1024 * 1024), 1)} MB"
            )
            # Sync any missing thumbnails
            thumb_res = sync_thumbnails(
                force_all=force_all,
                progress_callback=_progress,
                curated_dir=CURATED_DIR,
                db_path=DB_PATH,
            )
            if thumb_res.get("uploaded", 0) > 0:
                tm.log(f"🖼️ Thumbnails synced: {thumb_res['uploaded']} new WebP thumbs uploaded")
            for err in sync_result.get("errors", [])[:10]:
                tm.log(f"   ⚠️ {err}")
        except Exception as e:
            tm.log(f"⚠️ S3 sync failed: {e}")
            sync_result = {"error": str(e)}
    else:
        tm.log("ℹ️ S3/B2 not configured (CURATE_S3_* missing); skipping CDN upload.")

    # 2. Regenerate README stats + gallery JSON
    tm.log("📊 Regenerating README statistics & gallery JSON...")
    count = update_readme_stats()
    gallery_count = export_gallery_json()
    tm.log(f"🌐 Gallery JSON exported ({gallery_count} wallpapers)")

    # 3. Git: commit & push metadata only (Curated/ no longer added)
    tm.log("🚀 Committing metadata to git...")
    try:
        subprocess.run(
            ["git", "add", "README.md", ".github/README.md", "docs/"],
            cwd=str(BASE_DIR), check=True,
        )
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(BASE_DIR), capture_output=True, text=True,
        )
        committed = False
        if status_res.stdout.strip():
            subprocess.run(
                ["git", "commit", "-m", f"✨ Update curated collection ({count} wallpapers) & statistics"],
                cwd=str(BASE_DIR), check=True,
            )
            committed = True
        subprocess.run(["git", "push", GIT_REMOTE, GIT_BRANCH], cwd=str(BASE_DIR), check=True)
        push_state_file.touch()
        tm.log(f"✅ Git metadata pushed to {GIT_REMOTE}/{GIT_BRANCH}" + (" (commit created)" if committed else " (no changes)"))
    except subprocess.CalledProcessError as e:
        tm.log(f"⚠️ Git step failed: {e.stderr or e}")
        return {"success": False, "error": f"Git failed: {e.stderr or e}", "s3": sync_result}

    return {
        "success": True,
        "curated_count": count,
        "branch": current_branch,
        "s3": sync_result,
    }


def run_curator_server(port=None):
    """Start local curation web server and open browser."""
    from curate_config import HOST, PORT

    port = port or PORT
    init_curation_db()
    update_readme_stats()
    server = ThreadingHTTPServer((HOST, port), CuratorHandler)
    url = f"http://{HOST}:{port}"
    print(f"\n==================================================")
    print(f" 🖼️  WALLPAPER CURATOR PRO IS RUNNING")
    print(f"==================================================")
    print(f" Web UI: {url}")
    print(f" 📥 Ingest Wallhaven: Click '📥 Ingest Wallhaven'")
    print(f" 🤖 Auto-Classifier: Click '🤖 Auto-Classify'")
    print(f" ⚡ Command Bar: Press [Ctrl+K] or [/] anytime")
    print(f" 📊 Analytics Dashboard: Click '📊 Stats'")
    print(f" ⌨️ Hotkeys Legend: Press [?]")
    print(f"==================================================\n")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] Shutting down Wallpaper Curator Pro server...")
        server.server_close()


if __name__ == "__main__":
    port = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    run_curator_server(port=port)
