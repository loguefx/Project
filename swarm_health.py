"""
swarm_health.py — live BitTorrent swarm health check.

Indexers report a seeder count captured whenever the release was *indexed*,
which can be hours or days stale. A release listed with "50 seeders" is
frequently dead by the time we try to download it — that's the main cause of
the "no working release after trying every available torrent" failures.

This module asks the trackers themselves, right now, how many seeders a
torrent actually has, by SCRAPING them:

  * UDP trackers  — BEP 15 (connect → scrape)
  * HTTP trackers — BEP 48 (GET /scrape?info_hash=...)

It works off a magnet URI (infohash + tracker list). Private-tracker results
that ship only a .torrent file (no magnet) can't be scraped anonymously, so
those return ``checked=False`` and the caller falls back to the indexer count.

The check is best-effort and fail-open: if every tracker times out we report
``checked=False`` rather than falsely declaring a swarm dead.
"""

from __future__ import annotations

import base64
import logging
import random
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

import requests

logger = logging.getLogger(__name__)


# Well-seeded, scrape-capable open trackers. We scrape these IN ADDITION to a
# magnet's own trackers so that even a magnet carrying few/dead trackers still
# gets an authoritative answer from a tracker the swarm is very likely on.
DEFAULT_PUBLIC_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://opentracker.i2p.rocks:6969/announce",
    "udp://tracker.dler.org:6969/announce",
]

_UDP_PROTOCOL_ID = 0x41727101980  # magic constant from BEP 15


@dataclass
class SwarmHealth:
    """Result of a live swarm scrape.

    checked:  True only if at least one tracker actually answered. When False
              the caller MUST fall back to the indexer's reported count instead
              of treating the torrent as dead.
    seeders:  Highest seeder count seen across all responding trackers.
    leechers: Leechers reported alongside that best seeder count.
    """
    checked: bool = False
    seeders: int = 0
    leechers: int = 0


def parse_magnet(magnet: str) -> tuple[Optional[bytes], list[str]]:
    """Return (20-byte raw infohash, [tracker announce URLs]) from a magnet URI.

    Handles both 40-char hex and 32-char base32 btih forms. Returns
    (None, []) if no valid infohash is present.
    """
    if not magnet or not magnet.startswith("magnet:"):
        return None, []
    try:
        qs = parse_qs(urlsplit(magnet).query)
    except ValueError:
        return None, []

    infohash: Optional[bytes] = None
    for xt in qs.get("xt", []):
        if not xt.lower().startswith("urn:btih:"):
            continue
        val = xt.split(":", 2)[-1]
        if len(val) == 40:
            try:
                infohash = bytes.fromhex(val)
            except ValueError:
                infohash = None
        elif len(val) == 32:
            try:
                infohash = base64.b32decode(val.upper())
            except Exception:
                infohash = None
        if infohash and len(infohash) == 20:
            break
        infohash = None

    trackers = [unquote(t) for t in qs.get("tr", []) if t]
    return infohash, trackers


# ---------------------------------------------------------------------------
# Minimal bencode decoder (only what an HTTP scrape response needs)
# ---------------------------------------------------------------------------

def _bdecode(data: bytes, i: int = 0):
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            key, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out[key] = v
        return out, i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        length = int(data[i:colon])
        start = colon + 1
        return data[start:start + length], start + length
    raise ValueError(f"bad bencode at {i}")


# ---------------------------------------------------------------------------
# Per-protocol scrapers
# ---------------------------------------------------------------------------

def _scrape_udp(announce: str, infohash: bytes, timeout: float) -> Optional[tuple[int, int]]:
    """UDP tracker scrape (BEP 15). Returns (seeders, leechers) or None."""
    parsed = urlparse(announce)
    host, port = parsed.hostname, parsed.port or 80
    if not host:
        return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        addr = (host, port)

        # ── connect ──────────────────────────────────────────────────────
        txid = random.randint(0, 0xFFFFFFFF)
        req = struct.pack(">QII", _UDP_PROTOCOL_ID, 0, txid)
        sock.sendto(req, addr)
        resp = sock.recv(16)
        if len(resp) < 16:
            return None
        action, r_txid, conn_id = struct.unpack(">IIQ", resp)
        if action != 0 or r_txid != txid:
            return None

        # ── scrape ───────────────────────────────────────────────────────
        txid = random.randint(0, 0xFFFFFFFF)
        req = struct.pack(">QII", conn_id, 2, txid) + infohash
        sock.sendto(req, addr)
        resp = sock.recv(8 + 12)
        if len(resp) < 8 + 12:
            return None
        action, r_txid = struct.unpack(">II", resp[:8])
        if action != 2 or r_txid != txid:
            return None
        seeders, _completed, leechers = struct.unpack(">III", resp[8:20])
        return seeders, leechers
    except (socket.timeout, OSError, struct.error):
        return None
    finally:
        sock.close()


def _scrape_http(announce: str, infohash: bytes, timeout: float) -> Optional[tuple[int, int]]:
    """HTTP(S) tracker scrape (BEP 48). Returns (seeders, leechers) or None."""
    # Scrape URL = announce URL with the final "announce" path segment replaced
    # by "scrape". Only trackers that publish /announce support scraping.
    parsed = urlparse(announce)
    path = parsed.path
    if "announce" not in path.rsplit("/", 1)[-1]:
        return None
    scrape_path = path.rsplit("/", 1)
    scrape_path[-1] = scrape_path[-1].replace("announce", "scrape")
    scrape_url = parsed._replace(path="/".join(scrape_path)).geturl()

    info_hash_q = quote(infohash, safe="")
    try:
        r = requests.get(
            f"{scrape_url}?info_hash={info_hash_q}",
            timeout=timeout,
            headers={"User-Agent": "ShowTVDownloader/1.0"},
        )
        if r.status_code != 200 or not r.content:
            return None
        decoded, _ = _bdecode(r.content)
        if not isinstance(decoded, dict):
            return None
        files = decoded.get(b"files")
        if not isinstance(files, dict) or not files:
            return None
        # Single-hash scrape — take the first (only) file entry.
        stats = next(iter(files.values()))
        if not isinstance(stats, dict):
            return None
        seeders = int(stats.get(b"complete", 0) or 0)
        leechers = int(stats.get(b"incomplete", 0) or 0)
        return seeders, leechers
    except (requests.RequestException, ValueError, StopIteration):
        return None


class SwarmHealthChecker:
    """Scrapes trackers to find a torrent's *current* seeder count.

    A short-lived in-memory cache (keyed by infohash) avoids re-scraping the
    same release multiple times within a run/retry loop.
    """

    def __init__(
        self,
        timeout: float = 5.0,
        max_trackers: int = 10,
        cache_ttl: float = 600.0,
        extra_trackers: Optional[list[str]] = None,
    ):
        self._timeout = timeout
        self._max_trackers = max_trackers
        self._cache_ttl = cache_ttl
        self._extra = list(extra_trackers if extra_trackers is not None else DEFAULT_PUBLIC_TRACKERS)
        self._cache: dict[bytes, tuple[float, SwarmHealth]] = {}

    def check(self, magnet: str, stop_at: Optional[int] = None) -> SwarmHealth:
        """Scrape the magnet's trackers (plus public fallbacks) for live peers.

        stop_at: short-circuit as soon as a tracker reports at least this many
        seeders (we already know the swarm is healthy enough — no need to wait
        for slower trackers).
        """
        infohash, magnet_trackers = parse_magnet(magnet)
        if not infohash:
            return SwarmHealth(checked=False)

        cached = self._cache.get(infohash)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]

        # De-dupe magnet trackers + public fallbacks, preserving order.
        seen: set[str] = set()
        trackers: list[str] = []
        for t in list(magnet_trackers) + self._extra:
            key = t.strip().lower()
            if key and key not in seen:
                seen.add(key)
                trackers.append(t)
        trackers = trackers[: self._max_trackers]
        if not trackers:
            return SwarmHealth(checked=False)

        def _scrape_one(announce: str) -> Optional[tuple[int, int]]:
            scheme = urlparse(announce).scheme.lower()
            if scheme == "udp":
                return _scrape_udp(announce, infohash, self._timeout)
            if scheme in ("http", "https"):
                return _scrape_http(announce, infohash, self._timeout)
            return None

        best_seeders = -1
        best_leechers = 0
        answered = False
        with ThreadPoolExecutor(max_workers=min(len(trackers), 10)) as ex:
            futures = {ex.submit(_scrape_one, t): t for t in trackers}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res is None:
                    continue
                answered = True
                seeders, leechers = res
                if seeders > best_seeders:
                    best_seeders, best_leechers = seeders, leechers
                if stop_at is not None and best_seeders >= stop_at:
                    break

        if not answered:
            result = SwarmHealth(checked=False)
        else:
            result = SwarmHealth(checked=True, seeders=max(best_seeders, 0), leechers=best_leechers)
            self._cache[infohash] = (time.time(), result)
        return result
