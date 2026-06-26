import logging
import time
from typing import Optional
from urllib.parse import urljoin, quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

from sources.base_source import BaseSource, TorrentResult

logger = logging.getLogger(__name__)

# SceneTime category IDs (adjust if the site changes)
# 2 = Movies, 43 = TV Shows (common SceneTime categories)
TV_CATS = ["43", "2", "9", "28", "7"]
MOVIE_CATS = ["2", "1", "17", "11"]
ALL_CATS = list(dict.fromkeys(TV_CATS + MOVIE_CATS))


class SceneTimeSource(BaseSource):
    """SceneTime private tracker — login, search, and RSS."""

    # Candidates tried in order — auto-detection fills this in at runtime
    _LOGIN_CANDIDATES = [
        "/login.php",
        "/takelogin.php",
        "/account-login.php",
        "/user/login",
    ]
    SEARCH_PATH = "/browse.php"

    def __init__(self, config: dict):
        super().__init__(config)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        })
        self._last_login: float = 0.0
        self._login_url: Optional[str] = None   # resolved at first login
        self._rss_entries: list = []            # cached RSS entries for this run
        self._rss_fetched_at: float = 0.0      # timestamp of last RSS fetch
        self._RSS_TTL: float = 600.0            # re-fetch RSS at most every 10 minutes

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _discover_login_url(self) -> Optional[str]:
        """
        Fetch the site homepage and look for a login form action URL.
        Falls back to trying each candidate path with a HEAD request.
        """
        try:
            resp = self._session.get(self.base_url + "/", timeout=15)
            soup = BeautifulSoup(resp.text, "lxml")

            # Look for a <form> whose action contains "login"
            for form in soup.find_all("form"):
                action = form.get("action", "")
                if "login" in action.lower():
                    resolved = urljoin(self.base_url, action)
                    logger.info("[SceneTime] Login form found: %s", resolved)
                    return resolved

            # No form found — try candidate paths
            for path in self._LOGIN_CANDIDATES:
                try:
                    r = self._session.head(
                        self.base_url + path, timeout=8, allow_redirects=True
                    )
                    if r.status_code not in (404, 410):
                        url = self.base_url + path
                        logger.info("[SceneTime] Login candidate found: %s", url)
                        return url
                except requests.RequestException:
                    continue
        except requests.RequestException as exc:
            logger.warning("[SceneTime] Could not fetch homepage for form discovery: %s", exc)

        # Last resort — first candidate
        fallback = self.base_url + self._LOGIN_CANDIDATES[0]
        logger.warning("[SceneTime] Using fallback login URL: %s", fallback)
        return fallback

    def _build_login_payload(self) -> dict:
        """
        Fetch the login page, parse the actual <form> field names,
        and build the POST payload using those exact names.
        Falls back to common defaults if parsing fails.
        """
        login_page_url = self._login_url
        # Try the dedicated login page first — it has all hidden fields (CSRF tokens etc.)
        candidate_pages = [
            self.base_url + "/login.php",
            self.base_url + "/",
            login_page_url,
        ]
        for page_url in candidate_pages:
            try:
                r = self._session.get(page_url, timeout=15)
                soup = BeautifulSoup(r.text, "lxml")
                # Find the login form — look for a form with a password field
                for form in soup.find_all("form"):
                    if form.find("input", {"type": "password"}):
                        payload = {}
                        for inp in form.find_all("input"):
                            name = inp.get("name")
                            val  = inp.get("value", "")
                            if not name:
                                continue
                            itype = inp.get("type", "text").lower()
                            if itype == "password":
                                payload[name] = self.password
                            elif itype in ("text", "email", "hidden"):
                                # Fill in username for the text field most likely to be username
                                if any(k in name.lower() for k in ("user", "name", "login", "email")):
                                    payload[name] = self.username
                                else:
                                    payload[name] = val  # keep default hidden field value
                            elif itype == "submit":
                                payload[name] = val or "Login"
                        if payload:
                            logger.debug("[SceneTime] Login payload fields: %s", list(payload.keys()))
                            return payload
            except requests.RequestException:
                continue

        # Fallback defaults
        logger.warning("[SceneTime] Could not parse login form — using default field names")
        return {
            "username": self.username,
            "password": self.password,
            "login":    "submit",
        }

    def login(self, debug: bool = False) -> bool:
        if not self._login_url:
            self._login_url = self._discover_login_url()

        payload = self._build_login_payload()
        redacted = {k: ("***" if "pass" in k.lower() else v) for k, v in payload.items()}
        logger.info("[SceneTime] Posting to %s", self._login_url)
        logger.info("[SceneTime] Payload fields: %s", redacted)
        try:
            resp = self._session.post(
                self._login_url, data=payload, timeout=30, allow_redirects=True
            )
            if debug:
                print(f"\n[DEBUG] Final URL : {resp.url}")
                print(f"[DEBUG] Status    : {resp.status_code}")
                print(f"[DEBUG] Cookies   : {dict(self._session.cookies)}")
                print(f"[DEBUG] Response snippet:\n{resp.text[:1500]}\n")
            resp.raise_for_status()

            text_lower = resp.text.lower()
            final_url = resp.url.lower()

            logger.debug("[SceneTime] Post-login URL: %s", resp.url)

            # Primary check: did we land somewhere other than a login page?
            # takelogin*.php always redirects on success — if we're not on a login/error URL, we're in
            login_url_patterns = ("login", "takelogin", "signin")
            still_on_login = any(p in final_url for p in login_url_patterns)

            if not still_on_login and self._session.cookies:
                self._session_valid = True
                self._last_login = time.time()
                logger.info("[SceneTime] Login successful (redirected to %s)", resp.url)
                return True

            # Secondary: look for explicit success text even if URL didn't change
            success_phrases = ["logout", "my account", "welcome back", "logged in"]
            if any(p in text_lower for p in success_phrases):
                self._session_valid = True
                self._last_login = time.time()
                logger.info("[SceneTime] Login successful (success text found)")
                return True

            # Explicit failure phrases — check full phrases not single words
            fail_phrases = [
                "invalid username or password",
                "incorrect username or password",
                "login failed",
                "wrong password",
                "your account has been banned",
                "account is banned",
            ]
            for phrase in fail_phrases:
                if phrase in text_lower:
                    logger.error("[SceneTime] Login failed — %r found in response", phrase)
                    return False

            # Still on login page = likely bad credentials
            if still_on_login:
                logger.error(
                    "[SceneTime] Login failed — redirected back to %s. "
                    "Double-check username and password in config.json.",
                    resp.url,
                )
                return False

            # Has cookies but nothing definitive — assume success
            if self._session.cookies:
                self._session_valid = True
                self._last_login = time.time()
                logger.info("[SceneTime] Login assumed successful (cookies present)")
                return True

            logger.error("[SceneTime] Login failed — no cookies and no success signal")
            return False

        except requests.RequestException as exc:
            logger.error("[SceneTime] Login request failed: %s", exc)
            self._session_valid = False
            return False

    def _check_session(self) -> bool:
        """Re-login if session is older than 6 hours."""
        if not self._session_valid or (time.time() - self._last_login) > 21600:
            self._session_valid = False
            return self.login()
        return True

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[TorrentResult]:
        if not self._check_session():
            logger.warning("[SceneTime] Skipping search — not logged in")
            return []
        results = self._search_browse(query)
        if not results and self.rss_feed:
            results = self._search_rss(query)
        return results

    def _search_browse(self, query: str) -> list[TorrentResult]:
        self._polite_sleep(4.0)   # 4s between searches — avoids 429 rate limit
        params: dict = {"search": query, "do": "search"}
        # Add all categories
        for cat in ALL_CATS:
            params.setdefault("c[]", [])
            if isinstance(params["c[]"], list):
                params["c[]"].append(cat)

        url = self.base_url + self.SEARCH_PATH
        try:
            resp = self._session.get(url, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("[SceneTime] Search request failed: %s", exc)
            return []

        # Session may have expired — re-auth and retry once
        if "login" in resp.url.lower() or "takelogin" in resp.url.lower():
            logger.info("[SceneTime] Session expired during search, re-authenticating…")
            self._session_valid = False
            if not self.login():
                return []
            try:
                resp = self._session.get(url, params=params, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.error("[SceneTime] Retry search failed: %s", exc)
                return []

        return self._parse_results(resp.text)

    def _parse_results(self, html: str) -> list[TorrentResult]:
        soup = BeautifulSoup(html, "lxml")
        results: list[TorrentResult] = []

        # SceneTime results are in a table — find rows with torrent links
        table = soup.find("table", id="browse-list") or soup.find("table", class_="browse")
        if table is None:
            # Fall back: look for any table that has torrent download links
            for tbl in soup.find_all("table"):
                if tbl.find("a", href=lambda h: h and "download" in h):
                    table = tbl
                    break

        if table is None:
            logger.debug("[SceneTime] No results table found in HTML")
            return results

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            try:
                result = self._parse_row(row, cells)
                if result:
                    results.append(result)
            except Exception as exc:
                logger.debug("[SceneTime] Failed to parse row: %s", exc)

        logger.info("[SceneTime] Found %d results for browse search", len(results))
        return results

    def _parse_row(self, row, cells) -> Optional[TorrentResult]:
        # Look for the torrent name link — usually in a <td> with class "name" or similar
        name_link = (
            row.find("a", class_="torrent-name")
            or row.find("a", href=lambda h: h and "details" in h)
            or row.find("a", href=lambda h: h and "id=" in h)
        )
        if not name_link:
            return None

        name = name_link.get_text(strip=True)
        if not name:
            return None

        # Download link (torrent file) or magnet
        dl_link = row.find("a", href=lambda h: h and ("download" in h or "magnet:" in h))
        torrent_url: Optional[str] = None
        magnet: Optional[str] = None

        if dl_link:
            href = dl_link["href"]
            if href.startswith("magnet:"):
                magnet = href
            else:
                torrent_url = urljoin(self.base_url, href)

        # Info URL
        info_url: Optional[str] = None
        detail_href = name_link.get("href", "")
        if detail_href:
            info_url = urljoin(self.base_url, detail_href)

        # Size — look for a cell that contains GB/MB
        size_gb: Optional[float] = None
        for cell in cells:
            text = cell.get_text(strip=True)
            if any(unit in text.upper() for unit in ["GB", "MB", "GIB", "MIB", "TB"]):
                size_gb = self.parse_size_to_gb(text)
                if size_gb is not None:
                    break

        # Seeders / leechers — typically numeric cells near the end
        seeders = 0
        leechers = 0
        numeric_cells = []
        for cell in cells:
            text = cell.get_text(strip=True)
            if text.isdigit():
                numeric_cells.append(int(text))
        if len(numeric_cells) >= 2:
            # Convention: seeds before leeches
            seeders = numeric_cells[-2]
            leechers = numeric_cells[-1]
        elif len(numeric_cells) == 1:
            seeders = numeric_cells[0]

        return TorrentResult(
            name=name,
            magnet=magnet,
            torrent_url=torrent_url,
            size_gb=size_gb,
            seeders=seeders,
            leechers=leechers,
            source_name=self.name,
            info_url=info_url,
        )

    # ------------------------------------------------------------------
    # RSS feed
    # ------------------------------------------------------------------

    def _ensure_rss_cache(self) -> bool:
        """Fetch (or refresh) the RSS feed entries into self._rss_entries.
        Returns True if entries are available."""
        if not self.rss_feed:
            return False
        now = time.time()
        if self._rss_entries and (now - self._rss_fetched_at) < self._RSS_TTL:
            return True  # still fresh
        try:
            resp = self._session.get(self.rss_feed, timeout=20)
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
            self._rss_entries = list(feed.entries)
            self._rss_fetched_at = now
            logger.info("[SceneTime] RSS cache refreshed — %d entries", len(self._rss_entries))
            return True
        except Exception as exc:
            logger.error("[SceneTime] RSS fetch/parse error: %s", exc)
            return False

    def _search_rss(self, query: str) -> list[TorrentResult]:
        if not self._ensure_rss_cache():
            return []

        query_lower = query.lower()
        results: list[TorrentResult] = []
        for entry in self._rss_entries:
            title = entry.get("title", "")
            if not any(word in title.lower() for word in query_lower.split()):
                continue
            magnet = None
            torrent_url = None
            link = entry.get("link", "") or entry.get("enclosure", {}).get("url", "")
            if link.startswith("magnet:"):
                magnet = link
            elif link:
                torrent_url = link
            results.append(
                TorrentResult(
                    name=title,
                    magnet=magnet,
                    torrent_url=torrent_url,
                    source_name=self.name,
                )
            )

        logger.info("[SceneTime] RSS matched %d entries for query %r", len(results), query)
        return results

    # ------------------------------------------------------------------
    # Cookie passthrough for .torrent file downloads
    # ------------------------------------------------------------------

    def get_download_headers(self) -> dict:
        """Return Cookie header so qBit can download .torrent files directly."""
        cookie_str = "; ".join(
            f"{c.name}={c.value}" for c in self._session.cookies
        )
        return {"Cookie": cookie_str} if cookie_str else {}

    # ------------------------------------------------------------------
    # Rate limiting helper
    # ------------------------------------------------------------------

    def _polite_sleep(self, seconds: float = 2.0) -> None:
        """Brief pause between requests — avoids hammering SceneTime."""
        time.sleep(seconds)
