"""Automated Wallpaper Collection Daemon and Scheduler."""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import BASE_DIR, DB_PATH, WALLPAPERS_DIR
from .downloaders.wallhaven import WallhavenDownloader


DEFAULT_CONFIG_PATH = BASE_DIR / "collector_config.json"


class AutoCollector:
    """Automated collector daemon for multi-topic wallpaper ingestion."""

    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG_PATH,
        api_key: Optional[str] = None,
        db_path: Path = DB_PATH,
        wallpapers_dir: Path = WALLPAPERS_DIR,
    ):
        self.config_path = Path(config_path)
        self.api_key = api_key
        self.db_path = db_path
        self.wallpapers_dir = wallpapers_dir
        self.config = self.load_config()

    def load_config(self) -> Dict[str, Any]:
        """Load collection configuration from JSON file."""
        if not self.config_path.exists():
            return {
                "settings": {"delay_seconds": 1.0, "default_limit_per_target": 10},
                "targets": [
                    {"name": "Top Wallpapers", "query": None, "sorting": "toplist", "limit": 10}
                ],
            }
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def run_cycle(self, limit_override: Optional[int] = None) -> Dict[str, Any]:
        """Execute one complete collection cycle across all configured targets."""
        settings = self.config.get("settings", {})
        delay = settings.get("delay_seconds", 1.0)
        targets = self.config.get("targets", [])

        downloader = WallhavenDownloader(
            api_key=self.api_key,
            delay_seconds=delay,
        )

        cycle_summary = {
            "timestamp": datetime.now().isoformat(),
            "targets_count": len(targets),
            "total_scanned": 0,
            "total_ingested": 0,
            "total_rejected": 0,
            "total_duplicates": 0,
            "total_failed": 0,
            "target_results": [],
        }

        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting collection cycle ({len(targets)} targets)...")

        for idx, target in enumerate(targets, 1):
            name = target.get("name", f"Target #{idx}")
            query = target.get("query")
            limit = limit_override or target.get("limit", settings.get("default_limit_per_target", 10))
            sorting = target.get("sorting", "toplist")
            top_range = target.get("top_range", "1M")
            categories = target.get("categories", "111")
            purity = target.get("purity", settings.get("purity", "100"))
            ratios = target.get("ratios")
            cat_hint = target.get("category_hint")
            type_hint = target.get("type_hint")

            print(f"  -> [{idx}/{len(targets)}] Collecting '{name}' (limit={limit}, sorting={sorting})...")

            try:
                res = downloader.batch_collect(
                    query=query,
                    limit=limit,
                    categories=categories,
                    purity=purity,
                    sorting=sorting,
                    top_range=top_range,
                    ratios=ratios,
                    max_pages=10,
                    category_hint=cat_hint,
                    type_hint=type_hint,
                    db_path=self.db_path,
                    wallpapers_dir=self.wallpapers_dir,
                )

                cycle_summary["total_scanned"] += res["total_scanned"]
                cycle_summary["total_ingested"] += res["completed"]
                cycle_summary["total_rejected"] += res["rejected"]
                cycle_summary["total_duplicates"] += res["duplicates"]
                cycle_summary["total_failed"] += res["failed"]

                cycle_summary["target_results"].append({
                    "name": name,
                    "scanned": res["total_scanned"],
                    "ingested": res["completed"],
                    "rejected": res["rejected"],
                    "duplicates": res["duplicates"],
                    "failed": res["failed"],
                })

                print(f"     Ingested: {res['completed']} | Duplicates: {res['duplicates']} | Under 2K: {res['rejected']}")

            except Exception as e:
                print(f"     Failed: {e}")
                cycle_summary["total_failed"] += 1

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Cycle finished: {cycle_summary['total_ingested']} new wallpapers ingested.\n")
        return cycle_summary

    def run_continuous(self, interval_seconds: int = 3600, limit_override: Optional[int] = None) -> None:
        """Run continuous automated collection loop with sleep interval."""
        print(f"Starting Wallpaper Collection Daemon (interval: {interval_seconds}s / {interval_seconds/60:.1f}m)...")
        print("Press Ctrl+C to stop.\n")

        cycle_num = 1
        try:
            while True:
                print(f"--- Cycle #{cycle_num} ---")
                self.run_cycle(limit_override=limit_override)
                cycle_num += 1

                next_run = time.time() + interval_seconds
                print(f"Sleeping for {interval_seconds}s. Next run at {datetime.fromtimestamp(next_run).strftime('%H:%M:%S')}...")
                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            print("\nDaemon stopped gracefully by user.")
