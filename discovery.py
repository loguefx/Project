"""
Discovery engine — parses the SceneTime RSS feed, groups releases by show/movie,
and cross-references them against the NAS inventory so the UI can display what
you have, what you're missing, and what's brand new.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser

logger = logging.getLogger(__name__)

from runtime_paths import DATA_DIR
BASE_DIR        = DATA_DIR
DISCOVERY_CACHE = BASE_DIR / "discovery_cache.json"

# ---------------------------------------------------------------------------
# Release-name parsers
# ---------------------------------------------------------------------------

_TV_RE    = re.compile(r"^(.+?)[. _]S(\d{1,2})E(\d{1,2})", re.IGNORECASE)
_MOVIE_RE = re.compile(r"^(.+?)[. _]((?:19|20)\d{2})[. _]")


def parse_release_name(title: str) -> Optional[dict]:
    """
    Parse a torrent/release title into structured metadata.

    Returns one of:
      {"type":"tv",    "name":"The Bear", "season":3, "episode":5, "raw":title}
      {"type":"movie", "name":"Dune Part Two", "year":2024,         "raw":title}
      None  — if the title doesn't match either pattern
    """
    m = _TV_RE.match(title)
    if m:
        return {
            "type":    "tv",
            "name":    _clean_name(m.group(1)),
            "season":  int(m.group(2)),
            "episode": int(m.group(3)),
            "raw":     title,
        }
    m = _MOVIE_RE.search(title)
    if m:
        year = int(m.group(2))
        if 1950 < year < 2100:
            return {
                "type":  "movie",
                "name":  _clean_name(m.group(1)),
                "year":  year,
                "raw":   title,
            }
    return None


def _clean_name(s: str) -> str:
    return s.replace(".", " ").replace("_", " ").strip()


# ---------------------------------------------------------------------------
# Discovery engine
# ---------------------------------------------------------------------------

class DiscoveryEngine:
    def __init__(self, rss_url: str):
        self.rss_url = rss_url

    # ── RSS fetch ──────────────────────────────────────────────────────────

    def fetch(self) -> list[dict]:
        """Fetch the RSS feed and return a list of parsed release dicts."""
        try:
            feed = feedparser.parse(self.rss_url)
            items: list[dict] = []
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                parsed = parse_release_name(title)
                if not parsed:
                    continue
                parsed["link"]     = entry.get("link", "")
                parsed["pub_date"] = entry.get("published", "")
                # Enclosure may carry file size
                for enc in entry.get("enclosures", []):
                    try:
                        parsed["size_bytes"] = int(enc.get("length", 0))
                    except (ValueError, TypeError):
                        pass
                    break
                items.append(parsed)
            logger.info(
                "[Discovery] RSS fetched: %d/%d items parsed",
                len(items), len(feed.entries),
            )
            return items
        except Exception as exc:
            logger.error("[Discovery] RSS fetch failed: %s", exc)
            return []

    # ── Grouping ───────────────────────────────────────────────────────────

    def group_by_title(self, items: list[dict]) -> dict:
        """
        Group a flat list of RSS items by show/movie name.

        Returns:
          {
            "The Bear": {
              "name": "The Bear",
              "type": "tv",
              "releases": [...raw items...],
              "seasons": {3: [5, 6]},     # season → sorted episode list
            },
            "Dune Part Two": {
              "name": "Dune Part Two",
              "type": "movie",
              "year": 2024,
              "releases": [...],
            },
          }
        """
        groups: dict = {}
        for item in items:
            name = item["name"]
            if name not in groups:
                groups[name] = {
                    "name":     name,
                    "type":     item["type"],
                    "releases": [],
                    "seasons":  {},
                    "year":     item.get("year"),
                }
            groups[name]["releases"].append(item)
            if item["type"] == "tv":
                sn = item["season"]
                ep = item["episode"]
                groups[name]["seasons"].setdefault(sn, set()).add(ep)

        for g in groups.values():
            g["seasons"] = {sn: sorted(eps) for sn, eps in g["seasons"].items()}
        return groups

    # ── NAS cross-reference ────────────────────────────────────────────────

    def enrich_with_nas(
        self,
        groups: dict,
        tv_inventories: list[dict],
        movie_inventories: list[dict],
    ) -> list[dict]:
        """
        Merge RSS groups with disk inventory.

        Returns a list of enriched dicts, each containing:
          on_nas        bool
          disk_seasons  {sn: [eps]}   (TV only)
          missing_eps   [{"season":s,"episode":e}]
          status        "complete"|"missing"|"new"
        """
        from scanner import clean_show_name

        # Build lookup tables (lower-case keys)
        tv_disk: dict = {}
        for inv in tv_inventories:
            for raw, seasons in inv.items():
                key = clean_show_name(raw).lower()
                tv_disk[key] = {sn: set(eps) for sn, eps in seasons.items()}

        mov_disk: set = set()
        for inv in movie_inventories:
            for info in inv.values():
                mov_disk.add(info.get("title", "").lower())

        result: list[dict] = []

        for name, group in groups.items():
            enriched = dict(group)
            enriched["seasons"] = dict(group["seasons"])  # copy

            if group["type"] == "tv":
                key = name.lower()
                # Fuzzy match: "the bear" in "the bear [tmdb-174547]" etc.
                match_key = next(
                    (k for k in tv_disk if key == k or key in k or k in key), None
                )
                if match_key:
                    disk = tv_disk[match_key]
                    enriched["on_nas"] = True
                    enriched["disk_seasons"] = {
                        sn: sorted(eps) for sn, eps in disk.items()
                    }
                    missing = [
                        {"season": sn, "episode": ep}
                        for sn, rss_eps in group["seasons"].items()
                        for ep in rss_eps
                        if ep not in disk.get(sn, set())
                    ]
                    enriched["missing_eps"] = missing
                    enriched["status"] = "complete" if not missing else "missing"
                else:
                    enriched["on_nas"]      = False
                    enriched["disk_seasons"] = {}
                    enriched["missing_eps"] = [
                        {"season": sn, "episode": ep}
                        for sn, eps in group["seasons"].items()
                        for ep in eps
                    ]
                    enriched["status"] = "new"

            else:  # movie
                key = name.lower()
                enriched["on_nas"]       = any(key == k or key in k or k in key for k in mov_disk)
                enriched["disk_seasons"] = {}
                enriched["missing_eps"]  = []
                enriched["status"]       = "complete" if enriched["on_nas"] else "new"

            result.append(enriched)

        # Sort: new first → missing → complete; then alphabetically
        _order = {"new": 0, "missing": 1, "complete": 2}
        result.sort(key=lambda x: (_order.get(x["status"], 9), x["name"].lower()))
        return result

    # ── Cache ──────────────────────────────────────────────────────────────

    def save_cache(self, enriched: list[dict]) -> None:
        DISCOVERY_CACHE.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "items": enriched,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    def load_cache(self) -> dict:
        if DISCOVERY_CACHE.exists():
            try:
                return json.loads(DISCOVERY_CACHE.read_text("utf-8"))
            except Exception:
                pass
        return {"fetched_at": None, "items": []}
