"""Downloader modules for external wallpaper sources."""

from .base import BaseDownloader
from .wallhaven import WallhavenDownloader

__all__ = ["BaseDownloader", "WallhavenDownloader"]
