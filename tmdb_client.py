"""
TMDB API wrapper.
API key is optional — falls back to web scraping for movie TMDB IDs when no key is set.
"""

import json
import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_WEB  = "https://www.themoviedb.org"
_CACHE_TTL = 3600  # seconds

_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class TMDBClient:
    def __init__(self, api_key: str = ""):
        self._api_key = api_key
        self._session = requests.Session()
        self._cache: dict = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        cache_key = path + str(sorted((params or {}).items()))
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
            return cached["data"]

        url = TMDB_BASE + path
        p = {"api_key": self._api_key, **(params or {})}
        try:
            resp = self._session.get(url, params=p, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            self._cache[cache_key] = {"data": data, "ts": time.time()}
            return data
        except requests.RequestException as exc:
            logger.error("[TMDB] Request failed for %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # TV
    # ------------------------------------------------------------------

    def search_tv(self, show_name: str) -> Optional[dict]:
        """
        Search for a TV show by name.
        Returns the best-matching result dict (id, name, first_air_date, …) or None.
        """
        data = self._get("/search/tv", {"query": show_name})
        if not data or not data.get("results"):
            logger.warning("[TMDB] No TV results for %r", show_name)
            return None
        # Return the result with the highest popularity
        results = data["results"]
        results.sort(key=lambda r: r.get("popularity", 0), reverse=True)
        match = results[0]
        logger.debug("[TMDB] TV search %r → %s (id=%s)", show_name, match.get("name"), match.get("id"))
        return match

    def get_show_details(self, show_id: int) -> Optional[dict]:
        """Return full show details including seasons list."""
        return self._get(f"/tv/{show_id}")

    def get_season_episodes(self, show_id: int, season_num: int) -> list[dict]:
        """
        Return a list of episode dicts for the given season.
        Each dict has: episode_number, name, air_date, id.
        """
        data = self._get(f"/tv/{show_id}/season/{season_num}")
        if not data:
            return []
        episodes = data.get("episodes", [])
        # Only return episodes that have already aired
        today = time.strftime("%Y-%m-%d")
        aired = [e for e in episodes if e.get("air_date") and e["air_date"] <= today]
        return aired

    def get_all_episodes(self, show_id: int) -> dict[int, list[dict]]:
        """
        Return {season_number: [episode_dicts]} for all aired seasons.
        Skips season 0 (specials).
        """
        details = self.get_show_details(show_id)
        if not details:
            return {}
        result: dict[int, list[dict]] = {}
        for season_info in details.get("seasons", []):
            sn = season_info.get("season_number", 0)
            if sn == 0:
                continue
            episodes = self.get_season_episodes(show_id, sn)
            if episodes:
                result[sn] = episodes
        return result

    # ------------------------------------------------------------------
    # Movies
    # ------------------------------------------------------------------

    def search_movie(self, title: str, year: Optional[int] = None) -> Optional[dict]:
        """
        Search for a movie by title (and optional year).
        Returns best-match result dict or None.
        """
        params: dict = {"query": title}
        if year:
            params["year"] = year
        data = self._get("/search/movie", params)
        if not data or not data.get("results"):
            # Retry without year constraint
            if year:
                data = self._get("/search/movie", {"query": title})
            if not data or not data.get("results"):
                logger.warning("[TMDB] No movie results for %r (%s)", title, year)
                return None
        results = data["results"]
        results.sort(key=lambda r: r.get("popularity", 0), reverse=True)
        match = results[0]
        logger.debug("[TMDB] Movie search %r → %s (id=%s)", title, match.get("title"), match.get("id"))
        return match

    def get_movie_details(self, movie_id: int) -> Optional[dict]:
        """Return full movie details including external_ids (IMDB ID)."""
        data = self._get(f"/movie/{movie_id}", {"append_to_response": "external_ids"})
        return data

    def get_movie_imdb_id(self, movie_id: int) -> Optional[str]:
        """Return the IMDB ID string (e.g. 'tt0468569') for a movie."""
        data = self.get_movie_details(movie_id)
        if not data:
            return None
        # Try top-level imdb_id first, then external_ids
        imdb_id = data.get("imdb_id") or (data.get("external_ids") or {}).get("imdb_id")
        return imdb_id or None

    def get_full_movie_info(self, title: str, year: Optional[int] = None) -> Optional[dict]:
        """
        Convenience: search + fetch details.
        Returns {title, year, tmdb_id, poster_url} or None.
        Uses API if key is set, otherwise falls back to web scraping.
        """
        if self.has_key:
            result = self.search_movie(title, year)
            if not result:
                return None
            tmdb_id = result["id"]
            details = self.get_movie_details(tmdb_id)
            if not details:
                return None
            pp = details.get("poster_path") or ""
            poster = f"https://image.tmdb.org/t/p/w300{pp}" if pp else ""
            return {
                "title": details.get("title", title),
                "year": year or (details.get("release_date") or "")[:4] or None,
                "tmdb_id": tmdb_id,
                "poster_url": poster,
            }
        else:
            return self.scrape_movie_info(title, year)

    @property
    def has_key(self) -> bool:
        return bool(self._api_key) and "your_tmdb" not in self._api_key

    # ------------------------------------------------------------------
    # No-key fallback: scrape TMDB website
    # ------------------------------------------------------------------

    def scrape_movie_info(self, title: str, year: Optional[int] = None) -> Optional[dict]:
        """
        Scrape themoviedb.org to get a movie's TMDB ID without an API key.
        Returns {title, year, tmdb_id, poster_url} or None.
        """
        query = f"{title} {year}" if year else title
        try:
            session = requests.Session()
            resp = session.get(
                f"{TMDB_WEB}/search/movie",
                params={"query": query, "language": "en-US"},
                headers=_SCRAPE_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("[TMDB scrape] Request failed: %s", exc)
            return None

        # TMDB embeds results in a <script id="__NEXT_DATA__"> JSON blob
        tmdb_id, poster_url, found_title, found_year = self._parse_next_data_movie(
            resp.text, title, year
        )

        # Fallback: parse <a href="/movie/XXXX"> links from HTML
        if not tmdb_id:
            tmdb_id, found_title = self._parse_html_movie_link(resp.text, title)

        if not tmdb_id:
            logger.warning("[TMDB scrape] No movie found for %r", query)
            return None

        logger.info("[TMDB scrape] %r → tmdb_id=%s", title, tmdb_id)
        return {
            "title": found_title or title,
            "year": found_year or year,
            "tmdb_id": tmdb_id,
            "poster_url": poster_url or "",
        }

    def _parse_next_data_movie(
        self, html: str, title: str, year: Optional[int]
    ) -> tuple[Optional[int], str, str, Optional[int]]:
        """Extract movie info from TMDB's __NEXT_DATA__ JSON blob."""
        m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not m:
            return None, "", "", None
        try:
            data = json.loads(m.group(1))
            # Path varies by TMDB frontend version — try a few
            results = (
                data.get("props", {}).get("pageProps", {}).get("results")
                or data.get("props", {}).get("pageProps", {}).get("searchResults", {}).get("movie", {}).get("results")
                or []
            )
            if not results:
                return None, "", "", None

            # Filter by year if provided
            candidates = results
            if year:
                year_match = [
                    r for r in results
                    if str(year) in (r.get("release_date") or r.get("releaseDate") or "")
                ]
                if year_match:
                    candidates = year_match

            best = candidates[0]
            tmdb_id = best.get("id")
            found_title = best.get("title") or best.get("original_title") or ""
            rel = best.get("release_date") or best.get("releaseDate") or ""
            found_year = int(rel[:4]) if rel and len(rel) >= 4 else None
            pp = best.get("poster_path") or best.get("posterPath") or ""
            poster = f"https://image.tmdb.org/t/p/w300{pp}" if pp else ""
            return int(tmdb_id) if tmdb_id else None, poster, found_title, found_year
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return None, "", "", None

    def _parse_html_movie_link(self, html: str, title: str) -> tuple[Optional[int], str]:
        """Fallback: find /movie/XXXXX links in raw HTML."""
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=re.compile(r"^/movie/\d+")):
            m = re.match(r"^/movie/(\d+)", a["href"])
            if m:
                found_title = a.get_text(strip=True) or title
                return int(m.group(1)), found_title
        return None, ""
