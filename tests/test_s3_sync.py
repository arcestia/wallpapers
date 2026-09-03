"""Unit tests for S3/B2 sync engine (curate_s3.py) with mocked boto3."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import curate_s3
from curate_db import db_session, init_db, insert_wallpaper


class TestS3KeyGeneration(unittest.TestCase):
    def test_generate_s3_key_basic(self):
        key = curate_s3.generate_s3_key("Anime", "1.png")
        self.assertEqual(key, "images/wallpapers/Anime/1.png")

    def test_generate_s3_key_strips_slashes(self):
        key = curate_s3.generate_s3_key("/Anime/", "/1.png")
        self.assertEqual(key, "images/wallpapers/Anime/1.png")

    def test_generate_cdn_url(self):
        url = curate_s3.generate_cdn_url("images/wallpapers/Anime/1.png")
        self.assertEqual(url, "https://cdn.skiddle.id/images/wallpapers/Anime/1.png")

    def test_generate_cdn_url_strips_leading_slash(self):
        url = curate_s3.generate_cdn_url("/images/wallpapers/Anime/1.png")
        self.assertTrue(url.startswith("https://cdn.skiddle.id/images/wallpapers/"))


class TestGuessContentType(unittest.TestCase):
    def test_common_extensions(self):
        self.assertEqual(curate_s3.guess_content_type(Path("a.jpg")), "image/jpeg")
        self.assertEqual(curate_s3.guess_content_type(Path("a.jpeg")), "image/jpeg")
        self.assertEqual(curate_s3.guess_content_type(Path("a.png")), "image/png")
        self.assertEqual(curate_s3.guess_content_type(Path("a.webp")), "image/webp")

    def test_unknown_extension(self):
        ct = curate_s3.guess_content_type(Path("a.xyz"))
        self.assertTrue(ct.startswith("image/") or ct == "application/octet-stream")


class TestUploadFile(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.img_path = self.temp_dir / "test.png"
        Image.new("RGB", (64, 64), (255, 0, 0)).save(self.img_path, format="PNG")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_upload_success(self):
        mock_client = MagicMock()
        success, cdn_url, err = curate_s3.upload_file(
            self.img_path, "images/wallpapers/Test/test.png", bucket="test-bucket", client=mock_client
        )
        self.assertTrue(success)
        self.assertIn("cdn.skiddle.id", cdn_url)
        self.assertIsNone(err)
        mock_client.upload_file.assert_called_once()
        args, kwargs = mock_client.upload_file.call_args
        self.assertEqual(args[1], "test-bucket")
        self.assertEqual(args[2], "images/wallpapers/Test/test.png")
        self.assertEqual(kwargs["ExtraArgs"]["ContentType"], "image/png")
        self.assertIn("max-age=31536000", kwargs["ExtraArgs"]["CacheControl"])

    def test_upload_missing_file(self):
        mock_client = MagicMock()
        success, _, err = curate_s3.upload_file(
            self.temp_dir / "nope.png", "images/wallpapers/Test/nope.png", bucket="b", client=mock_client
        )
        self.assertFalse(success)
        self.assertIn("not found", err)
        mock_client.upload_file.assert_not_called()


class TestSyncCuratedCollection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db_path = self.temp_dir / "test.db"
        self.curated_dir = self.temp_dir / "Curated"
        self.curated_dir.mkdir(parents=True)

        init_db(self.db_path)

        # Create 3 curated wallpapers: 2 pending sync, 1 already synced
        for i, synced in enumerate([False, False, True], start=1):
            cat_dir = self.curated_dir / "Anime"
            cat_dir.mkdir(exist_ok=True)
            img_path = cat_dir / f"{i}.png"
            Image.new("RGB", (64, 64), (i * 10, 0, 0)).save(img_path, format="PNG")
            row = {
                "filename": f"{i}.png",
                "type": "NON-AI",
                "category": "Anime",
                "width": 64,
                "height": 64,
                "format": "PNG",
                "filesize": img_path.stat().st_size,
                "sha256": f"hash-{i}",
                "is_curated": 1,
                "curated_id": i,
                "curated_filename": f"{i}.png",
            }
            if synced:
                row["s3_key"] = f"images/wallpapers/Anime/{i}.png"
                row["s3_url"] = f"https://cdn.skiddle.id/images/wallpapers/Anime/{i}.png"
            insert_wallpaper(row, db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sync_uploads_pending_and_updates_db(self):
        mock_client = MagicMock()
        progress_calls = []

        with patch.object(curate_s3, "S3_BUCKET", "test-bucket"), \
             patch.object(curate_s3, "S3_KEY_ID", "key"), \
             patch.object(curate_s3, "S3_APP_KEY", "secret"), \
             patch.object(curate_s3, "S3_ENDPOINT_URL", "https://s3.test"), \
             patch.object(curate_s3, "get_s3_client", return_value=mock_client), \
             patch("curate_s3.update_wallpaper_s3") as mock_update:

            result = curate_s3.sync_curated_collection(
                workers=2,
                force_all=False,
                progress_callback=lambda d, t, m: progress_calls.append((d, t)),
                curated_dir=self.curated_dir,
                db_path=self.db_path,
            )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["uploaded"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(mock_client.upload_file.call_count, 2)
        self.assertEqual(mock_update.call_count, 2)
        # Progress callback fired for each completed item
        self.assertEqual(len(progress_calls), 2)

    def test_sync_force_all_includes_synced(self):
        mock_client = MagicMock()

        with patch.object(curate_s3, "S3_BUCKET", "test-bucket"), \
             patch.object(curate_s3, "S3_KEY_ID", "key"), \
             patch.object(curate_s3, "S3_APP_KEY", "secret"), \
             patch.object(curate_s3, "S3_ENDPOINT_URL", "https://s3.test"), \
             patch.object(curate_s3, "get_s3_client", return_value=mock_client), \
             patch("curate_s3.update_wallpaper_s3"):

            result = curate_s3.sync_curated_collection(
                workers=2,
                force_all=True,
                curated_dir=self.curated_dir,
                db_path=self.db_path,
            )

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["uploaded"], 3)

    def test_sync_not_configured_raises(self):
        with patch.object(curate_s3, "S3_BUCKET", ""), \
             patch.object(curate_s3, "S3_KEY_ID", ""):
            with self.assertRaises(ValueError):
                curate_s3.sync_curated_collection(
                    curated_dir=self.curated_dir, db_path=self.db_path
                )


class TestSchemaMigration(unittest.TestCase):
    def test_s3_columns_added_to_legacy_db(self):
        """Auto-migration adds s3_key/s3_url/s3_uploaded_at to an old-schema DB."""
        import sqlite3
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "legacy.db"

        # Create a minimal legacy table without S3 columns
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE wallpapers (id INTEGER PRIMARY KEY, filename TEXT, sha256 TEXT UNIQUE)"
        )
        conn.commit()
        conn.close()

        try:
            init_db(db_path)
            with db_session(db_path) as c:
                cols = {r["name"] for r in c.execute("PRAGMA table_info(wallpapers)")}
            self.assertIn("s3_key", cols)
            self.assertIn("s3_url", cols)
            self.assertIn("s3_uploaded_at", cols)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
