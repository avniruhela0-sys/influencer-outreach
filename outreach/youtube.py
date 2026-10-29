"""Thin, error-tolerant client for the YouTube Data API v3."""
from __future__ import annotations

import logging
import time

import requests

API = "https://www.googleapis.com/youtube/v3"
log = logging.getLogger(__name__)


class YouTubeError(Exception):
    pass


class QuotaExceeded(YouTubeError):
    pass


class YouTubeClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, max_retries: int = 3):
        self.key = api_key
        self.session = session or requests.Session()
        self.max_retries = max_retries

    def _get(self, endpoint: str, **params) -> dict:
        params["key"] = self.key
        last = None
        for attempt in range(self.max_retries):
            try:
                r = self.session.get(f"{API}/{endpoint}", params=params, timeout=20)
            except requests.RequestException as exc:
                last = exc
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last = r.text[:200]
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 403 and "quota" in r.text.lower():
                raise QuotaExceeded("YouTube API daily quota exceeded; resume tomorrow (state is saved).")
            raise YouTubeError(f"{endpoint} -> HTTP {r.status_code}: {r.text[:300]}")
        raise YouTubeError(f"{endpoint} failed after {self.max_retries} attempts: {last}")

    def search(self, q: str, type_: str, region: str | None = None, page_token: str | None = None,
               published_after: str | None = None) -> dict:
        params = dict(part="snippet", q=q, type=type_, maxResults=50)
        if region:
            params["regionCode"] = region
        if page_token:
            params["pageToken"] = page_token
        if published_after:
            params["publishedAfter"] = published_after
        return self._get("search", **params)

    def channels(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("channels", part="snippet,statistics,contentDetails",
                             id=",".join(ids[i:i + 50]), maxResults=50)
            out.extend(data.get("items", []))
        return out

    def recent_video_ids(self, uploads_playlist: str, n: int) -> list[str]:
        data = self._get("playlistItems", part="contentDetails", playlistId=uploads_playlist, maxResults=min(n, 50))
        return [it["contentDetails"]["videoId"] for it in data.get("items", [])]

    def videos(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        return self._get("videos", part="snippet,statistics", id=",".join(ids[:50])).get("items", [])
