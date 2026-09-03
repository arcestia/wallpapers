"""Wallhaven API client and automated wallpaper collector."""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from ..config import INCOMING_DIR, DB_PATH, WALLPAPERS_DIR
from ..pipeline import process_image
from .base import BaseDownloader


WALLHAVEN_API_BASE = "https://wallhaven.cc/api/v1"


class WallhavenDownloader(BaseDownloader):
    """Downloader for collecting wallpapers from Wallhaven.cc."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        incoming_dir: Path = INCOMING_DIR,
        delay_seconds: float = 1.0,
    ):
        super().__init__(incoming_dir=incoming_dir, delay_seconds=delay_seconds)
        self.api_key = api_key or os.getenv("WALLHAVEN_API_KEY")

    def get_wallpaper_details(self, wallpaper_id: str) -> Dict[str, Any]:
        """Fetch metadata for a single wallpaper by its alphanumeric ID."""
        clean_id = wallpaper_id.strip()
        # Handle full URL input (e.g. https://wallhaven.cc/w/1k7j9w or https://whvn.cc/1k7j9w)
        match = re.search(r"(?:/w/|whvn\.cc/)([a-zA-Z0-9]+)", clean_id)
        if match:
            clean_id = match.group(1)

        url = f"{WALLHAVEN_API_BASE}/w/{clean_id}"
        if self.api_key:
            url += f"?apikey={self.api_key}"

        res = self._fetch_json(url)
        return res.get("data", {})

    def search(
        self,
        query: Optional[str] = None,
        categories: str = "111",  # General/Anime/People binary string
        purity: str = "100",      # SFW/Sketchy/NSFW binary string
        sorting: str = "toplist", # toplist, hot, views, random, date_added
        order: str = "desc",
        top_range: str = "1M",    # 1d, 3d, 1w, 1M, 3M, 6M, 1y
        atleast: str = "2560x1440",
        ratios: Optional[str] = None,
        page: int = 1,
        seed: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search wallpapers on Wallhaven with 2K+ resolution filter."""
        params: Dict[str, Any] = {
            "categories": categories,
            "purity": purity,
            "sorting": sorting,
            "order": order,
            "atleast": atleast,
            "page": page,
        }

        if query:
            params["q"] = query
        if sorting == "toplist" and top_range:
            params["topRange"] = top_range
        if ratios:
            params["ratios"] = ratios
        if seed:
            params["seed"] = seed
        if self.api_key:
            params["apikey"] = self.api_key

        url = f"{WALLHAVEN_API_BASE}/search?{urlencode(params)}"
        return self._fetch_json(url)

    def download_and_ingest_single(
        self,
        wallpaper_id: str,
        category_hint: Optional[str] = None,
        type_hint: Optional[str] = None,
        db_path: Path = DB_PATH,
        wallpapers_dir: Path = WALLPAPERS_DIR,
    ) -> Dict[str, Any]:
        """Download a single Wallhaven wallpaper by ID/URL and ingest it into the archive."""
        data = self.get_wallpaper_details(wallpaper_id)
        if not data:
            return {"status": "FAILED", "reason": f"Wallpaper {wallpaper_id} not found on Wallhaven."}

        file_url = data.get("path")
        source_url = data.get("url") or f"https://wallhaven.cc/w/{data.get('id')}"
        uploader = data.get("uploader", {}).get("username")
        tags = [t.get("name") for t in data.get("tags", [])]

        # Infer category hint from tags or Wallhaven category if not supplied
        inferred_cat = category_hint
        if not inferred_cat and tags:
            tag_str = " ".join(tags).lower()
            if any(k in tag_str for k in ["anime", "manga", "waifu"]):
                inferred_cat = "Anime"
            elif any(k in tag_str for k in ["cyberpunk", "neon"]):
                inferred_cat = "Cyberpunk"
            elif any(k in tag_str for k in ["space", "galaxy", "nebula"]):
                inferred_cat = "Space"
            elif any(k in tag_str for k in ["game", "gaming", "video game"]):
                inferred_cat = "Gaming"

        ext = file_url.split(".")[-1] if "." in file_url else "jpg"
        dest_filename = f"wallhaven_{data.get('id')}.{ext}"

        # Download into incoming
        local_path = self._download_file(file_url, dest_filename=dest_filename)

        # Ingest through standard 9-stage pipeline
        result = process_image(
            file_path=local_path,
            source=f"Wallhaven (@{uploader})" if uploader else "Wallhaven",
            source_url=source_url,
            category_hint=inferred_cat,
            type_hint=type_hint,
            move=True,
            db_path=db_path,
            wallpapers_dir=wallpapers_dir,
        )

        return {
            "status": result.status,
            "reason": result.reason,
            "wallpaper_id": result.wallpaper_id,
            "target_path": str(result.target_path) if result.target_path else None,
            "wallhaven_id": data.get("id"),
        }

    def batch_collect(
        self,
        query: Optional[str] = None,
        limit: int = 10,
        categories: str = "111",
        purity: str = "100",
        sorting: str = "toplist",
        top_range: str = "1M",
        ratios: Optional[str] = None,
        max_pages: int = 5,
        category_hint: Optional[str] = None,
        type_hint: Optional[str] = None,
        db_path: Path = DB_PATH,
        wallpapers_dir: Path = WALLPAPERS_DIR,
    ) -> Dict[str, Any]:
        """
        Search and automatically collect wallpapers up to limit.
        """
        summary = {
            "requested_limit": limit,
            "total_scanned": 0,
            "completed": 0,
            "rejected": 0,
            "duplicates": 0,
            "failed": 0,
            "items": [],
        }

        current_page = 1
        while summary["completed"] < limit and current_page <= max_pages:
            try:
                search_res = self.search(
                    query=query,
                    categories=categories,
                    purity=purity,
                    sorting=sorting,
                    top_range=top_range,
                    ratios=ratios,
                    page=current_page,
                )
            except Exception as e:
                summary["failed"] += 1
                break

            items = search_res.get("data", [])
            if not items:
                break

            for item in items:
                if summary["completed"] >= limit:
                    break

                summary["total_scanned"] += 1
                wh_id = item.get("id")
                file_url = item.get("path")
                source_url = item.get("url")

                if not file_url:
                    continue

                ext = file_url.split(".")[-1] if "." in file_url else "jpg"
                dest_filename = f"wallhaven_{wh_id}.{ext}"

                try:
                    local_path = self._download_file(file_url, dest_filename=dest_filename)
                except Exception as e:
                    summary["failed"] += 1
                    summary["items"].append({"id": wh_id, "status": "FAILED", "reason": f"Download error: {e}"})
                    continue

                item_category = category_hint or query or item.get("category")
                res = process_image(
                    file_path=local_path,
                    source="Wallhaven",
                    source_url=source_url,
                    category_hint=item_category,
                    type_hint=type_hint,
                    move=True,
                    db_path=db_path,
                    wallpapers_dir=wallpapers_dir,
                )

                summary["items"].append({"id": wh_id, "status": res.status, "reason": res.reason, "db_id": res.wallpaper_id})

                if res.status == "COMPLETED":
                    summary["completed"] += 1
                elif res.status == "REJECTED":
                    summary["rejected"] += 1
                elif res.status == "DUPLICATE":
                    summary["duplicates"] += 1
                else:
                    summary["failed"] += 1

            meta = search_res.get("meta", {})
            last_page = meta.get("last_page", 1)
            if current_page >= last_page:
                break
            current_page += 1

        return summary
