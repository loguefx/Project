"""
TVMaze API client — completely free, no API key required.
Provides TV show episode data, TMDB IDs, TVDB IDs, and poster URLs.

API docs: https://www.tvmaze.com/api
Rate limit: 20 requests / 10 seconds (no key needed)
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TVMAZE_BASE = "https://api.tvmaze.com"
_MEM_TTL    = 3600       # in-process cache: 1 hour
_DISK_TTL   = 86400 * 3  # disk cache: 3 days for show/episode data

_DISK_CACHE_FILE = Path(__file__).parent / "tvmaze_cache.json"


def _load_disk_cache() -> dict:
    try:
        if _DISK_CACHE_FILE.exists():
            return json.loads(_DISK_CACHE_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def _save_disk_cache(cache: dict) -> None:
    try:
        _DISK_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    except Exception as exc:
        logger.debug("[TVMaze] Could not persist disk cache: %s", exc)


class TVMazeClient:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "JellyfinDownloader/1.0 (https://github.com/local)"
        })
        self._cache: dict = {}          # in-process memory cache
        self._disk: dict = _load_disk_cache()   # persistent disk cache
        self._disk_dirty: bool = False

    def flush(self) -> None:
        """Write dirty disk cache entries to disk."""
        if self._disk_dirty:
            _save_disk_cache(self._disk)
            self._disk_dirty = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None):
        cache_key = path + str(sorted((params or {}).items()))
        now = time.time()

        # 1. In-process memory cache (hot, short TTL)
        cached = self._cache.get(cache_key)
        if cached and (now - cached["ts"]) < _MEM_TTL:
            return cached["data"]

        # 2. Disk cache (cold start, longer TTL)
        disk_entry = self._disk.get(cache_key)
        if disk_entry and (now - disk_entry["ts"]) < _DISK_TTL:
            self._cache[cache_key] = disk_entry  # warm the memory cache
            return disk_entry["data"]

        url = TVMAZE_BASE + path
        try:
            resp = self._session.get(url, params=params, timeout=15)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                # Rate limited — wait and retry once
                logger.warning("[TVMaze] Rate limited, waiting 12 seconds…")
                time.sleep(12)
                resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            entry = {"data": data, "ts": now}
            self._cache[cache_key] = entry
            self._disk[cache_key] = entry
            self._disk_dirty = True
            return data
        except requests.RequestException as exc:
            logger.error("[TVMaze] Request failed for %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # Show search
    # ------------------------------------------------------------------

    def search_show(self, name: str) -> Optional[dict]:
        """
        Find the best-matching show for a given name.
        Returns the full TVMaze show object or None.
        """
        # singlesearch returns the single best match
        data = self._get("/singlesearch/shows", {"q": name})
        if data:
            logger.debug("[TVMaze] Found show %r → %s (id=%s)", name, data.get("name"), data.get("id"))
            return data

        # Fall back to scored search and pick the top result
        results = self._get("/search/shows", {"q": name})
        if results:
            best = max(results, key=lambda r: r.get("score", 0))
            return best.get("show")

        logger.warning("[TVMaze] No show found for %r", name)
        return None

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    def get_all_episodes(self, show_name: str) -> Optional[dict]:
        """
        Returns a dict:
        {
            "show": { TVMaze show object },
            "episodes": { season_num: [ {episode_number, name, air_date}, ... ] }
        }
        or None if the show wasn't found.
        """
        show = self.search_show(show_name)
        if not show:
            return None

        raw_eps = self._get(f"/shows/{show['id']}/episodes")
        if not raw_eps:
            return {"show": show, "episodes": {}}

        today = time.strftime("%Y-%m-%d")
        by_season: dict[int, list[dict]] = {}

        for ep in raw_eps:
            season = ep.get("season", 0)
            ep_num = ep.get("number")
            if not season or not ep_num:
                continue
            airdate = ep.get("airdate") or ""
            if airdate and airdate > today:
                continue   # not aired yet
            by_season.setdefault(season, []).append({
                "episode_number": ep_num,
                "name":           ep.get("name") or "",
                "air_date":       airdate,
            })

        return {"show": show, "episodes": by_season}

    # ------------------------------------------------------------------
    # External IDs (TMDB / TVDB / IMDB)
    # ------------------------------------------------------------------

    def get_externals(self, show_name: str) -> dict:
        """
        Return the external IDs dict for a show:
        { "tmdb": 136315, "thetvdb": 361217, "imdb": "tt14266692" }
        """
        show = self.search_show(show_name)
        return (show or {}).get("externals") or {}

    def get_tmdb_id(self, show_name: str) -> Optional[int]:
        return self.get_externals(show_name).get("tmdb")

    def get_tvdb_id(self, show_name: str) -> Optional[int]:
        return self.get_externals(show_name).get("thetvdb")

    # ------------------------------------------------------------------
    # Poster
    # ------------------------------------------------------------------

    def get_poster_url(self, show_name: str) -> str:
        show = self.search_show(show_name)
        if not show:
            return ""
        image = show.get("image") or {}
        return image.get("original") or image.get("medium") or ""

    # ------------------------------------------------------------------
    # Connectivity check
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Return True if TVMaze API is reachable."""
        try:
            r = self._session.get(TVMAZE_BASE + "/shows/1", timeout=8)
            return r.ok
        except requests.RequestException:
            return False
