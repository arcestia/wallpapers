"""Comprehensive unit and integration tests for the Wallpaper Collection Agent."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from wallpaper_agent.config import CATEGORIES, MIN_PIXELS, TYPES
from wallpaper_agent.db import (
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
from wallpaper_agent.validator import (
    compute_aspect_ratio,
    compute_orientation,
    validate_image,
)
from wallpaper_agent.classifier import (
    classify_ai,
    classify_category,
    classify_image,
)
from wallpaper_agent.deduplicator import (
    check_duplicates,
    compute_perceptual_hash,
    compute_sha256,
)
from wallpaper_agent.pipeline import process_image, process_incoming
from wallpaper_agent.storage import ensure_storage_structure, get_target_path, store_wallpaper


class TestWallpaperAgent(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db_path = self.temp_dir / "test_wallpapers.db"
        self.wallpapers_dir = self.temp_dir / "Wallpapers"
        self.incoming_dir = self.temp_dir / "incoming"
        init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_dummy_image(
        self,
        filename: str,
        width: int = 2560,
        height: int = 1440,
        color: tuple = (255, 0, 0),
        folder: Path = None,
        img_format: str = "JPEG",
    ) -> Path:
        target_folder = folder or self.temp_dir
        target_folder.mkdir(parents=True, exist_ok=True)
        img_path = target_folder / filename
        img = Image.new("RGB", (width, height), color=color)
        img.save(img_path, format=img_format)
        return img_path

    # --- Validator Tests ---
    def test_resolution_requirement_2k_pass(self):
        """Images with width * height >= 3,686,400 must pass."""
        img_2560x1440 = self.create_dummy_image("2k.jpg", 2560, 1440)
        res = validate_image(img_2560x1440)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.pixel_count, 2560 * 1440)

        img_4k = self.create_dummy_image("4k.png", 3840, 2160, img_format="PNG")
        res_4k = validate_image(img_4k)
        self.assertTrue(res_4k.is_valid)

        img_ultrawide = self.create_dummy_image("ultrawide.jpg", 3440, 1440)
        res_uw = validate_image(img_ultrawide)
        self.assertTrue(res_uw.is_valid)

    def test_resolution_boundary(self):
        """Test boundary conditions for 3,686,400 pixels."""
        img_exact = self.create_dummy_image("exact.jpg", 2560, 1440)
        self.assertTrue(validate_image(img_exact).is_valid)

        img_below = self.create_dummy_image("below.jpg", 2560, 1439)
        self.assertFalse(validate_image(img_below).is_valid)

    def test_resolution_requirement_sub_2k_reject(self):
        """1080p and lower images must be rejected."""
        img_1080p = self.create_dummy_image("1080p.jpg", 1920, 1080)
        res = validate_image(img_1080p)
        self.assertFalse(res.is_valid)
        self.assertIn("Below 2K", res.reason)

    def test_corrupted_image_rejection(self):
        """Corrupted or invalid files must be rejected."""
        corrupt_file = self.temp_dir / "corrupt.jpg"
        with open(corrupt_file, "wb") as f:
            f.write(b"not an image file")
        res = validate_image(corrupt_file)
        self.assertFalse(res.is_valid)
        self.assertIn("Corrupted", res.reason)

    def test_empty_file_rejection(self):
        """Empty 0-byte files must be rejected."""
        empty_file = self.temp_dir / "empty.jpg"
        empty_file.touch()
        res = validate_image(empty_file)
        self.assertFalse(res.is_valid)
        self.assertIn("Empty file", res.reason)

    def test_aspect_ratio_and_orientation(self):
        self.assertEqual(compute_aspect_ratio(2560, 1440), "16:9")
        self.assertEqual(compute_aspect_ratio(3440, 1440), "21:9")
        self.assertEqual(compute_aspect_ratio(2560, 1600), "16:10")
        self.assertEqual(compute_aspect_ratio(2000, 2000), "1:1")
        self.assertEqual(compute_orientation(2560, 1440), "Landscape")
        self.assertEqual(compute_orientation(3440, 1440), "Ultrawide")
        self.assertEqual(compute_orientation(1440, 2560), "Portrait")
        self.assertEqual(compute_orientation(2000, 2000), "Square")

    # --- Deduplication Tests ---
    def test_sha256_and_phash_computation(self):
        img_path = self.create_dummy_image("hash_test.jpg", 2560, 1440)
        sha = compute_sha256(img_path)
        phash = compute_perceptual_hash(img_path)
        self.assertEqual(len(sha), 64)
        self.assertTrue(len(phash) > 0)

    def test_duplicate_rejection(self):
        img1 = self.create_dummy_image("orig.jpg", 2560, 1440, color=(100, 150, 200))
        res1 = process_image(
            img1,
            db_path=self.db_path,
            wallpapers_dir=self.wallpapers_dir,
        )
        self.assertEqual(res1.status, "COMPLETED")
        self.assertEqual(res1.wallpaper_id, 1)

        # Re-processing the exact same image must yield DUPLICATE
        res2 = process_image(
            img1,
            db_path=self.db_path,
            wallpapers_dir=self.wallpapers_dir,
        )
        self.assertEqual(res2.status, "DUPLICATE")

    # --- Classifier Tests ---
    def test_ai_classification(self):
        ai_path = self.temp_dir / "skiddle-generated-1.jpg"
        ai_path.touch()
        ai_type, conf, _ = classify_ai(ai_path)
        self.assertEqual(ai_type, "AI")

        midjourney_path = self.temp_dir / "midjourney_artwork.jpg"
        midjourney_path.touch()
        mj_type, _, _ = classify_ai(midjourney_path)
        self.assertEqual(mj_type, "AI")

        unknown_path = self.temp_dir / "unnamed_photo.jpg"
        unknown_path.touch()
        un_type, _, _ = classify_ai(unknown_path)
        self.assertEqual(un_type, "UNKNOWN")

    def test_category_classification(self):
        cyber_path = self.temp_dir / "neon_tokyo_cyberpunk.jpg"
        self.assertEqual(classify_category(cyber_path), "Cyberpunk")

        anime_path = self.temp_dir / "pikachu_sunset_anime.jpg"
        self.assertEqual(classify_category(anime_path), "Anime")

        game_path = self.temp_dir / "endfield_gameplay.jpg"
        self.assertEqual(classify_category(game_path), "Games")

        ocean_path = self.temp_dir / "tropical_beach_ocean_waves.jpg"
        self.assertEqual(classify_category(ocean_path), "Landscape")

        space_path = self.temp_dir / "deep_galaxy_nebula_stars.jpg"
        self.assertEqual(classify_category(space_path), "Space")

        cars_path = self.temp_dir / "red_ferrari_supercar.jpg"
        self.assertEqual(classify_category(cars_path), "Cars")

        nature_path = self.temp_dir / "green_forest_trees_waterfall.jpg"
        self.assertEqual(classify_category(nature_path), "Landscape")

        animal_path = self.temp_dir / "wild_wolf_snow.jpg"
        self.assertEqual(classify_category(animal_path), "Animals")

        comic_path = self.temp_dir / "batman_dark_knight_superhero.jpg"
        self.assertEqual(classify_category(comic_path), "Games")

        pixel_path = self.temp_dir / "retro_city_pixel_art_8_bit.jpg"
        self.assertEqual(classify_category(pixel_path), "Pixel Art")

        music_path = self.temp_dir / "electric_guitar_concert_music.jpg"
        self.assertEqual(classify_category(music_path), "Music")

        horror_path = self.temp_dir / "gothic_skull_dark_horror.jpg"
        self.assertEqual(classify_category(horror_path), "Other")

        military_path = self.temp_dir / "f22_fighter_jet_military.jpg"
        self.assertEqual(classify_category(military_path), "Weapons")

    # --- Pipeline & Database Tests ---
    def test_sequential_permanent_ids(self):
        """Sequential IDs must be assigned: 1, 2, 3..."""
        for i in range(1, 4):
            img = self.create_dummy_image(
                f"img_{i}.jpg",
                2560,
                1440,
                color=(i * 30, i * 40, i * 50),
            )
            res = process_image(
                img,
                db_path=self.db_path,
                wallpapers_dir=self.wallpapers_dir,
            )
            self.assertEqual(res.status, "COMPLETED")
            self.assertEqual(res.wallpaper_id, i)
            # File must be named <id>.<ext>
            self.assertEqual(res.target_path.name, f"{i}.jpg")
            self.assertTrue(res.target_path.exists())

        all_w = get_all_wallpapers(self.db_path)
        self.assertEqual(len(all_w), 3)
        self.assertEqual([w["id"] for w in all_w], [1, 2, 3])

    def test_process_incoming_directory(self):
        """Process incoming batch directory."""
        # 1 valid 2k image
        self.create_dummy_image("valid.jpg", 2560, 1440, folder=self.incoming_dir)
        # 1 sub-2k image (1080p)
        self.create_dummy_image("invalid.jpg", 1920, 1080, folder=self.incoming_dir)

        results = process_incoming(
            incoming_dir=self.incoming_dir,
            db_path=self.db_path,
            wallpapers_dir=self.wallpapers_dir,
        )
        self.assertEqual(results["total"], 2)
        self.assertEqual(results["completed"], 1)
        self.assertEqual(results["rejected"], 1)

    def test_stats_generation(self):
        img = self.create_dummy_image("anime_girl.jpg", 3840, 2160, img_format="PNG")
        process_image(
            img,
            type_hint="NON-AI",
            category_hint="Anime",
            db_path=self.db_path,
            wallpapers_dir=self.wallpapers_dir,
        )
        stats = get_stats(self.db_path)
        self.assertEqual(stats["total_wallpapers"], 1)
        self.assertEqual(stats["curated_wallpapers"], 0)


if __name__ == "__main__":
    unittest.main()
