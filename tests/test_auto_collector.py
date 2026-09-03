"""Unit tests for AutoCollector daemon."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image

from wallpaper_agent.auto_collector import AutoCollector
from wallpaper_agent.db import get_all_wallpapers, init_db


class TestAutoCollector(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db_path = self.temp_dir / "test_wallpapers.db"
        self.wallpapers_dir = self.temp_dir / "Wallpapers"
        self.incoming_dir = self.temp_dir / "incoming"
        self.config_path = self.temp_dir / "test_config.json"
        init_db(self.db_path)

        test_config = {
            "settings": {"delay_seconds": 0.0, "default_limit_per_target": 2},
            "targets": [
                {"name": "Cyberpunk", "query": "cyberpunk", "limit": 2, "category_hint": "Cyberpunk"}
            ],
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(test_config, f)

        self.collector = AutoCollector(
            config_path=self.config_path,
            db_path=self.db_path,
            wallpapers_dir=self.wallpapers_dir,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_dummy_image(self, file_path: Path, width: int = 3840, height: int = 2160):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (width, height), color=(50, 100, 150))
        img.save(file_path, format="JPEG")

    @patch("wallpaper_agent.downloaders.wallhaven.WallhavenDownloader.batch_collect")
    def test_run_cycle(self, mock_batch_collect):
        mock_batch_collect.return_value = {
            "requested_limit": 2,
            "total_scanned": 2,
            "completed": 2,
            "rejected": 0,
            "duplicates": 0,
            "failed": 0,
            "items": [
                {"id": "w1", "status": "COMPLETED", "db_id": 1},
                {"id": "w2", "status": "COMPLETED", "db_id": 2},
            ],
        }

        summary = self.collector.run_cycle()
        self.assertEqual(summary["targets_count"], 1)
        self.assertEqual(summary["total_scanned"], 2)
        self.assertEqual(summary["total_ingested"], 2)
        self.assertEqual(summary["total_failed"], 0)


if __name__ == "__main__":
    unittest.main()
