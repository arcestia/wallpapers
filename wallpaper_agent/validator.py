"""Image validation and resolution verification."""

from math import gcd
from pathlib import Path
from typing import Dict, NamedTuple, Optional
from PIL import Image
Image.MAX_IMAGE_PIXELS = None  # Allow 8K/16K wallpapers without decompression warning

from .config import MIN_PIXELS, SUPPORTED_EXTENSIONS


class ValidationResult(NamedTuple):
    is_valid: bool
    reason: str
    width: int = 0
    height: int = 0
    pixel_count: int = 0
    format: str = ""
    filesize: int = 0
    aspect_ratio: str = ""
    orientation: str = ""


def compute_aspect_ratio(width: int, height: int) -> str:
    """Compute simplified or common standard aspect ratio string."""
    if width <= 0 or height <= 0:
        return "UNKNOWN"

    ratio = width / height

    # Standard common aspect ratios with tolerance
    common_ratios = [
        (16 / 9, "16:9"),
        (16 / 10, "16:10"),
        (21 / 9, "21:9"),
        (32 / 9, "32:9"),
        (4 / 3, "4:3"),
        (5 / 4, "5:4"),
        (3 / 2, "3:2"),
        (9 / 16, "9:16"),
        (10 / 16, "10:16"),
        (1 / 1, "1:1"),
    ]

    for target_val, name in common_ratios:
        if abs(ratio - target_val) < 0.08:
            return name

    # Simplify using GCD
    divisor = gcd(width, height)
    simplified_w = width // divisor
    simplified_h = height // divisor

    # If simplified numbers are too large, provide float approximation
    if simplified_w > 100 or simplified_h > 100:
        return f"{ratio:.2f}:1"

    return f"{simplified_w}:{simplified_h}"


def compute_orientation(width: int, height: int) -> str:
    """Classify image orientation."""
    if width <= 0 or height <= 0:
        return "UNKNOWN"

    ratio = width / height
    if ratio >= 2.1:
        return "Ultrawide"
    elif ratio > 1.2:
        return "Landscape"
    elif 0.8 <= ratio <= 1.2:
        return "Square"
    else:
        return "Portrait"


def validate_image(file_path: Path) -> ValidationResult:
    """
    Validate that an image file is readable, not corrupt, and meets the 2K+ requirement.
    """
    path = Path(file_path)
    if not path.exists():
        return ValidationResult(False, f"File does not exist: {path}")

    if path.stat().st_size == 0:
        return ValidationResult(False, "Empty file (0 bytes)")

    filesize = path.stat().st_size

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return ValidationResult(False, f"Unsupported file extension: {ext}")

    try:
        with Image.open(path) as img:
            img.verify()

        # Re-open after verify() because verify() closes the file descriptor
        with Image.open(path) as img:
            width, height = img.size
            img_format = img.format or ext.replace(".", "").upper()
            pixel_count = width * height
            aspect_ratio = compute_aspect_ratio(width, height)
            orientation = compute_orientation(width, height)

    except Exception as e:
        return ValidationResult(False, f"Corrupted or invalid image: {e}")

    # Check 2K minimum resolution requirement
    if pixel_count < MIN_PIXELS:
        return ValidationResult(
            False,
            f"Below 2K resolution requirement: {width}x{height} = {pixel_count:,} pixels (< {MIN_PIXELS:,} required)",
            width=width,
            height=height,
            pixel_count=pixel_count,
            format=img_format,
            filesize=filesize,
            aspect_ratio=aspect_ratio,
            orientation=orientation,
        )

    return ValidationResult(
        True,
        "Valid 2K+ wallpaper",
        width=width,
        height=height,
        pixel_count=pixel_count,
        format=img_format,
        filesize=filesize,
        aspect_ratio=aspect_ratio,
        orientation=orientation,
    )
