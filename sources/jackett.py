"""
Generic Jackett/Prowlarr proxy source.

Jackett and Prowlarr are self-hosted indexer aggregators that proxy searches
across many trackers via a unified API. Configure one of them to get results
from dozens of private/public trackers as a fallback when SceneTime has no hits.

Setup:
  1. Install Jackett (https://github.com/Jackett/Jackett) or
     Prowlarr (https://github.com/Prowlarr/Prowlarr)
  2. Add your trackers inside Jackett/Prowlarr
  3. Set config.json:
       {
         "name": "Jackett",
         "type": "jackett",
         "enabled": false,
         "url": "http://127.0.0.1:9117",
         "api_key": "your_jackett_api_key",
         "trackers": "all"   (or comma-separated: "scenetime,btn,mtv")
       }
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from .base_source import BaseSource, TorrentResult

logger = logging.getLogger(__name__)


class JackettSource(BaseSource):
    """Queries a local Jackett or Prowlarr instance as a fallback tracker."""

    # Jackett aggregates most of our indexers, so if it's down the search
    # results are badly degraded. Pause and wait for it to come back rather
    # than burning through the show list finding nothing.
    gate_when_down = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._api_key = config.get("api_key", "")
        self._trackers = config.get("trackers", "all")
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "ShowTVDownloader/1.0"
        # Health-probe tuning — a busy Jackett shouldn't be misread as offline.
        self._health_timeout = float(config.get("health_timeout_sec", 12))
        self._health_attempts = max(1, int(config.get("health_attempts", 3)))
        self._health_retry_gap = float(config.get("health_retry_gap_sec", 1.5))

    def health_check(self) -> bool:
        """Probe whether the Jackett/Prowlarr server is reachable.

        ANY HTTP response (even 401/404) means the server is up; only a
        connection error/timeout counts as down. A source with no API key is
        treated as 'healthy' so a config gap can't make the pipeline wait
        forever — login()/search() handle that case by no-op'ing instead.

        IMPORTANT: Jackett (Mono/.NET) is effectively single-threaded for the
        dashboard route and gets sluggish when several searches hit it at once,
        so a SINGLE short probe frequently times out even though the server is
        perfectly alive — which used to trip a spurious "Jackett offline" pause.
        We therefore give the probe a generous timeout and retry a couple of
        times before concluding it's actually down; a transient slow response no
        longer counts as an outage."""
        if not self._api_key:
            return True
        last_exc: Optional[Exception] = None
        for attempt in range(self._health_attempts):
            try:
                # Any response at all (even 4xx) proves the server is up.
                self._session.get(self.base_url, timeout=self._health_timeout)
                return True
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self._health_attempts - 1:
                    time.sleep(self._health_retry_gap)
        logger.warning(
            "[Jackett] health_check: no response after %d attempt(s) (%ds timeout each): %s",
            self._health_attempts, self._health_timeout, last_exc,
        )
        return False

    # ------------------------------------------------------------------
    # BaseSource interface
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """Jackett/Prowlarr use API keys — no session login needed."""
        if not self._api_key:
            logger.warning("[Jackett] No api_key configured")
            return False
        self._session_valid = True
        return True

    def search(self, query: str) -> list[TorrentResult]:
        if not self.ensure_logged_in():
            return []
        try:
            return self._torznab_search(query)
        except Exception as exc:
            logger.error("[Jackett] Search error: %s", exc)
            return []

    def get_download_headers(self) -> dict:
        return {}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _torznab_search(self, query: str) -> list[TorrentResult]:
        """Query the Torznab API exposed by Jackett/Prowlarr."""
        params = {
            "apikey": self._api_key,
            "t": "search",
            "q": query,
            "limit": 50,
        }
        # Prowlarr uses /api/v1/indexer/all/newznab, Jackett uses /api/v2.0/indexers/...
        url = f"{self.base_url}/api/v2.0/indexers/{self._trackers}/results/torznab/api"

        r = self._session.get(url, params=params, timeout=30)
        if r.status_code == 404:
            # Try Prowlarr endpoint
            url = f"{self.base_url}/api/v1/indexer/0/newznab"
            r = self._session.get(url, params=params, timeout=30)

        r.raise_for_status()

        from xml.etree import ElementTree as ET
        ns = {"torznab": "http://torznab.com/schemas/2015/feed"}
        root = ET.fromstring(r.text)
        results: list[TorrentResult] = []

        for item in root.findall(".//item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            size_text = item.findtext("size") or "0"
            try:
                size_bytes = int(size_text)
                size_gb = round(size_bytes / 1e9, 4)
            except ValueError:
                size_gb = None

            def _attr(name: str) -> str:
                el = item.find(f"torznab:attr[@name='{name}']", ns)
                return el.attrib.get("value", "") if el is not None else ""

            seeders = int(_attr("seeders") or 0)
            torrent_url = _attr("magneturl") or ""
            magnet = None
            if torrent_url.startswith("magnet:"):
                magnet = torrent_url
                torrent_url = ""
            if not torrent_url:
                torrent_url = link if link and not link.startswith("magnet:") else ""
            if not magnet and link.startswith("magnet:"):
                magnet = link

            results.append(TorrentResult(
                name=title,
                magnet=magnet or None,
                torrent_url=torrent_url or None,
                size_gb=size_gb,
                seeders=seeders,
                source_name=self.name,
            ))

        logger.info("[Jackett] Found %d results for %r", len(results), query)
        return results
