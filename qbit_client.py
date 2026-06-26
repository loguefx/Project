"""
qBittorrent Web API client.
Handles login, adding torrents (magnet or .torrent file URL), and checking status.

Authentication is skipped automatically when:
  - bypass_auth=True is passed to the constructor, OR
  - both username and password are empty strings

This matches qBittorrent's "Bypass authentication for clients on localhost" setting.
If a 403 is returned unexpectedly, the client will attempt a one-time login with
whatever credentials are configured before giving up.
"""

import base64
import hashlib
import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def _infohash_from_magnet(magnet: str) -> Optional[str]:
    """Extract the v1 BitTorrent infohash (40-char hex) from a magnet URI.
    Handles both hex (btih:<40 hex>) and base32 (btih:<32 base32>) forms.
    Returns lowercase hex, or None if not found."""
    m = re.search(r"xt=urn:btih:([A-Za-z0-9]+)", magnet or "")
    if not m:
        return None
    val = m.group(1)
    if len(val) == 40:
        return val.lower()
    if len(val) == 32:  # base32-encoded — decode to hex
        try:
            return base64.b32decode(val.upper()).hex()
        except Exception:
            return None
    return None


def _bdecode(data: bytes, i: int):
    """Minimal bencode decoder returning (value, next_index)."""
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


def _infohash_from_torrent(data: bytes) -> Optional[str]:
    """Compute the v1 infohash (SHA1 of the raw bencoded 'info' dict) from
    .torrent file bytes. Returns lowercase hex, or None on failure."""
    try:
        if data[0:1] != b"d":
            return None
        i = 1
        while data[i:i + 1] != b"e":
            key, i = _bdecode(data, i)
            val_start = i
            _, i = _bdecode(data, i)
            if key == b"info":
                return hashlib.sha1(data[val_start:i]).hexdigest()
        return None
    except Exception:
        return None


class QBitClient:
    def __init__(self, url: str, username: str = "", password: str = "", bypass_auth: bool = False):
        # On Windows, 'localhost' often resolves to ::1 (IPv6) instead of 127.0.0.1
        # qBittorrent's bypass only trusts 127.0.0.1, so force IPv4 loopback
        url = url.rstrip("/").replace("//localhost:", "//127.0.0.1:")
        self._base = url
        self._username = username
        self._password = password
        self._session = requests.Session()
        # qBittorrent 4.6+ enforces CSRF protection — Referer must match the Web UI origin
        self._session.headers.update({
            "Referer": self._base + "/",
            "Origin":  self._base,
        })
        # Skip auth if bypass is explicitly set, or no credentials provided
        self._bypass_auth: bool = bypass_auth or (not username and not password)
        self._logged_in: bool = self._bypass_auth  # treat bypass as already authenticated

        if self._bypass_auth:
            logger.info("[qBit] Auth bypass enabled — connecting to %s", self._base)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> bool:
        if self._bypass_auth:
            return True
        url = self._base + "/api/v2/auth/login"
        try:
            resp = self._session.post(
                url,
                data={"username": self._username, "password": self._password},
                timeout=15,
            )
            if resp.text.strip().lower() == "ok.":
                self._logged_in = True
                logger.info("[qBit] Login successful")
                return True
            logger.error("[qBit] Login failed: %s", resp.text[:200])
            return False
        except requests.RequestException as exc:
            logger.error("[qBit] Login request failed: %s", exc)
            return False

    def _ensure_logged_in(self) -> bool:
        if self._bypass_auth:
            return True
        if not self._logged_in:
            return self.login()
        return True

    def _handle_403(self) -> bool:
        """Called when an API response comes back 403. Try a one-shot login."""
        if self._bypass_auth:
            logger.warning("[qBit] Got 403 despite bypass — check qBittorrent settings")
            return False
        self._logged_in = False
        return self.login()

    # ------------------------------------------------------------------
    # Add torrents
    # ------------------------------------------------------------------

    def has_torrent(self, infohash: Optional[str]) -> bool:
        """Return True if a torrent with this v1 infohash is already in qBit.
        qBittorrent's add API returns the unhelpful string 'Fails.' for a
        duplicate infohash; this lets callers distinguish 'already present'
        (benign — the download exists/completed) from a real add failure."""
        if not infohash:
            return False
        try:
            ih = infohash.lower()
            for t in self.get_torrents():
                if (t.get("hash", "") or "").lower() == ih:
                    return True
        except Exception:
            pass
        return False

    def add_magnet(self, magnet: str, save_path: str, rename: Optional[str] = None) -> bool:
        """Add a magnet link to qBittorrent, saving to save_path."""
        if not self._ensure_logged_in():
            return False
        url = self._base + "/api/v2/torrents/add"
        data: dict = {
            "urls": magnet,
            "savepath": save_path,
            "autoTMM": "false",
        }
        if rename:
            data["rename"] = rename
        try:
            resp = self._session.post(url, data=data, timeout=15)
            if resp.text.strip().lower() == "ok.":
                logger.info("[qBit] Magnet queued → %s", save_path)
                return True
            if "forbidden" in resp.text.lower() or resp.status_code == 403:
                if self._handle_403():
                    return self.add_magnet(magnet, save_path, rename)
            # qBit returns "Fails." for a DUPLICATE infohash. That isn't a real
            # failure — the torrent is already present (often already complete),
            # so report success and let the watcher pick up the existing download
            # instead of falsely marking the episode as having no source.
            if self.has_torrent(_infohash_from_magnet(magnet)):
                logger.info("[qBit] Magnet already present (duplicate infohash) — treating as queued.")
                return True
            logger.error("[qBit] Failed to add magnet: %s", resp.text[:200])
            return False
        except requests.RequestException as exc:
            logger.error("[qBit] Add magnet failed: %s", exc)
            return False

    def add_torrent_url(self, torrent_url: str, save_path: str, rename: Optional[str] = None,
                        extra_headers: Optional[dict] = None) -> bool:
        """
        Download a .torrent file and add it to qBittorrent.
        extra_headers are sent when fetching the .torrent (e.g. session cookies).
        """
        if not self._ensure_logged_in():
            return False

        # A "torrent URL" may actually be (or redirect to) a magnet link —
        # common with Jackett's /dl/ proxy for magnet-only indexers (e.g. EZTV).
        # requests can't fetch a magnet: scheme, so follow redirects manually
        # and hand any magnet off to add_magnet instead of failing.
        if torrent_url.startswith("magnet:"):
            return self.add_magnet(torrent_url, save_path, rename)
        try:
            headers = extra_headers or {}
            current = torrent_url
            torrent_bytes = b""
            for _ in range(6):  # follow up to 6 redirects manually
                r = requests.get(current, headers=headers, timeout=30, allow_redirects=False)
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location", "")
                    if loc.startswith("magnet:"):
                        logger.info("[qBit] Torrent URL redirected to a magnet — adding via magnet")
                        return self.add_magnet(loc, save_path, rename)
                    if not loc:
                        logger.error("[qBit] Redirect with no Location from %s", current)
                        return False
                    current = loc
                    continue
                r.raise_for_status()
                torrent_bytes = r.content
                break
            else:
                logger.error("[qBit] Too many redirects fetching torrent file from %s", torrent_url)
                return False
        except requests.RequestException as exc:
            logger.error("[qBit] Failed to download torrent file: %s", exc)
            return False

        # Bencoded torrent files always start with 'd' (0x64).
        # If we got HTML instead (login redirect / rate-limit page) reject it.
        if not torrent_bytes or torrent_bytes[0:1] != b"d":
            snippet = torrent_bytes[:120].decode("utf-8", errors="replace")
            logger.error(
                "[qBit] Downloaded content is not a valid torrent file "
                "(got HTML/redirect instead of bencode). "
                "Session cookies may have expired. Snippet: %s",
                snippet,
            )
            return False
        logger.debug("[qBit] Torrent file OK (%d bytes)", len(torrent_bytes))

        url = self._base + "/api/v2/torrents/add"
        data: dict = {
            "savepath": save_path,
            "autoTMM": "false",
        }
        if rename:
            data["rename"] = rename
        files = {"torrents": ("file.torrent", torrent_bytes, "application/x-bittorrent")}
        try:
            resp = self._session.post(url, data=data, files=files, timeout=15)
            if resp.text.strip().lower() == "ok.":
                logger.info("[qBit] Torrent file queued → %s", save_path)
                return True
            if "forbidden" in resp.text.lower() or resp.status_code == 403:
                if self._handle_403():
                    return self.add_torrent_url(torrent_url, save_path, rename, extra_headers)
            # Duplicate infohash → "Fails." but the torrent is already present.
            if self.has_torrent(_infohash_from_torrent(torrent_bytes)):
                logger.info("[qBit] Torrent already present (duplicate infohash) — treating as queued.")
                return True
            logger.error("[qBit] Failed to add torrent: %s", resp.text[:200])
            return False
        except requests.RequestException as exc:
            logger.error("[qBit] Add torrent file failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Status / listing
    # ------------------------------------------------------------------

    def get_torrents(self, filter_state: Optional[str] = None) -> list[dict]:
        """
        Return list of torrent info dicts.
        filter_state: 'downloading', 'seeding', 'completed', 'paused', 'active', etc.
        """
        if not self._ensure_logged_in():
            return []
        url = self._base + "/api/v2/torrents/info"
        params = {}
        if filter_state:
            params["filter"] = filter_state
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("[qBit] get_torrents failed: %s", exc)
            return []

    def get_completed_torrents(self) -> list[dict]:
        return self.get_torrents(filter_state="completed")

    # ------------------------------------------------------------------
    # Preferences / download queueing
    # ------------------------------------------------------------------

    def set_preferences(self, prefs: dict) -> bool:
        """Push a partial preferences dict to qBittorrent (app/setPreferences)."""
        if not self._ensure_logged_in():
            return False
        import json as _json
        url = self._base + "/api/v2/app/setPreferences"
        try:
            resp = self._session.post(url, data={"json": _json.dumps(prefs)}, timeout=15)
            if resp.status_code == 403 and self._handle_403():
                resp = self._session.post(url, data={"json": _json.dumps(prefs)}, timeout=15)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.error("[qBit] set_preferences failed: %s", exc)
            return False

    def get_preferences(self) -> dict:
        """Return qBittorrent's current application preferences."""
        if not self._ensure_logged_in():
            return {}
        try:
            resp = self._session.get(self._base + "/api/v2/app/preferences", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("[qBit] get_preferences failed: %s", exc)
            return {}

    def transfer_info(self) -> dict:
        """Return qBittorrent's global transfer info (connection_status,
        dht_nodes, speeds, …). Empty dict if qBit is unreachable."""
        if not self._ensure_logged_in():
            return {}
        try:
            resp = self._session.get(self._base + "/api/v2/transfer/info", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.debug("[qBit] transfer_info failed: %s", exc)
            return {}

    def network_health(self) -> dict:
        """Detect whether qBittorrent can actually reach the BitTorrent network.

        Returns {'up', 'known', 'status', 'dht_nodes'}. ``up`` is False in the
        classic VPN-down / interface-bound-but-tunnel-offline state: qBit reports
        'disconnected' (or 'firewalled' with **zero DHT nodes**), so every torrent
        shows 0 seeds even when the releases are perfectly healthy.

        ``dht_nodes == 0`` is the reliable tell — when the network is reachable,
        qBit bootstraps hundreds of DHT nodes within seconds; a sustained 0 means
        outgoing BitTorrent traffic is blocked (expired VPN, dropped tunnel, etc.).
        ``known`` is False when qBit itself is unreachable, so callers can avoid
        acting on an uncertain reading.
        """
        ti = self.transfer_info()
        if not ti:
            return {"up": False, "known": False, "status": "unreachable", "dht_nodes": 0}
        status = ti.get("connection_status", "")
        dht = int(ti.get("dht_nodes", 0) or 0)
        # 'connected' = qBit has live peer connections → healthy regardless of DHT.
        # Otherwise we require DHT nodes; 0 DHT means it can't reach the network.
        up = (status == "connected") or (dht > 0)
        return {"up": up, "known": True, "status": status, "dht_nodes": dht}

    def ensure_queue_settings(self) -> int:
        """
        Make sure qBittorrent's download queue is enabled and its OWN configured
        max_active_downloads is actually honoured — we do NOT override the number,
        we use whatever you set inside qBittorrent.

        Two settings are required for the configured limit to behave:
          - queueing_enabled = True
          - dont_count_slow_torrents = False  (otherwise slow/metadata torrents
            don't count, and qBittorrent starts far more than the configured limit)
        We also raise max_active_torrents if it's lower than max_active_downloads,
        so the total-active cap can't choke the download cap.

        Returns the effective max_active_downloads.
        """
        prefs = self.get_preferences()
        cur_dl  = int(prefs.get("max_active_downloads", 5) or 5)
        cur_tor = int(prefs.get("max_active_torrents", cur_dl) or cur_dl)
        new: dict = {
            "queueing_enabled": True,
            "dont_count_slow_torrents": False,
        }
        if cur_tor < cur_dl + 3:
            new["max_active_torrents"] = cur_dl + 3
        self.set_preferences(new)
        logger.info(
            "[qBit] Queue enabled — honouring qBittorrent's configured limit of %d active downloads",
            cur_dl,
        )
        return cur_dl

    def resume_incomplete_paused(self) -> int:
        """Resume any incomplete torrents that are paused/stopped so qBittorrent's
        queue keeps feeding downloads up to the configured limit. Returns the count
        resumed. (Unlike force-start, this respects the active-download limit.)"""
        if not self._ensure_logged_in():
            return 0
        paused = [
            t["hash"] for t in self.get_torrents()
            if t.get("state", "") in ("pausedDL", "stoppedDL")
            and (t.get("progress", 0) or 0) < 1.0
        ]
        if not paused:
            return 0
        for h in paused:
            self.resume_torrent(h)
        logger.info("[qBit] Resumed %d paused torrent(s) to keep downloads flowing", len(paused))
        return len(paused)

    def unforce_all(self) -> int:
        """Clear the force-start flag on every torrent so they obey the queue limit.
        Returns the number of force-started torrents that were un-forced."""
        if not self._ensure_logged_in():
            return 0
        forced = [t["hash"] for t in self.get_torrents() if "forced" in t.get("state", "")]
        if not forced:
            return 0
        url = self._base + "/api/v2/torrents/setForceStart"
        try:
            self._session.post(url, data={"hashes": "|".join(forced), "value": "false"}, timeout=15)
            logger.info("[qBit] Cleared force-start on %d torrent(s) so the queue limit applies", len(forced))
            return len(forced)
        except requests.RequestException as exc:
            logger.error("[qBit] unforce_all failed: %s", exc)
            return 0

    def torrent_exists(self, name_fragment: str) -> bool:
        """Check if a torrent with a name containing name_fragment is already in qBit."""
        torrents = self.get_torrents()
        fragment_lower = name_fragment.lower()
        return any(fragment_lower in t.get("name", "").lower() for t in torrents)

    def reannounce_stalled(self) -> int:
        """Force-reannounce all stalled torrents so they re-contact the tracker.
        Returns the number of torrents reannounced."""
        if not self._ensure_logged_in():
            return 0
        stalled = self.get_torrents(filter_state="stalledDL")
        if not stalled:
            return 0
        hashes = "|".join(t["hash"] for t in stalled)
        url = self._base + "/api/v2/torrents/reannounce"
        try:
            resp = self._session.post(url, data={"hashes": hashes}, timeout=15)
            if resp.status_code == 200:
                logger.info("[qBit] Force-reannounced %d stalled torrent(s)", len(stalled))
                return len(stalled)
            logger.warning("[qBit] Reannounce returned %s", resp.status_code)
            return 0
        except requests.RequestException as exc:
            logger.error("[qBit] Reannounce failed: %s", exc)
            return 0

    def reannounce_torrent(self, torrent_hash: str) -> bool:
        """Force a single torrent to re-contact its tracker(s) / re-announce to the
        swarm — the cheapest way to recover a torrent that stalled because it lost
        all of its peer connections."""
        if not self._ensure_logged_in():
            return False
        try:
            resp = self._session.post(
                self._base + "/api/v2/torrents/reannounce",
                data={"hashes": torrent_hash}, timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException as exc:
            logger.debug("[qBit] reannounce_torrent failed: %s", exc)
            return False

    def resume_torrent(self, torrent_hash: str) -> bool:
        """Resume (re-start) a torrent — recovers torrents stuck in an error state
        after a transient I/O hiccup, without losing download progress."""
        if not self._ensure_logged_in():
            return False
        # qBittorrent renamed this endpoint from /resume to /start in v5.x; try both.
        for path in ("/api/v2/torrents/start", "/api/v2/torrents/resume"):
            try:
                resp = self._session.post(self._base + path, data={"hashes": torrent_hash}, timeout=10)
                if resp.status_code == 200:
                    return True
            except requests.RequestException as exc:
                logger.debug("[qBit] resume via %s failed: %s", path, exc)
        return False

    def recheck_torrent(self, torrent_hash: str) -> bool:
        """Force a re-check of a torrent's existing data (verifies pieces on disk)."""
        if not self._ensure_logged_in():
            return False
        try:
            resp = self._session.post(
                self._base + "/api/v2/torrents/recheck",
                data={"hashes": torrent_hash}, timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException as exc:
            logger.error("[qBit] recheck_torrent failed: %s", exc)
            return False

    def get_tracker_messages(self, torrent_hash: str) -> list[dict]:
        """Return tracker list for a specific torrent — useful for diagnosing stalls."""
        if not self._ensure_logged_in():
            return []
        url = self._base + "/api/v2/torrents/trackers"
        try:
            resp = self._session.get(url, params={"hash": torrent_hash}, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("[qBit] get_tracker_messages failed: %s", exc)
            return []

    def delete_torrent(self, torrent_hash: str, delete_files: bool = False) -> bool:
        """Delete a torrent (and optionally its downloaded files) from qBittorrent."""
        if not self._ensure_logged_in():
            return False
        url = self._base + "/api/v2/torrents/delete"
        data = {
            "hashes": torrent_hash,
            "deleteFiles": "true" if delete_files else "false",
        }
        try:
            resp = self._session.post(url, data=data, timeout=10)
            return resp.status_code == 200
        except requests.RequestException as exc:
            logger.error("[qBit] delete_torrent failed: %s", exc)
            return False

    def get_zero_seed_stalled(self, min_stall_minutes: int = 10) -> list[dict]:
        """
        Return torrents that are genuinely stuck and will never finish on their own:
          - 'stalledDL' / force-started torrents with 0 speed and a DEAD swarm
            (num_complete == 0, i.e. "0 (0)" seeds — no seeders exist anywhere), OR
          - torrents stuck "Downloading metadata" (metaDL): a magnet whose swarm is
            dead, so the metadata never arrives and it never becomes a real download.
        Such torrents should be removed and re-searched for a seeded release.
        Torrents showing "0 (N>0)" are NOT flagged — seeds exist in the swarm and the
        torrent will download once it connects.
        """
        import time as _time
        all_torrents = self.get_torrents()
        now = _time.time()
        results = []
        stuck_states = {"stalledDL", "stalled", "forcedDL", "forcedMetaDL"}

        for t in all_torrents:
            state    = t.get("state", "")
            progress = t.get("progress", 0)
            dlspeed  = t.get("dlspeed", 0)
            seeds    = t.get("num_seeds", 0)       # connected seeds (not swarm total)
            # Seeders that EXIST in the swarm (the "(N)" in qBit's Seeds column).
            # A torrent showing "0 (36)" has 36 seeds available and will download
            # once it connects — only "0 (0)" means a genuinely dead swarm.
            swarm_seeds = t.get("num_complete", 0)

            if progress >= 1.0:
                continue  # already done

            # Time since the torrent was added (metadata fetch never updates
            # last_activity in a useful way, so use added_on for the metaDL case).
            added_on   = t.get("added_on") or now
            added_mins = (now - added_on) / 60
            last_act   = t.get("last_activity") or added_on
            idle_mins  = (now - last_act) / 60
            has_metadata = (t.get("total_size") or t.get("size") or 0) > 0

            # Stuck fetching metadata (dead magnet) past the timeout
            is_stuck_meta = (
                state in ("metaDL", "forcedMetaDL")
                and not has_metadata
                and added_mins >= min_stall_minutes
            )

            # Dead torrent sitting in the download QUEUE. qBittorrent announces
            # queued torrents to the tracker, so num_complete is populated — if a
            # torrent has been queued past the stall window and still shows a dead
            # swarm (0 seeders AND 0 leechers), it will NEVER download even when it
            # reaches the front of the queue. These are the "0 seeds queued" items
            # that pile up; cull them so a seeded release can replace them.
            leechers = t.get("num_incomplete", 0) or 0
            is_dead_queued = (
                state in ("queuedDL", "stalledDL", "stalled")
                and has_metadata
                and swarm_seeds <= 0
                and leechers <= 0
                and added_mins >= min_stall_minutes
            )

            if state not in stuck_states and not is_stuck_meta \
                    and not is_dead_queued and dlspeed > 512:
                continue  # actively downloading fast enough — leave it alone

            # Genuinely dead: no seeds in the swarm AND nothing connected, stalled
            # for a while. If swarm_seeds > 0 the release is alive (just not
            # connected yet) — leave it alone so we don't cull good torrents.
            is_stuck = (
                dlspeed == 0
                and seeds == 0
                and swarm_seeds <= 0
                and idle_mins >= min_stall_minutes
                and state in stuck_states
            )
            # Force-started but dead swarm with 0 speed for a long time
            is_forced_stuck = (
                state in ("forcedDL", "forcedMetaDL")
                and dlspeed == 0
                and swarm_seeds <= 0
                and idle_mins >= min_stall_minutes
            )

            if is_stuck or is_forced_stuck or is_stuck_meta or is_dead_queued:
                results.append(t)

        return results

    def set_bottom_priority(self, hashes) -> bool:
        """Move torrent(s) to the BOTTOM of the download queue so qBittorrent
        deactivates them and promotes the next queued torrent into the freed
        active slot. Requires torrent queueing to be enabled (it is, via
        ensure_queue_settings). Accepts a hash string or an iterable of hashes."""
        if not self._ensure_logged_in():
            return False
        if isinstance(hashes, (list, tuple, set)):
            hashes = "|".join(h for h in hashes if h)
        if not hashes:
            return False
        try:
            resp = self._session.post(
                self._base + "/api/v2/torrents/bottomPrio",
                data={"hashes": hashes}, timeout=15,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.error("[qBit] bottomPrio failed: %s", exc)
            return False

    def get_slow_stalled(self, min_stall_minutes: int = 20,
                         min_speed_bytes: int = 1024) -> list[dict]:
        """Return incomplete torrents that are holding an ACTIVE download slot
        but making no real progress — stalled at ~0 B/s with no data exchanged
        for >= ``min_stall_minutes`` — REGARDLESS of how many seeders the swarm
        has. These hog the ``max_active_downloads`` slots and starve well-seeded
        queued torrents, so the caller demotes them to the bottom of the queue to
        rotate the queue (they get another turn when they cycle back to the top).

        Crawling-but-moving torrents are NOT flagged: any data transfer refreshes
        ``last_activity``, so a torrent trickling even a few KiB/s keeps its idle
        timer low and is left alone. Truly-dead (0-seeder) torrents are handled by
        ``get_zero_seed_stalled`` instead (those get removed, not just demoted).
        """
        import time as _time
        now = _time.time()
        active_states = {"downloading", "stalledDL", "forcedDL", "metaDL", "forcedMetaDL"}
        out = []
        for t in self.get_torrents():
            if (t.get("progress", 0) or 0) >= 1.0:
                continue
            if t.get("state", "") not in active_states:
                continue  # queued/paused torrents don't hold an active slot
            if (t.get("dlspeed", 0) or 0) >= min_speed_bytes:
                continue  # actually moving — leave it alone
            last_act = t.get("last_activity") or t.get("added_on") or now
            if (now - last_act) / 60 >= min_stall_minutes:
                out.append(t)
        return out
