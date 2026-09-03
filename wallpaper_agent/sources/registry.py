"""Source adapter registry and discovery."""

from typing import Any, Dict, List, Optional

from .base import SourceAdapter, SourceItem, SourceSearchResult
from .deviantart import DeviantArtAdapter


class SourceRegistry:
    """Central registry of all automatic wallpaper source adapters."""

    def __init__(self):
        self._adapters: Dict[str, SourceAdapter] = {}
        self._register_defaults()

    def _register_defaults(self):
        self.register(DeviantArtAdapter())

    def register(self, adapter: SourceAdapter) -> None:
        self._adapters[adapter.source_key] = adapter

    def get(self, key: str) -> Optional[SourceAdapter]:
        return self._adapters.get(key)

    def list_keys(self) -> List[str]:
        return list(self._adapters.keys())

    def get_api_payload(self) -> List[Dict[str, Any]]:
        """Return list of sources with configuration status for UI display (no secrets)."""
        payload = []
        for key, adapter in self._adapters.items():
            entry = {
                "key": adapter.source_key,
                "name": adapter.source_name,
                "requires_auth": adapter.requires_auth,
                "is_configured": adapter.is_configured(),
                "config_help": adapter.get_config_help() if not adapter.is_configured() else "Configured and ready",
            }
            payload.append(entry)
        return payload


GLOBAL_SOURCE_REGISTRY = SourceRegistry()


def get_source_registry() -> SourceRegistry:
    return GLOBAL_SOURCE_REGISTRY
