"""Base source adapter contract and data models for wallpaper sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("wallpaper_agent.sources")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 WallpaperCurator/2.0"
)


@dataclass
class SourceItem:
    """Unified representation of a wallpaper candidate from any source."""
    source_name: str
    source_key: str
    external_id: str
    page_url: str
    image_url: Optional[str] = None
    thumb_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    title: Optional[str] = None
    author: Optional[str] = None
    license: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    category_hint: Optional[str] = None
    is_mature: bool = False
    is_downloadable: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceSearchResult:
    """Standardized search response from any source adapter."""
    success: bool
    items: List[SourceItem] = field(default_factory=list)
    total_found: Optional[int] = None
    has_more: bool = False
    next_cursor: Optional[str] = None
    error: Optional[str] = None


class SourceAdapter(ABC):
    """Abstract interface for all wallpaper source adapters."""

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, timeout: int = 20):
        self.user_agent = user_agent
        self.timeout = timeout

    @property
    @abstractmethod
    def source_key(self) -> str:
        """Unique identifier key (e.g. 'alphacoders', 'deviantart')."""
        pass

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable display name (e.g. 'Alpha Coders (Wallpaper Abyss)')."""
        pass

    @property
    @abstractmethod
    def requires_auth(self) -> bool:
        """Whether this source requires API key or OAuth credentials."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if credentials/prerequisites are configured."""
        pass

    @abstractmethod
    def get_config_help(self) -> str:
        """Instructions on how to configure this source (e.g. environment variable name)."""
        pass

    @abstractmethod
    def search(
        self,
        query: Optional[str] = None,
        limit: int = 10,
        page: int = 1,
        cursor: Optional[str] = None,
        category_hint: Optional[str] = None,
        include_mature: bool = False,
        sort: Optional[str] = None,
        time_range: Optional[str] = None,
        subreddit: Optional[str] = None,
    ) -> SourceSearchResult:
        """Search or browse items from this source."""
        pass

    def resolve_download_url(self, item: SourceItem) -> Optional[str]:
        """Resolve the final downloadable image URL for a given item.

        Default implementation returns item.image_url.
        Adapters can override if an additional API step is needed (e.g. DeviantArt download endpoint).
        """
        return item.image_url

    def _fetch_json(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        method: Optional[str] = None,
    ) -> Tuple[bool, Any, Optional[str]]:
        """Utility for making JSON HTTP requests with timeouts and standard headers."""
        req_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return True, json.loads(raw), None
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = str(e)
            return False, None, f"HTTP {e.code}: {err_body[:200]}"
        except Exception as e:
            return False, None, f"{type(e).__name__}: {e}"
