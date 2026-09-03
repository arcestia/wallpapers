"""Unit tests for Wallhaven Downloader with mock API responses."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image

from wallpaper_agent.db import get_all_wallpapers, init_db
from wallpaper_agent.downloaders.wallhaven import WallhavenDownloader


class TestWallhavenDownloader(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db_path = self.temp_dir / "test_wallpapers.db"
        self.wallpapers_dir = self.temp_dir / "Wallpapers"
        self.incoming_dir = self.temp_dir / "incoming"
        init_db(self.db_path)

        self.downloader = WallhavenDownloader(
            incoming_dir=self.incoming_dir,
            delay_seconds=0.0,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_dummy_image(self, file_path: Path, width: int = 3840, height: int = 2160):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (width, height), color=(50, 100, 150))
        img.save(file_path, format="JPEG")

    @patch("wallpaper_agent.downloaders.base.BaseDownloader._fetch_json")
    @patch("wallpaper_agent.downloaders.base.BaseDownloader._download_file")
    def test_single_wallpaper_download(self, mock_download, mock_fetch):
        # Mock Wallhaven wallpaper details response
        mock_fetch.return_value = {
            "data": {
                "id": "1k7j9w",
                "url": "https://wallhaven.cc/w/1k7j9w",
                "path": "https://w.wallhaven.cc/full/1k/wallhaven-1k7j9w.jpg",
                "dimension_x": 3840,
                "dimension_y": 2160,
                "category": "anime",
                "uploader": {"username": "AnimeLover"},
                "tags": [{"name": "anime girl"}, {"name": "cyberpunk"}],
            }
        }

        # Mock downloaded image file
        mock_file = self.temp_dir / "mock_1k7j9w.jpg"
        self.create_dummy_image(mock_file, 3840, 2160)
        mock_download.return_value = mock_file

        res = self.downloader.download_and_ingest_single(
            wallpaper_id="1k7j9w",
            db_path=self.db_path,
            wallpapers_dir=self.wallpapers_dir,
        )

        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["wallpaper_id"], 1)

        wallpapers = get_all_wallpapers(self.db_path)
        self.assertEqual(len(wallpapers), 1)
        self.assertEqual(wallpapers[0]["source"], "Wallhaven (@AnimeLover)")
        self.assertEqual(wallpapers[0]["source_url"], "https://wallhaven.cc/w/1k7j9w")

    @patch("wallpaper_agent.downloaders.base.BaseDownloader._fetch_json")
    @patch("wallpaper_agent.downloaders.base.BaseDownloader._download_file")
    def test_batch_collect(self, mock_download, mock_fetch):
        # Mock search response
        mock_fetch.return_value = {
            "data": [
                {
                    "id": "wh1",
                    "url": "https://wallhaven.cc/w/wh1",
                    "path": "https://w.wallhaven.cc/full/wh/wallhaven-wh1.jpg",
                },
                {
                    "id": "wh2",
                    "url": "https://wallhaven.cc/w/wh2",
                    "path": "https://w.wallhaven.cc/full/wh/wallhaven-wh2.jpg",
                },
            ],
            "meta": {"current_page": 1, "last_page": 1, "total": 2},
        }

        def mock_download_side_effect(file_url, dest_filename=None):
            file_name = dest_filename or "test.jpg"
            img_path = self.temp_dir / file_name
            # create distinct images
            width = 2560 if "wh1" in file_name else 3840
            self.create_dummy_image(img_path, width, 1440)
            return img_path

        mock_download.side_effect = mock_download_side_effect

        summary = self.downloader.batch_collect(
            query="cyberpunk",
            limit=2,
            db_path=self.db_path,
            wallpapers_dir=self.wallpapers_dir,
        )

        self.assertEqual(summary["total_scanned"], 2)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(len(get_all_wallpapers(self.db_path)), 2)


if __name__ == "__main__":
    unittest.main()
