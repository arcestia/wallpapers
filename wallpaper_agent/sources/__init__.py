"""Wallpaper collection source adapters."""

from .base import SourceAdapter, SourceItem, SourceSearchResult
from .deviantart import DeviantArtAdapter
from .registry import GLOBAL_SOURCE_REGISTRY, SourceRegistry, get_source_registry

__all__ = [
    "SourceAdapter",
    "SourceItem",
    "SourceSearchResult",
    "DeviantArtAdapter",
    "SourceRegistry",
    "GLOBAL_SOURCE_REGISTRY",
    "get_source_registry",
]
