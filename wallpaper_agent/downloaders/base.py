"""Base downloader interface and common utilities."""

import os
import time
import urllib.request
import urllib.error
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import INCOMING_DIR


DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WallpaperAgent/1.0"


class BaseDownloader(ABC):
    """Abstract base class for wallpaper downloaders."""

    def __init__(self, incoming_dir: Path = INCOMING_DIR, delay_seconds: float = 1.0):
        self.incoming_dir = Path(incoming_dir)
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self.delay_seconds = delay_seconds
        self.user_agent = DEFAULT_USER_AGENT

    def _fetch_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Fetch JSON from a URL with standard user-agent and error handling."""
        req_headers = {"User-Agent": self.user_agent}
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            data = response.read().decode("utf-8")
            return json.loads(data)

    def _download_file(self, file_url: str, dest_filename: Optional[str] = None) -> Path:
        """Download remote image file into the incoming directory."""
        if not dest_filename:
            clean_name = file_url.split("?")[0].split("/")[-1] or "downloaded_wallpaper.jpg"
            dest_filename = clean_name

        dest_path = self.incoming_dir / dest_filename
        counter = 1
        stem = dest_path.stem
        ext = dest_path.suffix or ".jpg"
        while dest_path.exists():
            dest_path = self.incoming_dir / f"{stem}_{counter}{ext}"
            counter += 1

        req = urllib.request.Request(file_url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=45) as response, open(dest_path, "wb") as out_f:
            out_f.write(response.read())

        # Rate limiting delay
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        return dest_path
