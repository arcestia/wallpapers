"""Unit tests for CLIP Vision Classifier."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image

from wallpaper_agent.vision_classifier import (
    CATEGORY_PROMPTS,
    classify_image_visually,
    is_vision_available,
)


class TestVisionClassifier(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.img_path = self.temp_dir / "sample.jpg"
        img = Image.new("RGB", (2560, 1440), color=(100, 150, 200))
        img.save(self.img_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_category_prompts_completeness(self):
        """All 25 categories must have prompt descriptions."""
        from wallpaper_agent.config import CATEGORIES
        for cat in CATEGORIES:
            self.assertIn(cat, CATEGORY_PROMPTS)
            self.assertTrue(len(CATEGORY_PROMPTS[cat]) > 10)

    @patch("wallpaper_agent.vision_classifier.is_vision_available")
    def test_vision_fallback_when_unavailable(self, mock_is_avail):
        mock_is_avail.return_value = False
        res = classify_image_visually(self.img_path)
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
