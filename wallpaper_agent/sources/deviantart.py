"""DeviantArt official OAuth2 source adapter."""

import os
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

from .base import SourceAdapter, SourceItem, SourceSearchResult


class DeviantArtAdapter(SourceAdapter):
    """Adapter for official DeviantArt OAuth2 browse and download endpoints."""

    TOKEN_URL = "https://www.deviantart.com/oauth2/token"
    API_BASE = "https://www.deviantart.com/api/v1/oauth2"

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.client_id = client_id or os.getenv("DEVIANTART_CLIENT_ID", "").strip()
        self.client_secret = client_secret or os.getenv("DEVIANTART_CLIENT_SECRET", "").strip()
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def source_key(self) -> str:
        return "deviantart"

    @property
    def source_name(self) -> str:
        return "DeviantArt"

    @property
    def requires_auth(self) -> bool:
        return True

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def get_config_help(self) -> str:
        return (
            "Set DEVIANTART_CLIENT_ID and DEVIANTART_CLIENT_SECRET environment variables "
            "(obtain at https://www.deviantart.com/developers/apps)"
        )

    def _get_access_token(self) -> Tuple[bool, Optional[str], Optional[str]]:
        """Acquire or return cached OAuth2 client-credentials bearer token."""
        now = time.time()
        if self._cached_token and now < (self._token_expires_at - 180):
            return True, self._cached_token, None

        params = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        url = f"{self.TOKEN_URL}?{urllib.parse.urlencode(params)}"
        ok, data, err = self._fetch_json(url)
        if not ok:
            return False, None, f"DeviantArt auth token failed: {err}"

        if not isinstance(data, dict) or "access_token" not in data:
            return False, None, "DeviantArt auth response missing access_token"

        token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        self._cached_token = token
        self._token_expires_at = now + expires_in
        return True, token, None

    def search(
        self,
        query: Optional[str] = None,
        limit: int = 10,
        page: int = 1,
        cursor: Optional[str] = None,
        category_hint: Optional[str] = None,
        include_mature: bool = False,
        **kwargs,
    ) -> SourceSearchResult:
        if not self.is_configured():
            return SourceSearchResult(
                success=False,
                error="DeviantArt credentials not configured. " + self.get_config_help(),
            )

        ok, token, err = self._get_access_token()
        if not ok:
            return SourceSearchResult(success=False, error=err)

        offset = int(cursor) if (cursor and cursor.isdigit()) else ((page - 1) * limit)
        fetch_limit = min(50, max(limit * 2, 20))  # Fetch extra to account for non-downloadable filtering

        headers = {"Authorization": f"Bearer {token}"}

        params: Dict[str, Any] = {
            "offset": offset,
            "limit": fetch_limit,
            "mature_content": "true" if include_mature else "false",
        }
        if query and query.strip():
            params["q"] = query.strip()

        url = f"{self.API_BASE}/browse/home?{urllib.parse.urlencode(params)}"
        ok, data, err = self._fetch_json(url, headers=headers)
        if not ok:
            return SourceSearchResult(success=False, error=f"DeviantArt browse error: {err}")

        if not isinstance(data, dict):
            return SourceSearchResult(success=False, error="Invalid response from DeviantArt API")

        results = data.get("results", [])
        items: List[SourceItem] = []

        for d in results:
            if len(items) >= limit:
                break

            dev_id = d.get("deviationid")
            if not dev_id:
                continue

            # Check creator-enabled download flag
            is_dl = d.get("is_downloadable", False)
            if not is_dl:
                continue

            is_mature = d.get("is_mature", False)
            if is_mature and not include_mature:
                continue

            title = d.get("title") or "DeviantArt Artwork"
            page_url = d.get("url") or f"https://www.deviantart.com/view/{dev_id}"
            author_info = d.get("author", {})
            author_name = author_info.get("username") if isinstance(author_info, dict) else None

            # Thumbnail
            thumbs = d.get("thumbs", [])
            thumb_url = thumbs[0].get("src") if (thumbs and isinstance(thumbs[0], dict)) else None
            content_info = d.get("content", {})
            content_url = content_info.get("src") if isinstance(content_info, dict) else None

            item = SourceItem(
                source_name=self.source_name,
                source_key=self.source_key,
                external_id=str(dev_id),
                page_url=page_url,
                image_url=content_url,  # May be upgraded in resolve_download_url
                thumb_url=thumb_url or content_url,
                title=title,
                author=author_name,
                category_hint=category_hint,
                is_mature=is_mature,
                is_downloadable=True,
                metadata={
                    "download_filesize": d.get("download_filesize"),
                    "published_time": d.get("published_time"),
                },
            )
            items.append(item)

        has_more = data.get("has_more", False)
        next_offset = data.get("next_offset")
        next_cursor = str(next_offset) if (has_more and next_offset is not None) else None

        return SourceSearchResult(
            success=True,
            items=items,
            total_found=data.get("estimated_total"),
            has_more=has_more,
            next_cursor=next_cursor,
        )

    def resolve_download_url(self, item: SourceItem) -> Optional[str]:
        """Fetch the direct original download URL via DeviantArt download endpoint."""
        ok, token, _ = self._get_access_token()
        if not ok or not token:
            return item.image_url

        url = f"{self.API_BASE}/deviation/download/{item.external_id}"
        headers = {"Authorization": f"Bearer {token}"}
        ok, data, _ = self._fetch_json(url, headers=headers)

        if ok and isinstance(data, dict) and data.get("src"):
            # Update reported dimensions if available
            w = data.get("width")
            h = data.get("height")
            if w and h:
                item.width = int(w)
                item.height = int(h)
            return data["src"]

        return item.image_url
