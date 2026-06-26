#!/usr/bin/env python3
"""
Jellyfin Media Auto-Downloader
Main orchestrator — load config, scan libraries, diff against TMDB, queue downloads.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import schedule

from notifier import Notifier
from qbit_client import QBitClient
from scanner import (
    clean_show_name,
    detect_season_padding,
    make_movie_filename,
    make_movie_folder,
    find_existing_season_folder,
    make_tv_filename,
    make_tv_season_folder,
    make_tv_show_folder,
    scan_movie_library,
    scan_tv_library,
)
from sources.scenetime import SceneTimeSource
from sources.jackett import JackettSource
from tmdb_client import TMDBClient
from torrent_finder import TorrentFinder
from tvmaze_client import TVMazeClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE = Path(__file__).parent / "downloader.log"
def _make_stream_handler() -> logging.StreamHandler:
    h = logging.StreamHandler(sys.stdout)
    # Force UTF-8 on Windows consoles that default to cp1252
    if hasattr(h.stream, "reconfigure"):
        try:
            h.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    else:
        h.stream = open(
            h.stream.fileno(), mode="w", encoding="utf-8", errors="replace", closefd=False
        )
    return h

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        _make_stream_handler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("downloader")

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

STATE_FILE = Path(__file__).parent / "state.json"
RUN_LOCK_FILE = Path(__file__).parent / "run.lock"
# A run-lock older than this (no live owner) is considered abandoned.
RUN_LOCK_STALE_MINUTES = 30


def _pid_alive(pid: int) -> bool:
    """Best-effort check whether a process id is still running (Windows-first,
    with a portable fallback)."""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, Exception):  # pylint: disable=broad-except
        return False


def _run_lock_holder() -> Optional[dict]:
    """Return the lock-owner info dict if a DIFFERENT live, fresh run holds the
    run lock, else None (lock free, ours, dead, or stale)."""
    if not RUN_LOCK_FILE.exists():
        return None
    try:
        info = json.loads(RUN_LOCK_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    owner_pid = int(info.get("pid", 0) or 0)
    if not owner_pid or owner_pid == os.getpid():
        return None
    ts = info.get("ts", "")
    fresh = False
    if ts:
        try:
            age = (datetime.now() - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")).total_seconds()
            fresh = age < RUN_LOCK_STALE_MINUTES * 60
        except ValueError:
            fresh = False
    if _pid_alive(owner_pid) and fresh:
        return info
    return None


def acquire_run_lock(label: str, wait: bool = False, wait_timeout: int = 900) -> bool:
    """Single-writer guard. Returns True if THIS process now owns the run lock.

    Prevents the runaway-process pile-up where many overlapping downloader runs
    all queue the whole library into qBittorrent at once. A lock is taken over
    only if its owner PID is dead OR its heartbeat is stale, so a crashed run
    can never wedge the pipeline permanently.

    wait=False (auto/opportunistic runs): return False immediately if another
    live run holds it (caller should exit).
    wait=True (the user-initiated campaign — the primary writer): poll until the
    holder releases (transient rss/run-now finishes), up to wait_timeout, then
    take it anyway.
    """
    try:
        deadline = time.time() + wait_timeout
        while True:
            holder = _run_lock_holder()
            if holder is None:
                break
            if not wait:
                logger.warning(
                    "[RunLock] Another run (%s, pid=%s) is already active — exiting so we don't "
                    "double-queue.", holder.get("label", "?"), holder.get("pid"),
                )
                return False
            if time.time() >= deadline:
                logger.warning(
                    "[RunLock] Waited %ds for run (%s, pid=%s) to finish — taking the lock anyway.",
                    wait_timeout, holder.get("label", "?"), holder.get("pid"),
                )
                break
            logger.info(
                "[RunLock] Waiting for transient run (%s, pid=%s) to finish before starting %r…",
                holder.get("label", "?"), holder.get("pid"), label,
            )
            time.sleep(5)
        RUN_LOCK_FILE.write_text(
            json.dumps({"pid": os.getpid(), "label": label,
                        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}),
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        # If we can't even read/write the lock, fail OPEN (run anyway) rather
        # than block downloads on a filesystem hiccup.
        logger.debug("[RunLock] lock IO error (continuing unguarded): %s", exc)
        return True


def release_run_lock() -> None:
    """Release the run lock if we own it."""
    try:
        if RUN_LOCK_FILE.exists():
            info = json.loads(RUN_LOCK_FILE.read_text(encoding="utf-8"))
            if int(info.get("pid", 0) or 0) == os.getpid():
                RUN_LOCK_FILE.unlink()
    except (OSError, json.JSONDecodeError):
        pass


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"queued": [], "history": [], "last_run": None, "retry_queue": {}}


def save_state(state: dict, preserve_quarantine: bool = True) -> None:
    # The 'quarantine' map is mutated by TWO processes at once — the
    # --validate-paths producer (which ADDS entries as it finds broken files)
    # and the detached validation watcher (which REMOVES them when a
    # replacement passes or all sources are exhausted). A normal bulk save of a
    # stale in-memory snapshot would clobber the other process's recent change
    # (e.g. re-adding an entry the watcher just purged), leaving orphaned
    # bookkeeping. So unless the caller is explicitly editing the map (via
    # update_quarantine, which passes preserve_quarantine=False), we always keep
    # whatever the quarantine map currently is ON DISK.
    if preserve_quarantine:
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE, encoding="utf-8") as f:
                    disk_q = json.load(f).get("quarantine")
            else:
                disk_q = None
            if disk_q is not None:
                state = {**state, "quarantine": disk_q}
            elif "quarantine" in state:
                # disk has none yet but our snapshot carries one — drop it so a
                # stale snapshot can never resurrect quarantine bookkeeping.
                state = {k: v for k, v in state.items() if k != "quarantine"}
        except (json.JSONDecodeError, OSError):
            pass
    # Sanitize to valid JSON (strip control characters from all string values)
    text = json.dumps(state, indent=2)
    import re as _re
    text = _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def update_quarantine(key: str, path: Optional[str] = None, remove: bool = False) -> None:
    """Atomically add or remove a single quarantine entry on disk. This is the
    ONLY sanctioned way to mutate the quarantine map, because it read-modify-
    writes the latest disk state (rather than a possibly-stale in-memory
    snapshot) and so is safe against the concurrent producer/watcher writers."""
    st = load_state()
    q = st.setdefault("quarantine", {})
    if remove:
        q.pop(key, None)
    elif path is not None:
        q[key] = path
    save_state(st, preserve_quarantine=False)


def _jackett_config(config: dict) -> Optional[dict]:
    """Return the enabled Jackett source config block, if any."""
    for src in config.get("torrent_sources", []):
        if src.get("type") == "jackett" and src.get("enabled"):
            return src
    return None


def jackett_reachable(jk: dict, timeout: int = 8) -> bool:
    """Quick liveness probe of the Jackett Torznab API (caps call is cheap)."""
    try:
        import requests
        url = f"{jk.get('url', 'http://127.0.0.1:9117').rstrip('/')}" \
              f"/api/v2.0/indexers/{jk.get('trackers', 'all')}/results/torznab/api"
        r = requests.get(
            url, params={"apikey": jk.get("api_key", ""), "t": "caps"}, timeout=timeout
        )
        return r.status_code == 200
    except Exception:
        return False


def ensure_jackett_up(config: dict, restart: bool = True, wait_s: int = 45) -> bool:
    """Make sure the indexer is reachable before we delete-and-replace anything.

    Returns True if Jackett answers. If it's down and ``restart`` is set and an
    ``exe_path`` is configured, launch it detached and poll until it comes up or
    ``wait_s`` elapses. This is the guard that prevents the "delete a corrupt
    file but find no replacement (because the indexer was offline)" failure mode.
    """
    jk = _jackett_config(config)
    if not jk:
        # No Jackett configured — nothing to guard; let the caller proceed.
        return True
    if jackett_reachable(jk):
        return True
    if not restart:
        return False
    exe = jk.get("exe_path")
    if not exe or not Path(exe).exists():
        logger.error("[Jackett] DOWN and no valid exe_path configured — cannot auto-restart.")
        return False
    logger.warning("[Jackett] Indexer is DOWN — launching %s and waiting up to %ds…", exe, wait_s)
    try:
        import subprocess
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        log_path = Path(__file__).parent / "jackett_console.log"
        with open(log_path, "ab") as lf:
            subprocess.Popen(
                [exe], stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                creationflags=flags, close_fds=True,
            )
    except Exception as exc:
        logger.error("[Jackett] Failed to launch: %s", exc)
        return False
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(3)
        if jackett_reachable(jk):
            logger.info("[Jackett] Back online.")
            return True
    logger.error("[Jackett] Still unreachable after %ds.", wait_s)
    return False


def is_already_queued(state: dict, key: str) -> bool:
    return key in state.get("queued", [])


def mark_queued(state: dict, key: str) -> None:
    if key not in state["queued"]:
        state["queued"].append(key)
    # Remove from retry queue if it was there
    state.setdefault("retry_queue", {}).pop(key, None)


def add_to_retry_queue(state: dict, key: str, label: str) -> None:
    """Record a failed search for retry on the next run."""
    from datetime import datetime as _dt
    rq = state.setdefault("retry_queue", {})
    if key not in rq:
        rq[key] = {"label": label, "attempts": 1, "last_tried": _dt.utcnow().isoformat()}
    else:
        rq[key]["attempts"] = rq[key].get("attempts", 0) + 1
        rq[key]["last_tried"] = _dt.utcnow().isoformat()


def should_retry(state: dict, key: str, schedule_hours: int = 6) -> bool:
    """
    Return True if this key should be searched this run.
    Applies an exponential backoff:
      - 1-3 failures  → retry every run (normal)
      - 4-6 failures  → retry every 3 runs  (~18h at 6h schedule)
      - 7-10 failures → retry every 7 runs  (~42h)
      - 11+ failures  → retry every 14 runs (~84h)
    This prevents SceneTime from being hammered with searches that always fail.
    """
    from datetime import datetime as _dt
    rq = state.get("retry_queue", {})
    if key not in rq:
        return True  # never failed — always try
    entry    = rq[key]
    attempts = entry.get("attempts", 0)
    if attempts <= 3:
        return True
    # Compute how many hours must pass before retrying
    if attempts <= 6:
        wait_hours = schedule_hours * 3
    elif attempts <= 10:
        wait_hours = schedule_hours * 7
    else:
        wait_hours = schedule_hours * 14
    last_str = entry.get("last_tried", "")
    if not last_str:
        return True
    try:
        last = _dt.fromisoformat(last_str)
        hours_since = (_dt.utcnow() - last).total_seconds() / 3600
        return hours_since >= wait_hours
    except Exception:
        return True


def is_in_retry_queue(state: dict, key: str) -> bool:
    return key in state.get("retry_queue", {})


def record_history(state: dict, entry: dict) -> None:
    """Prepend a download event to history. Keeps last 500 entries."""
    if "history" not in state:
        state["history"] = []
    entry["queued_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    state["history"].insert(0, entry)
    state["history"] = state["history"][:500]


def update_scan_status(
    state: dict,
    status: str,
    detail: str = "",
    lib_name: str = "",
    lib_idx: int = 0,
    lib_total: int = 0,
    show_idx: int = 0,
    show_total: int = 0,
    show_name: str = "",
) -> None:
    state["scan_status"] = {
        "status":     status,   # "idle" | "running" | "error"
        "detail":     detail,
        "lib_name":   lib_name,
        "lib_idx":    lib_idx,
        "lib_total":  lib_total,
        "show_idx":   show_idx,
        "show_total": show_total,
        "show_name":  show_name,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def save_library_snapshot(state: dict, snapshot: dict) -> None:
    """Cache a library inventory snapshot so the web UI can read it quickly."""
    state["library_snapshot"] = {
        "data": snapshot,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).parent / "config.json"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        logger.error("config.json not found — copy config.json.example and fill in your details")
        sys.exit(1)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    """Persist config.json atomically."""
    text = json.dumps(config, indent=2)
    CONFIG_FILE.write_text(text, encoding="utf-8")


def validate_config(config: dict) -> list[str]:
    """Sanity-check config.json and return a list of human-readable warnings.

    Never raises and never exits — it just surfaces the common foot-guns at
    startup so they're caught before a run silently misbehaves: unreachable or
    typo'd library paths (e.g. a misspelled NAS share), duplicate paths, enabled
    sources missing credentials, placeholder/empty Discord webhook, and feature
    flags that need a companion setting (RSS buttons need web_public_url).
    """
    warnings: list[str] = []

    # ── Libraries ───────────────────────────────────────────────────────────
    libs = config.get("libraries", [])
    if not libs:
        warnings.append("No libraries configured — nothing to scan.")
    seen_paths: dict[str, str] = {}
    valid_types = {"tv", "animation", "movie"}
    for lib in libs:
        name = lib.get("name", "?")
        if not lib.get("enabled", True):
            continue
        path = lib.get("path", "")
        if not path:
            warnings.append(f"Library '{name}' has no path.")
            continue
        ltype = (lib.get("type") or "").lower()
        if ltype not in valid_types:
            warnings.append(
                f"Library '{name}' has unknown type {lib.get('type')!r} (expected tv/animation/movie)."
            )
        key = path.rstrip("/\\").lower()
        if key in seen_paths:
            warnings.append(f"Library '{name}' duplicates the path of '{seen_paths[key]}': {path}")
        else:
            seen_paths[key] = name
        try:
            if not os.path.exists(path):
                warnings.append(
                    f"Library '{name}' path is not reachable (typo or offline?): {path}"
                )
        except Exception as exc:
            warnings.append(f"Library '{name}' path could not be checked ({exc}): {path}")

    # ── Torrent sources ───────────────────────────────────────────────────────
    srcs = config.get("torrent_sources", [])
    if not [s for s in srcs if s.get("enabled", True)]:
        warnings.append("No torrent sources enabled — searches will return nothing.")
    for s in srcs:
        if not s.get("enabled", True):
            continue
        name = s.get("name", "?")
        stype = (s.get("type") or s.get("name", "")).lower()
        if "jackett" in stype or "prowlarr" in stype:
            key = s.get("api_key", "")
            if not key or "your_" in key.lower():
                warnings.append(f"Source '{name}' is enabled but has no valid api_key.")
            if not s.get("url"):
                warnings.append(f"Source '{name}' is enabled but has no url.")
        elif "scenetime" in stype:
            if not s.get("username") or not s.get("password"):
                warnings.append(f"Source '{name}' is enabled but is missing username/password.")

    # ── Notifications + feature flags ──────────────────────────────────────────
    hook = (config.get("notifications", {}).get("discord_webhook") or "").strip()
    hook_on = bool(hook and "your_webhook" not in hook)
    if not hook_on:
        warnings.append("No Discord webhook configured — Discord notifications are off.")

    web_url = (config.get("web_public_url") or "").strip()
    if web_url and not web_url.startswith(("http://", "https://")):
        warnings.append(f"web_public_url should start with http:// or https:// — got {web_url!r}.")
    if hook_on and not web_url:
        warnings.append(
            "web_public_url is empty — Discord corrupt-review buttons will be hidden "
            "(set it to your LAN URL, e.g. http://192.168.0.x:5000, to enable them)."
        )

    sh = config.get("schedule_hours", 6)
    if not isinstance(sh, (int, float)) or isinstance(sh, bool) or sh <= 0:
        warnings.append(f"schedule_hours should be a positive number — got {sh!r}.")

    return warnings


# ---------------------------------------------------------------------------
# Source factory
# ---------------------------------------------------------------------------

SOURCE_MAP = {
    "scenetime": SceneTimeSource,
    "jackett":   JackettSource,
    "prowlarr":  JackettSource,  # Prowlarr uses the same Torznab API
}


def build_sources(torrent_sources: list[dict]) -> list:
    sources = []
    for src_cfg in torrent_sources:
        if not src_cfg.get("enabled", True):
            continue
        name_key = src_cfg.get("name", "").lower().replace(" ", "")
        cls = SOURCE_MAP.get(name_key)
        if cls is None:
            logger.warning("Unknown torrent source: %s — skipping", src_cfg.get("name"))
            continue
        sources.append(cls(src_cfg))
    return sources


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_WATCHLIST_STRIP_RE = re.compile(r"\s*\(\d{4}\)\s*$")


def normalise_show_name(name: str) -> str:
    return _WATCHLIST_STRIP_RE.sub("", name).strip().lower()


def parse_watchlist_movie(entry: str) -> tuple[str, Optional[int]]:
    """Parse 'Movie Title (2024)' → ('Movie Title', 2024)."""
    m = re.match(r"^(.+?)\s*\((\d{4})\)\s*$", entry)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return entry.strip(), None


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

class Downloader:
    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run

        quality = config.get("quality", {})
        sources = build_sources(config.get("torrent_sources", []))
        # Finder will be wired to persisted group-failure counts after
        # state is loaded below.
        self.finder = TorrentFinder(sources, quality)
        # Let the finder surface a "paused — waiting for Jackett" status on the
        # dashboard while it blocks for a downed gating source.
        self.finder.on_wait_status = self._finder_wait_status

        tmdb_cfg = config.get("tmdb", {})
        self.tmdb = TMDBClient(tmdb_cfg.get("api_key", ""))
        self.tvmaze = TVMazeClient()

        qbit_cfg = config.get("qbittorrent", {})
        self.qbit = QBitClient(
            qbit_cfg.get("url", "http://localhost:8080"),
            username=qbit_cfg.get("username", ""),
            password=qbit_cfg.get("password", ""),
            bypass_auth=qbit_cfg.get("bypass_auth", False),
        )

        notif_cfg = config.get("notifications", {})
        self.notifier = Notifier(
            discord_webhook=notif_cfg.get("discord_webhook", ""),
            ntfy_topic=notif_cfg.get("ntfy_topic", ""),
        )

        self.state = load_state()
        self._global_show_index: dict = {}  # populated at start of each run()
        self._min_seeders: int = quality.get("min_seeders", 5)
        # ── In-flight download cap ──────────────────────────────────────────
        # Hard ceiling on how many incomplete torrents may sit in qBittorrent
        # before we stop starting NEW shows. Keeps the pipeline working a couple
        # of series at a time (download → validate → next) instead of dumping the
        # whole library into the queue at once. 0 = unlimited (legacy behaviour).
        self._max_in_flight: int = int(config.get("max_in_flight_downloads", 20))
        self._in_flight_cache_ts: float = 0.0
        self._last_in_flight: int = 0
        # ── qBittorrent connectivity cache (VPN-down detection) ──────────────
        self._conn_cache_ts: float = 0.0
        self._conn_up: bool = True
        # When True, episodes are published to pending_validation AS THEY ARE
        # QUEUED and the watcher is spawned immediately, so it manages/validates
        # downloads CONCURRENTLY with a long search phase (used by catch_up_show).
        self._concurrent_watch: bool = False
        self._watcher_spawned: bool = False
        # When True, the per-episode retry-backoff cooldown is ignored so EVERY
        # still-missing episode is searched/retried on this run (no "wait for the
        # next scan"). Set by the manual "Download Missing" button + catch-up.
        self._ignore_cooldown: bool = False

        # Now that state is loaded, plug the persisted per-release-group failure
        # counter into the finder so it auto-deprioritises uploaders that have
        # produced corrupt downloads in the past.
        self.finder._group_failures = self.state.get("group_failure_count", {})

    def _finder_wait_status(self, msg: Optional[str]) -> None:
        """Called by TorrentFinder while it's paused waiting for a downed
        gating source (Jackett). Reflects the pause on the dashboard so it's
        obvious the run isn't stuck — it's deliberately waiting.

        msg=None means the source just recovered: we MUST overwrite the lingering
        "Paused…" detail right away, otherwise the banner stays stuck on
        "Paused" long after searches resumed (the watcher's own status update can
        be many minutes away while it works through a deep-validation backlog)."""
        try:
            st = getattr(self, "state", None)
            if st is None:
                return
            update_scan_status(
                st, "running",
                detail=msg or "Source back online — resuming searches…",
            )
            save_state(st)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _recover_stalled_torrents(self) -> list[str]:
        """
        Detect zero-seed stalled torrents in qBittorrent that have been stuck
        for >10 minutes.  Delete them from qBit and unmark the corresponding
        episode keys in state.json so the next pass re-searches for a better release.
        Returns a list of state keys that were cleared (for immediate retry).
        """
        import re as _re
        # Don't cull while qBit is offline — the 0-seed readings are an artifact
        # of the dead connection, not dead releases. Culling+re-searching here
        # would churn healthy torrents and (via the watcher) blacklist them.
        if self._qbit_offline():
            logger.warning("[Recovery] qBittorrent offline — skipping zero-seed cull.")
            return []
        zero_seed = self.qbit.get_zero_seed_stalled(min_stall_minutes=10)
        if not zero_seed:
            return []

        logger.info(
            "[Recovery] Found %d zero-seed stalled torrent(s) — removing for retry",
            len(zero_seed),
        )

        # Build a lookup of save_path → state keys for accurate matching
        queued: list = self.state.get("queued", [])
        cleared_keys: list[str] = []

        for t in zero_seed:
            name = t.get("name", "")
            torrent_hash = t.get("hash", "")
            save_path = t.get("save_path", "")

            # Delete from qBittorrent (keep partial files — harmless on NAS)
            deleted = self.qbit.delete_torrent(torrent_hash, delete_files=False)
            if not deleted:
                continue
            logger.info("[Recovery] Deleted stalled: %s", name)

            # ── Find the matching state key ─────────────────────────────────
            # Strategy 1: parse SxxExx from torrent name
            se_match = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", name)
            if se_match:
                season  = int(se_match.group(1))
                episode = int(se_match.group(2))
                # Try to match by save_path show folder
                show_guess = ""
                if save_path:
                    # save_path is like \\NAS\lib\Show Name\Season 01
                    parts = _re.split(r"[/\\]", save_path.rstrip("/\\"))
                    # Walk back past "Season XX" to the show folder
                    for i, p in enumerate(reversed(parts)):
                        if _re.match(r"(?i)season\s*\d+", p):
                            if i + 1 < len(parts):
                                show_guess = parts[-(i + 2)]
                            break
                candidate = f"tv::{show_guess}::S{season:02d}E{episode:02d}"
                # Find the closest matching key in queued list
                matching = [
                    k for k in queued
                    if f"S{season:02d}E{episode:02d}".lower() in k.lower()
                    and (not show_guess or show_guess.lower()[:8] in k.lower())
                ]
            else:
                # Strategy 2: season pack — match by state key pattern
                matching = [
                    k for k in queued
                    if any(w.lower() in k.lower() for w in name.split()[:4] if len(w) > 3)
                ]

            for k in matching:
                if k in queued:
                    queued.remove(k)
                    cleared_keys.append(k)
                    logger.info("[Recovery] Cleared queued key for retry: %s", k)

        self.state["queued"] = queued
        save_state(self.state)

        if cleared_keys:
            logger.info(
                "[Recovery] %d episode(s) unqueued — will be re-searched this run",
                len(cleared_keys),
            )
        return cleared_keys

    def _recover_errored_torrents(self) -> list[str]:
        """
        Detect torrents qBittorrent has marked 'error' or 'missingFiles' (e.g.
        "system cannot find the file specified" or "End of file" — usually because
        the partial file was moved/removed mid-download by older code).  These never
        recover on their own, so delete them (and any truncated partial) and unmark
        the matching episode keys so this run re-downloads them cleanly.
        Returns the list of cleared state keys.
        """
        import re as _re
        errored = [
            t for t in self.qbit.get_torrents()
            if t.get("state", "") in ("error", "missingFiles")
        ]
        if not errored:
            return []

        logger.info(
            "[Recovery] Found %d errored/missing-file torrent(s) — removing for clean re-download",
            len(errored),
        )

        queued: list = self.state.get("queued", [])
        downloaded: dict = self.state.get("downloaded_torrents", {})
        cleared_keys: list[str] = []

        for t in errored:
            name = t.get("name", "")
            torrent_hash = t.get("hash", "")
            save_path = t.get("save_path", "")

            # Remove the torrent and any truncated partial it left behind
            if not self.qbit.delete_torrent(torrent_hash, delete_files=True):
                continue
            logger.info("[Recovery] Deleted errored: %s (%s)", name, t.get("state"))

            se_match = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", name)
            if se_match:
                season  = int(se_match.group(1))
                episode = int(se_match.group(2))
                show_guess = ""
                if save_path:
                    parts = _re.split(r"[/\\]", save_path.rstrip("/\\"))
                    for i, p in enumerate(reversed(parts)):
                        if _re.match(r"(?i)season\s*\d+", p):
                            if i + 1 < len(parts):
                                show_guess = parts[-(i + 2)]
                            break
                matching = [
                    k for k in queued
                    if f"S{season:02d}E{episode:02d}".lower() in k.lower()
                    and (not show_guess or show_guess.lower()[:8] in k.lower())
                ]
            else:
                matching = [
                    k for k in queued
                    if any(w.lower() in k.lower() for w in name.split()[:4] if len(w) > 3)
                ]

            for k in matching:
                if k in queued:
                    queued.remove(k)
                    cleared_keys.append(k)
                downloaded.pop(k, None)
                logger.info("[Recovery] Cleared marker for re-download: %s", k)

        self.state["queued"] = queued
        self.state["downloaded_torrents"] = downloaded
        save_state(self.state)

        if cleared_keys:
            logger.info(
                "[Recovery] %d errored episode(s) unqueued — will be re-downloaded this run",
                len(cleared_keys),
            )
        return cleared_keys

    def run(self, library_filter: Optional[str] = None, aggressive: bool = False) -> None:
        import time as _time_mod
        _run_start = _time_mod.time()

        # Aggressive mode (the manual "Download Missing" button): manage/validate
        # downloads CONCURRENTLY with the scan and retry failures immediately
        # instead of deferring to the next scheduled scan. Renaming/moving of
        # completed downloads is owned entirely by the watcher in this mode (so
        # we skip the end-of-run rename/extract pass that would otherwise fight
        # the watcher over the same files).
        self._concurrent_watch = aggressive
        self._ignore_cooldown = aggressive

        self.state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        update_scan_status(self.state, "running", "Scan started")
        save_state(self.state)

        # Accumulate what was queued this run for the Discord summary
        self._run_tv_queued:     dict[str, list[str]] = {}   # show → [S01E01, ...]
        self._run_tv_paths:      dict[str, str]       = {}   # show → NAS show folder path
        self._run_movies_queued: list[str]            = []
        self._run_movie_paths:   dict[str, str]       = {}   # movie label → NAS folder path
        self._run_not_found:     dict[str, list[str]] = {}   # show/movie → [S01E01, ...]
        # Track this run's downloads so the post-download watcher can wait for
        # them to finish in qBittorrent, then validate ONLY these files.
        self._run_pending_torrents: list[str] = []           # torrent release names to wait for
        self._run_validation_keys:  set[str]  = set()        # episode state_keys to validate

        # ── Respect qBittorrent's own configured download-queue limit ──────
        try:
            self.qbit.ensure_queue_settings()
            self.qbit.resume_incomplete_paused()
        except Exception as _q_exc:
            logger.warning("[qBit] Could not apply queue settings (non-fatal): %s", _q_exc)

        # ── Error recovery: clear errored / missing-file torrents ───────────
        try:
            self._recover_errored_torrents()
        except Exception as _err_exc:
            logger.warning("[Recovery] Errored-torrent recovery error (non-fatal): %s", _err_exc)

        # ── Stall recovery: clear zero-seed stuck torrents before searching ──
        try:
            self._recover_stalled_torrents()
        except Exception as _rec_exc:
            logger.warning("[Recovery] Stall recovery error (non-fatal): %s", _rec_exc)

        # ── Build cross-library show index ──────────────────────────────────
        self._global_show_index = self._build_global_show_index()

        snapshot: dict = {}
        raw_libraries = self.config.get("libraries", [])
        # Process animation libraries first so they are never starved by long TV Show runs
        libraries = sorted(
            raw_libraries,
            key=lambda l: 0 if l.get("type", "").lower() == "animation" else 1,
        )
        # Count total enabled libraries for progress display
        active_libs = [
            l for l in libraries
            if l.get("enabled", True)
            and (not library_filter or l.get("name", "").lower() == library_filter.lower())
        ]
        lib_total = len(active_libs)
        lib_idx   = 0

        try:
            for lib in libraries:
                if not lib.get("enabled", True):
                    continue
                if library_filter and lib.get("name", "").lower() != library_filter.lower():
                    continue
                lib_idx  += 1
                lib_type  = lib.get("type", "").lower()
                lib_name  = lib.get("name", "?")
                update_scan_status(
                    self.state, "running",
                    detail   = f"Scanning {lib_name}…",
                    lib_name = lib_name,
                    lib_idx  = lib_idx,
                    lib_total= lib_total,
                )
                save_state(self.state)

                try:
                    if lib_type == "tv" or lib_type == "animation":
                        lib_snapshot = self._process_tv_library(lib, lib_idx=lib_idx, lib_total=lib_total)
                        snapshot[lib_name] = lib_snapshot
                    elif lib_type == "movie":
                        lib_snapshot = self._process_movie_library(lib)
                        snapshot[lib_name] = lib_snapshot
                    else:
                        logger.warning("Unknown library type %r for %r", lib_type, lib.get("name"))
                except Exception as _lib_exc:
                    logger.error(
                        "[Run] Library %r failed — skipping and continuing with remaining libraries: %s",
                        lib_name, _lib_exc, exc_info=True,
                    )

            save_library_snapshot(self.state, snapshot)

            # ── Discord scan summary — fire immediately after all libraries ──
            # Sending here (before rename/RAR/RSS) ensures the notification
            # is delivered even if post-processing hangs or the server restarts.
            duration = _time_mod.time() - _run_start
            # Persist summary data so the UI "Resend Notification" button can replay it
            self.state["last_scan_summary"] = {
                "tv_added":      self._run_tv_queued,
                "tv_paths":      self._run_tv_paths,
                "movies_added":  self._run_movies_queued,
                "movie_paths":   self._run_movie_paths,
                "not_found":     self._run_not_found,
                "duration_sec":  duration,
                "sent_at":       _time_mod.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            save_state(self.state)
            try:
                self.notifier.scan_summary(
                    tv_added=self._run_tv_queued,
                    tv_paths=self._run_tv_paths,
                    movies_added=self._run_movies_queued,
                    movie_paths=self._run_movie_paths,
                    tv_queued={},
                    not_found=self._run_not_found,
                    duration_sec=duration,
                )
                logger.info("[Notify] Scan summary sent to Discord.")
            except Exception as notif_exc:
                logger.warning("Discord summary error (non-fatal): %s", notif_exc)

            # ── Mid-run stall recovery: catch anything that stalled this run ───
            try:
                self._recover_stalled_torrents()
            except Exception as _rec2_exc:
                logger.warning("[Recovery] Mid-run stall recovery error (non-fatal): %s", _rec2_exc)

            # ── RSS discovery pass ──────────────────────────────────────────
            try:
                update_scan_status(self.state, "running", "Running RSS discovery…")
                save_state(self.state)
                self._run_rss_discovery(snapshot)
            except Exception as disc_exc:
                logger.warning("RSS discovery error (non-fatal): %s", disc_exc)

            # In concurrent mode the validation watcher moves/renames each file
            # as it finishes downloading, so running the bulk extract/rename pass
            # here would race it over the same files. Skip them — the watcher owns
            # post-download file handling (same as the catch-up flow).
            if not self._concurrent_watch:
                # ── Auto-extract RAR archives from completed downloads ───────────
                try:
                    update_scan_status(self.state, "running", "Extracting RAR archives…")
                    save_state(self.state)
                    self._run_extraction()
                except Exception as ext_exc:
                    logger.warning("RAR extraction error (non-fatal): %s", ext_exc)

                # ── Rename scene-style filenames to clean Jellyfin names ─────────
                try:
                    update_scan_status(self.state, "running", "Renaming files…")
                    save_state(self.state)
                    self._run_rename()
                except Exception as ren_exc:
                    logger.warning("File rename error (non-fatal): %s", ren_exc)

            # ── Hand off to the post-download validation watcher ─────────────
            # If this run queued anything, spawn a detached watcher that waits
            # for those torrents to finish downloading, then validates ONLY
            # those files (full decode) and writes pending_download_review for
            # the UI popup. Keeps the scan status as "running" until the watcher
            # takes over so the UI doesn't prematurely flash "idle".
            if not self._arm_download_watcher():
                update_scan_status(self.state, "idle", "Last scan completed")

            # ── Persist TVMaze disk cache ───────────────────────────────────
            try:
                self.tvmaze.flush()
            except Exception:
                pass

        except Exception as exc:
            logger.error("Scan error: %s", exc, exc_info=True)
            update_scan_status(self.state, "error", str(exc))
            # Still attempt to notify with whatever was accumulated before the crash
            try:
                duration = _time_mod.time() - _run_start
                self.state["last_scan_summary"] = {
                    "tv_added":     self._run_tv_queued,
                    "tv_paths":     self._run_tv_paths,
                    "movies_added": self._run_movies_queued,
                    "movie_paths":  self._run_movie_paths,
                    "not_found":    self._run_not_found,
                    "duration_sec": duration,
                    "sent_at":      _time_mod.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                self.notifier.scan_summary(
                    tv_added=self._run_tv_queued,
                    tv_paths=self._run_tv_paths,
                    movies_added=self._run_movies_queued,
                    movie_paths=self._run_movie_paths,
                    tv_queued={},
                    not_found=self._run_not_found,
                    duration_sec=duration,
                )
            except Exception:
                pass
            # CRITICAL: even though the run crashed, anything we already queued is
            # still downloading — arm the validation watcher so those files still
            # get tested and surfaced in the review popup (a crash must not skip it).
            try:
                self._arm_download_watcher()
            except Exception:
                pass
        finally:
            save_state(self.state)

    def _arm_download_watcher(self) -> bool:
        """Finalise pending_validation for this run and ensure the watcher is
        running. In concurrent mode the watcher is already up and episodes were
        published as they queued — here we just MERGE in anything not yet
        published and flip scan_active off so the watcher knows the producer is
        done and may drain + exit once everything is resolved. Returns True if
        there is anything to validate."""
        if self.dry_run or not self._run_pending_torrents:
            return False
        pv = self.state.setdefault("pending_validation", {})
        pv["torrents"]   = sorted(set(pv.get("torrents", [])) | set(self._run_pending_torrents))
        pv["keys"]       = sorted(set(pv.get("keys", [])) | set(self._run_validation_keys))
        pv.setdefault("started_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        pv["scan_active"] = False   # producer finished → watcher may drain & exit
        update_scan_status(
            self.state, "running",
            detail=f"Waiting for {len(pv['torrents'])} download(s) to finish…",
        )
        save_state(self.state)
        if not self._watcher_spawned:
            self._spawn_download_watcher()
        return True

    def _run_rss_discovery(self, snapshot: dict) -> None:
        """
        Fetch the RSS feed, group releases, and process any show that is:
          - Already on the NAS but has missing episodes advertised in the RSS
          - Auto-add enabled and not already on the watchlist (config flag)
        """
        from discovery import DiscoveryEngine
        from scanner import scan_tv_library, scan_movie_library

        rss_url = ""
        for src in self.config.get("torrent_sources", []):
            if src.get("enabled") and src.get("rss_feed"):
                rss_url = src["rss_feed"]
                break
        if not rss_url:
            return

        engine   = DiscoveryEngine(rss_url)
        items    = engine.fetch()
        groups   = engine.group_by_title(items)

        # Build NAS inventories from the snapshot (already scanned above)
        tv_inv, mov_inv = [], []
        for lib in self.config.get("libraries", []):
            if not lib.get("enabled", True):
                continue
            if lib.get("type") == "tv":
                tv_inv.append(scan_tv_library(lib.get("path", "")))
            elif lib.get("type") == "movie":
                mov_inv.append(scan_movie_library(lib.get("path", "")))

        enriched = engine.enrich_with_nas(groups, tv_inv, mov_inv)

        # Save discovery cache for the web UI
        engine.save_cache(enriched)

        # Collect all watchlist show names (lower) for quick lookup
        watched_lower = set()
        for lib in self.config.get("libraries", []):
            for s in lib.get("watchlist", []):
                watched_lower.add(s.lower())

        # Process shows that are on NAS but have RSS-advertised missing episodes
        for item in enriched:
            if item["type"] != "tv":
                continue
            if item["status"] != "missing":
                continue
            name = item["name"]
            # Only auto-process if show is already on NAS (we own it)
            if not item.get("on_nas"):
                continue
            logger.info("[Discovery] NAS show '%s' has missing RSS episodes: %s",
                        name, item["missing_eps"])

            # Find the matching TV library
            for lib in self.config.get("libraries", []):
                if lib.get("type") != "tv" or not lib.get("enabled", True):
                    continue
                lib_path = lib.get("path", "")
                inv = scan_tv_library(lib_path)
                from scanner import clean_show_name
                matched = next(
                    (raw for raw in inv if clean_show_name(raw).lower() == name.lower()),
                    None
                )
                if matched:
                    disk = inv[matched]
                    logger.info("[Discovery] Processing missing episodes for '%s'", name)
                    self._process_show(name, lib_path, disk, lib)
                    break

    def run_rss_grab(self) -> None:
        """Lightweight, frequent RSS poll: grab newly-aired episodes of monitored
        (already-on-NAS) shows as soon as they appear in the feed, between the
        full scheduled scans — Sonarr-style "grab within minutes of release".

        Skips entirely when a full scan or the validation watcher is already
        active (the producer rule: only one writer mutates state/queues at a
        time), and arms the download watcher for anything it queues.
        """
        st = load_state()
        if (st.get("scan_status") or {}).get("status") == "running":
            logger.info("[RSS] Poll skipped — a scan/watcher is already active.")
            return

        self.state = st
        # Initialise the per-run accumulators _process_show / discovery expect.
        self._concurrent_watch = False
        self._ignore_cooldown = False
        self._run_tv_queued = {}
        self._run_tv_paths = {}
        self._run_movies_queued = []
        self._run_movie_paths = {}
        self._run_not_found = {}
        self._run_pending_torrents = []
        self._run_validation_keys = set()
        try:
            self._global_show_index = self._build_global_show_index()
        except Exception:
            self._global_show_index = {}

        update_scan_status(self.state, "running", "RSS poll — checking for new episodes…")
        save_state(self.state)
        try:
            self._run_rss_discovery({})
        except Exception as exc:
            logger.warning("[RSS] Poll error (non-fatal): %s", exc)

        grabbed = sum(len(v) for v in self._run_tv_queued.values())
        if self._run_pending_torrents:
            self._arm_download_watcher()
        else:
            update_scan_status(self.state, "idle", "RSS poll complete")
        try:
            self.tvmaze.flush()
        except Exception:
            pass
        save_state(self.state)
        if grabbed:
            logger.info("[RSS] Poll grabbed %d new episode(s).", grabbed)

    def _run_extraction(self) -> None:
        """Extract RAR archives found inside any configured library path."""
        from extractor import scan_and_extract
        lib_paths = self._lib_paths()
        if not lib_paths:
            return
        result = scan_and_extract(lib_paths, delete_rar=True)
        if not result.get("ok"):
            logger.warning("[Extractor] %s", result.get("reason", "extraction unavailable"))
        elif result["extracted"] > 0:
            logger.info("[Extractor] Extracted %d archive(s) this run", result["extracted"])

    def _extract_completed_rars(self, completed: list, lib_paths: list[str]) -> None:
        """Post-download: extract any RAR'd release folders for the torrents that
        just finished, so the video file actually exists before the rename and
        deep-validation steps run. Without this, a RAR'd season pack would leave
        only ``.rar``/``.rNN`` parts on disk — the watcher would find no video,
        treat the episode as missing, and pointlessly re-queue it.

        Runs targeted on just the completed torrents' folders (not a full-library
        walk), is a no-op when no extraction tool is installed, and never deletes
        the RAR parts (NAS files are user-managed — same policy as extractor.py).
        """
        try:
            from extractor import find_extractor, process_folder
        except Exception:
            return

        folders: dict[str, Path] = {}
        for k, t in completed:
            if t is None:
                continue
            cp = t.get("content_path") or t.get("save_path") or ""
            if not cp:
                continue
            p = Path(cp)
            # content_path is the release folder for multi-file torrents and the
            # file itself for single-file torrents. is_file() is reliable here
            # because files are kept on disk (delete_files=False above); release
            # folder names often contain dots, so don't rely on the suffix.
            try:
                base = p.parent if p.is_file() else p
            except OSError:
                base = p
            folders[str(base)] = base
        if not folders:
            return

        tool = find_extractor()
        if not tool:
            logger.debug("[Watcher] RAR found but no extraction tool installed — skipping.")
            return

        for folder in folders.values():
            if not folder.exists():
                continue
            try:
                candidates = [folder] + [d for d in folder.iterdir() if d.is_dir()]
            except OSError:
                candidates = [folder]
            for cand in candidates:
                try:
                    n = process_folder(cand, tool, delete_rar=False)
                    if n:
                        logger.info("[Watcher] Auto-extracted %d RAR archive(s) in %s", n, cand.name)
                except Exception as exc:
                    logger.debug("[Watcher] RAR extraction error in %s: %s", cand, exc)

    def _run_rename(self) -> None:
        """
        Rename scene-style video filenames to clean Show Name - SxxExx format.
        First removes completed/seeding torrents from qBittorrent so the files
        are no longer locked, then renames and flattens release subfolders,
        then cleans up junk files (.nfo, .sfv, Screens folders, etc.).
        """
        from renamer import rename_video_files, cleanup_library, VIDEO_EXTENSIONS, _extract_se, _make_clean_name, _clean_show_name
        import re as _re

        import os as _os
        # ── Step 1: Remove all 100% complete/seeding torrents to release file locks ──
        to_remove_hashes: list[str] = []
        incomplete_paths: set[str] = set()
        try:
            all_torrents = self.qbit.get_torrents()

            # 1a. Completed (100%) torrents
            for t in all_torrents:
                if t.get("progress", 0) >= 1.0:
                    to_remove_hashes.append(t["hash"])
                else:
                    # Still downloading — record its on-disk location so the
                    # renamer NEVER moves a partially-downloaded file out of its
                    # release folder. We only flatten fully-complete downloads.
                    for key in ("content_path", "save_path"):
                        p = t.get(key) or ""
                        if p:
                            incomplete_paths.add(_os.path.normcase(_os.path.normpath(p)))

            # 1b. Duplicate torrents: the target clean file already exists for this torrent.
            #     This catches re-queued episodes that were already renamed from a prior run.
            for t in all_torrents:
                if t["hash"] in to_remove_hashes:
                    continue
                save_path = t.get("save_path", "")
                torrent_name = t.get("name", "")
                # Extract SxxExx from torrent name
                m = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", torrent_name)
                if not m:
                    continue
                season, episode = int(m.group(1)), int(m.group(2))
                # Derive what show_name would be from the save_path folder structure
                # save_path is like \\NAS\lib\Show Name\Season X
                save_dir = Path(save_path)
                show_dir = save_dir.parent  # one level up from season folder
                show_name_guess = _clean_show_name(show_dir.name)
                # Check if the clean file already exists
                clean_name = f"{show_name_guess} - S{season:02d}E{episode:02d}.mkv"
                clean_path = save_dir / clean_name
                if clean_path.exists():
                    logger.info(
                        "[Renamer] Duplicate torrent detected (clean file exists): %s → removing from qBit",
                        torrent_name,
                    )
                    to_remove_hashes.append(t["hash"])

            if to_remove_hashes:
                unique_hashes = list(dict.fromkeys(to_remove_hashes))
                for h in unique_hashes:
                    self.qbit.delete_torrent(h, delete_files=False)
                logger.info(
                    "[Renamer] Released %d torrent(s) from qBittorrent before rename",
                    len(unique_hashes),
                )
                # Give Windows time to fully release file handles
                time.sleep(8)
        except Exception as e:
            logger.warning("[Renamer] Could not release torrents from qBit: %s", e)

        lib_paths = self._lib_paths()
        if not lib_paths:
            return

        if incomplete_paths:
            logger.info(
                "[Renamer] Skipping %d still-downloading torrent path(s) — only complete files will be flattened",
                len(incomplete_paths),
            )
        result = rename_video_files(lib_paths, skip_paths=incomplete_paths)
        if result["renamed"] > 0:
            logger.info("[Renamer] Renamed %d file(s) this run", result["renamed"])

        # NOTE: Post-download validation no longer happens here. Because torrents
        # download asynchronously, they usually aren't finished by the time the
        # run reaches this point. Validation is now owned by the background
        # download watcher (see _validate_run_downloads / watch_downloads), which
        # waits for THIS run's torrents to actually complete before checking
        # them with a full end-to-end decode. Auto-cleanup is also disabled —
        # duplicates are prevented at the source by the scanner.

    # ------------------------------------------------------------------
    # Per-episode re-search / re-queue (used by the validate-as-you-go watcher)
    # ------------------------------------------------------------------

    def _find_or_make_season_path(
        self, show_name: str, season_num: int, lib_paths: list[str]
    ) -> str:
        """Locate the existing season folder for a show across ALL libraries,
        or build a sensible new path if the show isn't on disk yet.

        Standalone version of _resolve_show_save_path for the watcher process,
        which has no disk_inventory / global index built up.
        """
        norm = normalise_show_name(show_name)
        for lib in lib_paths:
            root = Path(lib)
            if not root.exists():
                continue
            try:
                for show_dir in root.iterdir():
                    if not show_dir.is_dir():
                        continue
                    if normalise_show_name(show_dir.name) != norm:
                        continue
                    existing = find_existing_season_folder(show_dir, season_num)
                    if existing:
                        return str(existing)
                    zero_pad = detect_season_padding(show_dir)
                    return str(show_dir / make_tv_season_folder(season_num, zero_pad=zero_pad))
            except OSError:
                continue
        base = lib_paths[0] if lib_paths else "."
        return str(
            Path(base)
            / make_tv_show_folder(show_name, None)
            / make_tv_season_folder(season_num, zero_pad=False)
        )

    def _requeue_one_episode(self, state_key: str, lib_paths: list[str]) -> Optional[str]:
        """Re-acquire a single failed/corrupt episode from a DIFFERENT source.

        SOURCE ORDER — SEASON PACKS FIRST (per download policy):
          1. Try a DIFFERENT season pack. Every pack already tried for this
             episode is in state["torrent_blacklist"][state_key], so the finder
             returns a new one. We keep falling through season packs on each
             failure until none is left.
          2. Only once NO untried, adequately-seeded season pack remains do we
             look for the EPISODE specifically — an individual-episode torrent
             that isn't corrupted.
          3. When neither source has an untried release the episode's sources are
             exhausted: return None so the caller skips it and moves on.
        """
        parts = state_key.split("::")
        if len(parts) < 3 or parts[0] != "tv":
            return None
        show_name = parts[1]
        m = re.match(r"S(\d{1,2})E(\d{1,2})", parts[2], re.IGNORECASE)
        if not m:
            return None
        season_num, ep_num = int(m.group(1)), int(m.group(2))

        # 1) SEASON PACK FIRST — try another season download.
        pack_rel = self._queue_season_pack_for_key(
            state_key, show_name, season_num, ep_num, lib_paths
        )
        if pack_rel:
            return pack_rel

        # 2) No untried season pack left → look for the episode specifically.
        blacklist = self.state.get("torrent_blacklist", {}).get(state_key, [])
        logger.info(
            "[Watcher] %s — no untried season pack left; searching for an "
            "individual episode release (tried %d release(s) so far).",
            state_key, len(blacklist),
        )
        result = self.finder.find_tv(show_name, season_num, ep_num, blacklist=blacklist)
        if not result:
            logger.info(
                "[Watcher] %s — no untried individual release either. "
                "Sources exhausted — skipping and moving on.", state_key,
            )
            return None
        if result.seeders < self._min_seeders:
            logger.info(
                "[Watcher] %s — best remaining individual release %r has only %d "
                "seeders (<%d). Sources exhausted — skipping and moving on.",
                state_key, result.name, result.seeders, self._min_seeders,
            )
            return None

        save_path = self._find_or_make_season_path(show_name, season_num, lib_paths)
        if not self._queue_result(result, save_path):
            logger.warning("[Watcher] %s — failed to queue replacement %r", state_key, result.name)
            return None

        dt = self.state.setdefault("downloaded_torrents", {})
        meta = dt.setdefault(state_key, {})
        meta["release"] = result.name
        meta["queued_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        meta.pop("validated_ok", None)
        # Switching to an INDIVIDUAL release — clear any season-pack routing left
        # over from a previous pack attempt so the watcher validates this as a
        # single file instead of trying to surgically import it from a pack.
        meta.pop("pack_release", None)
        record_history(self.state, {
            "type": "tv", "show": show_name, "season": season_num,
            "episode": ep_num, "release": result.name,
            "size_gb": result.size_gb, "seeders": result.seeders,
            "note": "auto-retry (previous release failed validation)",
        })
        logger.info(
            "[Watcher] %s — re-queued a DIFFERENT release: %r (%d seeders)",
            state_key, result.name, result.seeders,
        )
        return result.name

    def _queue_season_pack_for_key(
        self, state_key: str, show_name: str, season_num: int,
        ep_num: int, lib_paths: list[str],
    ) -> Optional[str]:
        """PRIMARY retry source for ONE failed/corrupt episode: re-acquire it
        from a season pack. Re-uses a pack already queued this run for the same
        season instead of grabbing a second copy, and never re-tries a pack
        already blacklisted for this episode (so a pack whose copy of this
        episode is also corrupt can't loop forever — it falls through to the
        next untried pack, and finally to an individual release in
        _requeue_one_episode). Returns the pack release name, or None when no
        untried, adequately-seeded season pack remains."""
        tag = f"S{season_num:02d}E{ep_num:02d}"
        bl = self.state.get("torrent_blacklist", {}).get(state_key, [])
        pw = self.state.setdefault("pack_whitelist", {})

        # Re-use a pack already queued for this show+season this run.
        for prel, info in pw.items():
            if info.get("show") == show_name and info.get("season") == season_num and prel not in bl:
                if tag not in info.setdefault("wanted", []):
                    info["wanted"].append(tag)
                meta = self.state.setdefault("downloaded_torrents", {}).setdefault(state_key, {})
                meta["release"] = prel
                meta["pack_release"] = prel
                meta["queued_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                meta.pop("validated_ok", None)
                logger.info("[Watcher] %s — attaching to already-queued season pack %r", state_key, prel)
                return prel

        pack = self.finder.find_season_pack(show_name, season_num, blacklist=bl)
        if not pack:
            logger.info("[Watcher] %s — no season pack available either. Sources exhausted.", state_key)
            return None
        if pack.seeders < self._min_seeders:
            logger.info(
                "[Watcher] %s — best season pack %r has only %d seeders (<%d). Giving up.",
                state_key, pack.name, pack.seeders, self._min_seeders,
            )
            return None

        save_path = self._find_or_make_season_path(show_name, season_num, lib_paths)
        # Stage outside the library; only validated wanted eps move into save_path.
        staging = self._pack_staging_dir(save_path, pack.name)
        if not self._queue_result(pack, str(staging)):
            logger.warning("[Watcher] %s — failed to queue season pack %r", state_key, pack.name)
            return None

        meta = self.state.setdefault("downloaded_torrents", {}).setdefault(state_key, {})
        meta["release"] = pack.name
        meta["pack_release"] = pack.name
        meta["queued_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        meta.pop("validated_ok", None)
        pw[pack.name] = {
            "show": show_name, "season": season_num,
            "save_path": str(save_path), "staging": str(staging), "wanted": [tag],
        }
        self._track_pending_torrent(pack.name)
        record_history(self.state, {
            "type": "tv", "show": show_name, "season": season_num, "episode": ep_num,
            "release": pack.name, "size_gb": pack.size_gb, "seeders": pack.seeders,
            "note": "auto-retry via season pack (no clean individual release)",
        })
        logger.info(
            "[Watcher] %s — re-queued via season pack %r (%d seeders)",
            state_key, pack.name, pack.seeders,
        )
        return pack.name

    def _try_season_pack_fallback(
        self, show_name: str, season_num: int, eps: list[dict],
        disk_inventory: dict, lib_path: str, tmdb_id, poster_url,
    ) -> bool:
        """Queue a season pack as the source for a specific set of episodes.

        The pack is imported SURGICALLY by the watcher: only the episodes listed
        here are pulled out of it (each deep-validated first), and every other
        file in the pack is discarded — so a numbering mismatch can never create
        duplicates. Used both for the bulk case (most of a season missing) and as
        a per-episode fallback when no individual torrent exists.

        Returns True if a pack was queued.
        """
        if not eps:
            return False
        # Only episodes that aren't already in-flight this run. (Re-process passes
        # after a corrupt episode was removed/quarantined make it "missing" again —
        # those genuinely need a pack; episodes already queued must not be re-queued.)
        needed = [
            ep for ep in eps
            if not is_already_queued(
                self.state, f"tv::{show_name}::S{season_num:02d}E{ep['episode_number']:02d}"
            )
        ]
        if not needed:
            return False
        eps = needed

        pack_state_key = f"tv::{show_name}::S{season_num:02d}::pack"

        # Re-use a pack already queued for this season (still in-flight, whitelist
        # present) instead of downloading the whole pack again — just attach the
        # newly-needed episodes to its import whitelist.
        pw_existing = self.state.get("pack_whitelist", {}) or {}
        existing_prel = next(
            (k for k, v in pw_existing.items()
             if v.get("show") == show_name and v.get("season") == season_num),
            None,
        )
        if existing_prel and not self.dry_run:
            info = pw_existing[existing_prel]
            dt = self.state.setdefault("downloaded_torrents", {})
            for ep in eps:
                ep_tag = f"S{season_num:02d}E{ep['episode_number']:02d}"
                ep_key = f"tv::{show_name}::{ep_tag}"
                if ep_tag not in info.setdefault("wanted", []):
                    info["wanted"].append(ep_tag)
                mark_queued(self.state, ep_key)
                dt[ep_key] = {
                    "release": existing_prel,
                    "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "pack_release": existing_prel,
                }
                self._run_validation_keys.add(ep_key)
                self._publish_pending(ep_key, existing_prel)
            logger.info(
                "[TV] Attached %d ep(s) to already-queued season pack %r: %s",
                len(eps), existing_prel,
                ", ".join(f"S{season_num:02d}E{e['episode_number']:02d}" for e in eps),
            )
            return True

        pack = self.finder.find_season_pack(show_name, season_num)
        if not pack:
            logger.info("[TV] No season pack available for %s S%02d", show_name, season_num)
            return False
        if pack.seeders < self._min_seeders:
            logger.info(
                "[TV] Season pack %r has only %d seeders (min %d) — not using",
                pack.name, pack.seeders, self._min_seeders,
            )
            return False

        save_path = self._resolve_show_save_path(
            show_name, disk_inventory, lib_path, season_num, tmdb_id
        )
        # Download the pack to a staging area OUTSIDE the library; only the
        # validated, wanted episodes get moved into `save_path` on import.
        staging = self._pack_staging_dir(save_path, pack.name)
        if self.dry_run:
            logger.info(
                "[DRY RUN] Would queue season pack %r for %s S%02d (%d ep(s))",
                pack.name, show_name, season_num, len(eps),
            )
            return True
        if not self._queue_result(pack, str(staging)):
            logger.warning("[TV] Failed to queue season pack %r", pack.name)
            return False

        mark_queued(self.state, pack_state_key)
        record_history(self.state, {
            "type": "tv", "show": show_name, "season": season_num, "episode": 0,
            "title": f"Season {season_num} Pack", "release": pack.name,
            "size_gb": pack.size_gb, "seeders": pack.seeders,
            "poster_url": poster_url, "tmdb_id": tmdb_id,
        })
        dt = self.state.setdefault("downloaded_torrents", {})
        wanted: list[str] = []
        for ep in eps:
            ep_num = ep["episode_number"]
            ep_tag = f"S{season_num:02d}E{ep_num:02d}"
            ep_key = f"tv::{show_name}::{ep_tag}"
            mark_queued(self.state, ep_key)
            dt[ep_key] = {
                "release": pack.name,
                "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                # Flag this key as a pack member so the watcher routes it to the
                # surgical importer instead of the generic flatten/rename.
                "pack_release": pack.name,
            }
            wanted.append(ep_tag)
            self._run_tv_queued.setdefault(show_name, []).append(ep_tag)
            self._run_validation_keys.add(ep_key)
            self._publish_pending(ep_key, pack.name)
        # Persist the import whitelist so the watcher (a separate process) knows
        # EXACTLY which episodes are allowed out of this pack.
        pw = self.state.setdefault("pack_whitelist", {})
        pw[pack.name] = {
            "show": show_name,
            "season": season_num,
            "save_path": str(save_path),   # import TARGET = library season folder
            "staging": str(staging),       # where the pack actually downloads
            "wanted": wanted,
        }
        self._run_tv_paths.setdefault(show_name, str(Path(save_path).parent))
        self._track_pending_torrent(pack.name)
        self.notifier.season_filled(show_name, season_num, len(wanted))
        logger.info(
            "[TV] Season pack queued: %r — will import ONLY %d needed ep(s): %s",
            pack.name, len(wanted), ", ".join(wanted),
        )
        time.sleep(3)
        return True

    def _pack_staging_dir(self, target_save_path: str, pack_name: str) -> Path:
        """A staging folder for season-pack downloads that lives OUTSIDE every
        scanned library, so partial/in-progress pack files can never masquerade
        as finished episodes (which would fool missing-detection and pollute the
        library with nested release folders).

        Placed at the target's share/drive root (sibling of the library folders)
        so the eventual move INTO the library season folder stays on the same
        volume — a fast server-side rename rather than a copy.
        """
        sp = Path(target_save_path)
        anchor = Path(sp.anchor) if sp.anchor else sp.parents[-1]
        safe = re.sub(r'[<>:"/\\|?*]+', "_", pack_name).strip()[:120] or "pack"
        return anchor / ".showtv_pack_staging" / safe

    def _safe_replace(self, src: Path, target: Path, existing: Optional[Path]) -> bool:
        """Move a validated `src` to `target`, quarantining any file it displaces
        until the move succeeds (so a failed move never loses the old copy)."""
        import shutil
        from validator import (
            quarantine_broken_file, purge_quarantined_file, restore_quarantined_file,
        )
        quarantined: list = []
        try:
            for victim in {p for p in (existing, target) if p and p.exists()}:
                q = quarantine_broken_file(victim)
                if q:
                    quarantined.append(q)
            shutil.move(str(src), str(target))
            for q in quarantined:
                purge_quarantined_file(q)
            return True
        except OSError as exc:
            logger.warning("[Pack] Could not place %s → %s: %s", src.name, target.name, exc)
            for q in quarantined:
                restore_quarantined_file(q)
            return False

    def _import_pack_group(self, pack_release: str, members: list, lib_paths: list[str]) -> set:
        """Surgically import the wanted episodes out of a completed season pack.

        `members` is a list of (state_key, torrent_dict) that all belong to the
        same pack. ONLY the episodes represented by these member keys are imported
        (every other file in the pack is discarded), and each is deep-validated
        BEFORE being placed — so a numbering mismatch can never create duplicates
        and a corrupt pack episode can never overwrite a good file. Returns the
        set of episode tags (e.g. "S01E03") that imported and passed validation.
        """
        import shutil
        from validator import validate_video_file
        from renamer import (
            _extract_se, _make_clean_name, _find_episode_file,
            _detect_naming_pattern, VIDEO_EXTENSIONS,
        )

        # Derive everything from the member keys + torrent so this works even if
        # the persisted whitelist is unavailable (e.g. members span poll cycles).
        wl = (self.state.get("pack_whitelist", {}) or {}).get(pack_release, {})
        show_name = wl.get("show", "")
        season_num = wl.get("season")
        wanted: set = set()
        for k, _t in members:
            parts = k.split("::")
            if len(parts) >= 3:
                if not show_name:
                    show_name = parts[1]
                m = re.match(r"S(\d{1,2})E(\d{1,2})", parts[2], re.IGNORECASE)
                if m:
                    if season_num is None:
                        season_num = int(m.group(1))
                    wanted.add(f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}")

        save_path = wl.get("save_path", "")
        if not save_path:
            for _k, t in members:
                sp = t.get("save_path") or ""
                if sp:
                    save_path = sp
                    break
        if not (show_name and season_num is not None and wanted and save_path):
            logger.warning("[Pack] Cannot resolve import target for %r — skipping.", pack_release)
            return set()

        season_dir = Path(save_path)
        try:
            season_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        use_titles = _detect_naming_pattern(season_dir.parent) == "with_title"

        # Collect every video file the pack downloaded.
        videos: list[Path] = []
        seen: set[str] = set()
        for _k, t in members:
            cp = t.get("content_path") or t.get("save_path") or ""
            if not cp:
                continue
            p = Path(cp)
            roots = [p] if p.exists() else []
            for root in roots:
                if root.is_dir():
                    for f in root.rglob("*"):
                        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS and str(f) not in seen:
                            seen.add(str(f)); videos.append(f)
                elif root.is_file() and root.suffix.lower() in VIDEO_EXTENSIONS and str(root) not in seen:
                    seen.add(str(root)); videos.append(root)

        if not videos:
            logger.warning("[Pack] %r — no video files found in downloaded content.", pack_release)
            return set()

        imported: set = set()
        pack_roots: set = set()
        for video in videos:
            se = _extract_se(video.name)
            if not se:
                continue
            s, e, end_ep = se
            tag = f"S{s:02d}E{e:02d}"
            if s != season_num or tag not in wanted or tag in imported:
                # Not an episode we asked for — discard so it can't become a dup.
                try:
                    video.unlink()
                except OSError:
                    pass
                continue
            logger.info("[Pack] %s %s — deep-validating pack copy %s", show_name, tag, video.name)
            ok, detail = validate_video_file(video, deep=True, full=True)
            if not ok:
                logger.warning("[Pack] %s %s from pack FAILED validation (%s) — discarding", show_name, tag, detail)
                try:
                    video.unlink()
                except OSError:
                    pass
                continue
            target = season_dir / _make_clean_name(
                show_name, s, e, end_ep, video.suffix.lower(), include_title=use_titles,
            )
            existing = _find_episode_file(season_dir, s, e)
            if self._safe_replace(video, target, existing):
                imported.add(tag)
                logger.info("[Pack] Imported %s %s → %s", show_name, tag, target.name)

        # Clean up leftover pack folders (junk/non-wanted files already removed).
        for _k, t in members:
            cp = t.get("content_path") or ""
            if cp:
                p = Path(cp)
                if p.is_dir() and p != season_dir:
                    pack_roots.add(p)
        # Also tear down the whole staging wrapper for this pack.
        staging = wl.get("staging", "")
        if staging:
            pack_roots.add(Path(staging))
        for root in pack_roots:
            try:
                shutil.rmtree(str(root))
                logger.info("[Pack] Removed leftover pack folder: %s", root.name)
            except Exception:
                pass

        # Whitelist consumed.
        try:
            self.state.get("pack_whitelist", {}).pop(pack_release, None)
        except Exception:
            pass
        return imported

    def _claim_watcher_lock(self, lock_file: Path, hb_file: Path) -> bool:
        """Acquire the exclusive single-watcher lock.

        Returns True if THIS process now owns it. A lock held by a watcher that
        is still writing its heartbeat (within the last 90s) means another
        watcher is alive → return False so this one exits. A stale lock (no fresh
        heartbeat — the previous watcher died without cleaning up) is reclaimed.
        Uses O_CREAT|O_EXCL so the create-or-fail is atomic at the filesystem
        level, closing the spawn-time race two watchers could otherwise slip
        through.
        """
        mypid = str(os.getpid())
        for _ in range(2):
            try:
                fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, mypid.encode())
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                try:
                    if hb_file.exists():
                        age = time.time() - hb_file.stat().st_mtime
                    else:
                        # No heartbeat yet → the holder may have JUST created the
                        # lock and not written its first beat. Fall back to the
                        # lock file's own age so a freshly-created lock isn't
                        # mistaken for stale (which would let a simultaneous
                        # starter steal it and run a second watcher).
                        age = time.time() - lock_file.stat().st_mtime
                    fresh = age < 90
                except Exception:
                    fresh = True  # can't tell → assume alive, don't double-spawn
                if fresh:
                    return False
                # Stale lock from a dead watcher — remove and retry the claim.
                try:
                    lock_file.unlink()
                except Exception:
                    return False
            except Exception:
                # Never block the watcher on an odd lock-file IO error.
                return True
        return True

    def _spawn_download_watcher(self) -> None:
        """Launch a detached subprocess that watches this run's downloads to
        completion, then validates them. Detached so it survives this run
        process exiting."""
        try:
            import subprocess
            # Single-instance guard: if a watcher wrote a heartbeat in the last
            # 90s it's still alive — don't spawn a second one that would fight it
            # over the same torrents/files.
            hb = Path(__file__).parent / "watcher.heartbeat"
            try:
                if hb.exists() and (time.time() - hb.stat().st_mtime) < 90:
                    logger.info("[Watcher] Already running (recent heartbeat) — not spawning another.")
                    self._watcher_spawned = True
                    return
            except Exception:
                pass
            cmd = [sys.executable, str(Path(__file__).resolve()), "--watch-downloads"]
            # A DETACHED_PROCESS has NO console, so sys.stdout/stderr are None —
            # which breaks the logging StreamHandler setup at import time and
            # silently kills the watcher. Redirect both to a logfile so the
            # process has valid handles AND we can inspect what it did.
            log_path = Path(__file__).parent / "watcher.log"
            log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
            kwargs: dict = {
                "cwd": str(Path(__file__).parent),
                "stdout": log_fh,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
            }
            if os.name == "nt":
                kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(cmd, **kwargs)
            self._watcher_spawned = True
            logger.info("[Watcher] Spawned post-download validation watcher (logs → watcher.log).")
        except Exception as exc:
            logger.warning("[Watcher] Could not spawn download watcher: %s", exc)

    def _publish_pending(self, state_key: str, release: str) -> None:
        """Incrementally publish a freshly-queued episode to pending_validation
        and, on the first call this run, spawn the watcher so it manages and
        validates downloads CONCURRENTLY with a long search/queue phase instead
        of only after it finishes. Marks scan_active=True so the watcher won't
        exit while we're still queuing more."""
        if self.dry_run or not self._concurrent_watch:
            return
        pv = self.state.setdefault("pending_validation", {})
        pv.setdefault("keys", [])
        pv.setdefault("torrents", [])
        pv.setdefault("started_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        pv["scan_active"] = True
        if state_key not in pv["keys"]:
            pv["keys"].append(state_key)
        if release and release not in pv["torrents"]:
            pv["torrents"].append(release)
        save_state(self.state)
        if not self._watcher_spawned:
            self._spawn_download_watcher()

    # Active qBittorrent states that mean a torrent is still working toward
    # completion (we keep waiting on these). Anything else that isn't complete
    # is treated as failed/stalled and skipped.
    _ACTIVE_DL_STATES = {
        "downloading", "forcedDL", "metaDL", "forcedMetaDL",
        "stalledDL", "checkingDL", "queuedDL", "allocating", "checkingResumeData",
        "moving",
    }
    _ERROR_DL_STATES = {"error", "missingFiles", "unknown"}

    def watch_downloads(self) -> None:
        """
        Background worker (run as `downloader.py --watch-downloads`).

        VALIDATE-AS-YOU-GO pipeline. For every episode this run queued
        (state["pending_validation"]["keys"]) it independently drives:

            download → (on 100%) move the mkv into the season folder + delete the
            release folder → FULL end-to-end decode → PASS (mark validated, done)
            or FAIL (delete the bad file, blacklist that release, queue a
            DIFFERENT torrent, keep watching) … repeating until the episode
            passes or no untried release is left (sources exhausted).

        Downloads still run in parallel (qBit's queue); we just validate each one
        the instant it finishes and auto-retry only the ones that fail — so the
        user never has to babysit a "delete-everything" popup for fresh
        downloads. Episodes that exhaust all sources are reported at the end.
        """
        from validator import (
            validate_video_file, remove_broken_files, _path_for_state_key,
            restore_quarantined_file, purge_quarantined_file,
        )
        from renamer import rename_video_files

        POLL_SEC      = 30
        STALL_MINUTES = 8           # seeded-but-slow: no % progress this long → reconnect
        NUDGE_GRACE_SEC = 5 * 60    # after a reannounce/resume, give it this long to recover
        DEAD_STALL_MIN  = 3         # 0 connectable seeds + 0 speed → reconnect fast
        DEAD_GRACE_SEC  = 3 * 60    # …and give up + retry fast if reconnect didn't help
        # Anti-zombie floor: a torrent that's still under ABS_FLOOR_PROGRESS after
        # ABS_FLOOR_MIN minutes of total download time is hopeless even if it
        # micro-crawls a few hundredths of a percent per grace window (which would
        # otherwise reset the "slow but advancing" patience forever). Give up and
        # try a different release instead of stalling the whole run on one peer.
        ABS_FLOOR_PROGRESS = 0.20   # 20%
        ABS_FLOOR_MIN      = 40     # minutes since the download first started
        ABS_CAP_SEC   = 36 * 3600   # hard safety cap so the watcher can never zombie
        MISSING_LIMIT = 4           # polls a queued torrent may be absent before we re-queue
        RECHECK_TIMEOUT_SEC = 30 * 60  # max wait for a 100%-completeness recheck to finish

        # qBittorrent states meaning a recheck (piece re-hash) is in progress.
        CHECKING_STATES = {"checkingUP", "checkingDL", "checkingResumeData", "checking"}

        self.state = load_state()
        pending = self.state.get("pending_validation") or {}
        keys    = list(pending.get("keys") or [])
        scan_active = bool(pending.get("scan_active"))
        if not keys and not scan_active:
            logger.info("[Watcher] Nothing pending to validate — exiting.")
            return

        lib_paths = self._lib_paths()

        # Heartbeat file — lets a concurrent run detect this watcher is alive and
        # avoid spawning a duplicate that would fight us over the same torrents.
        HB_FILE = Path(__file__).parent / "watcher.heartbeat"
        LOCK_FILE = Path(__file__).parent / "watcher.lock"
        def _beat() -> None:
            try:
                HB_FILE.write_text(str(time.time()))
            except Exception:
                pass

        # ── Single-instance lock ────────────────────────────────────────────
        # Several producers can race to spawn a watcher (the scheduled run, the
        # manual "Download Missing" button, and the RSS poll). Only ONE may
        # manage the shared torrents / state / quarantine at a time — two would
        # fight each other (double-delete, blacklist thrash, restore wars). The
        # heartbeat guard at spawn time has a race window before the first beat
        # is written, so claim an exclusive on-disk lock here too: if a live
        # watcher already holds it, exit immediately.
        if not self._claim_watcher_lock(LOCK_FILE, HB_FILE):
            logger.info("[Watcher] Another watcher already holds the lock — exiting.")
            return
        import atexit
        atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))

        # ── Background heartbeat ────────────────────────────────────────────
        # A full end-to-end validation of a large season pack can block the main
        # loop for several minutes — far longer than the 90s staleness window the
        # lock/spawn guards use. If the only heartbeat write is at the top of the
        # 30s poll, a long validation batch lets the heartbeat go stale, a
        # producer concludes this watcher died, and a SECOND watcher reclaims the
        # lock and runs alongside us. A daemon thread beats every 15s so liveness
        # reflects the PROCESS being alive, not how busy the loop currently is.
        import threading
        _hb_stop = threading.Event()
        def _heartbeat_loop() -> None:
            while not _hb_stop.wait(15):
                _beat()
        _hb_thread = threading.Thread(
            target=_heartbeat_loop, name="watcher-heartbeat", daemon=True
        )
        _hb_thread.start()
        atexit.register(_hb_stop.set)

        def _stop_heartbeat() -> None:
            """Stop the background beat so it can't keep rewriting the heartbeat
            file after this watcher exits (a stale-but-fresh-looking heartbeat
            would mislead monitoring and block the next watcher's lock claim)."""
            _hb_stop.set()
            if _hb_thread.is_alive():
                _hb_thread.join(timeout=2)

        def _norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]", "", (s or "").lower())

        # Per-episode tracker.  status: waiting | passed | exhausted
        track: dict[str, dict] = {}

        def _ingest_keys(klist: list) -> int:
            """Add any keys not yet tracked (used both at startup and to pick up
            episodes queued AFTER the watcher started, during a concurrent
            catch-up). Returns how many new 'waiting' episodes were added."""
            dt0 = self.state.get("downloaded_torrents", {})
            added = 0
            for k in klist:
                if k in track:
                    continue
                meta = dt0.get(k) or {}
                rel = meta.get("release", "")
                if meta.get("validated_ok"):
                    track[k] = {"release": rel, "status": "passed", "attempts": 1, "missing": 0}
                else:
                    track[k] = {"release": rel, "status": "waiting", "attempts": 1, "missing": 0}
                    added += 1
                    logger.info("[Watcher] Tracking episode: %s", k)
            return added

        _beat()
        _ingest_keys(keys)
        logger.info(
            "[Watcher] Validate-as-you-go: tracking %d episode(s) (scan_active=%s).",
            len(track), scan_active,
        )
        start = time.time()

        def _ws_save() -> None:
            """save_state, but never clobber pending_validation keys/scan_active
            that the concurrent producer may have appended since we loaded state."""
            try:
                disk_pv = load_state().get("pending_validation")
                if disk_pv is not None:
                    cur = self.state.get("pending_validation") or {}
                    merged = dict(disk_pv)
                    merged["keys"] = sorted(set(disk_pv.get("keys", [])) | set(cur.get("keys", [])))
                    merged["torrents"] = sorted(set(disk_pv.get("torrents", [])) | set(cur.get("torrents", [])))
                    self.state["pending_validation"] = merged
            except Exception:
                pass
            save_state(self.state)

        def _show_dir_for_torrent(t: dict) -> Optional[str]:
            sp = t.get("save_path") or t.get("content_path") or ""
            return str(Path(sp).parent) if sp else None

        def _handle_failure(k: str, file_path) -> None:
            """Delete the bad file (if any), blacklist the failed release, and
            queue a DIFFERENT torrent. Updates track[k] in place."""
            rel = track[k]["release"]
            if file_path is not None:
                try:
                    remove_broken_files([str(file_path)], state=self.state, library_paths=lib_paths)
                except Exception as exc:
                    logger.warning("[Watcher] %s — removal error: %s", k, exc)
            # Ensure the release is blacklisted even when no file ever landed
            # (dead/errored torrent) so the re-search can't pick it again.
            if rel:
                bl = self.state.setdefault("torrent_blacklist", {}).setdefault(k, [])
                if rel not in bl:
                    bl.append(rel)
            _ws_save()

            new_rel = self._requeue_one_episode(k, lib_paths)
            if new_rel:
                track[k].update(release=new_rel, status="waiting",
                                attempts=track[k]["attempts"] + 1, missing=0)
                # Reset ALL per-attempt transient state so the fresh release
                # starts with a clean slate. Without this the new torrent
                # inherits the previous release's stall timers (nudged_at /
                # stall_progress) and gets blacklisted within seconds — before it
                # can even fetch metadata — wrongly exhausting viable replacements.
                # The completeness-gate flags must reset too, or a retry could
                # skip the on-disk recheck.
                for _tk in ("stall_progress", "stall_progress_at", "nudged_at",
                            "recheck_at", "verified_complete"):
                    track[k].pop(_tk, None)
            else:
                track[k]["status"] = "exhausted"
                # Every source exhausted → restore the quarantined original so
                # the user keeps the (broken) episode instead of losing it.
                qpath = load_state().get("quarantine", {}).get(k)
                if qpath:
                    restore_quarantined_file(qpath)
                    update_quarantine(k, remove=True)
            _ws_save()

        while True:
            self.state = load_state()
            _beat()
            # Pick up episodes queued AFTER we started (concurrent catch-up) and
            # re-read whether the producer is still searching/queuing.
            pend = self.state.get("pending_validation") or {}
            scan_active = bool(pend.get("scan_active"))
            _ingest_keys(pend.get("keys") or [])
            try:
                torrents = self.qbit.get_torrents()
            except Exception as exc:
                logger.warning("[Watcher] qBit query failed: %s — retrying", exc)
                torrents = []
            now = time.time()

            by_norm: dict[str, dict] = {}
            for t in torrents:
                by_norm[_norm(t.get("name", ""))] = t

            # ── Phase 1: classify every waiting key (no slow disk work here) ──
            completed: list[tuple] = []   # (key, torrent) pairs that hit 100%
            for k, info in list(track.items()):
                if info["status"] != "waiting":
                    continue
                rel = info["release"]
                rn  = _norm(rel)
                t = by_norm.get(rn)
                if t is None and rn:
                    for tn, tt in by_norm.items():
                        if tn and (rn in tn or tn in rn):
                            t = tt
                            break

                if t is None:
                    # Torrent not in qBit. CRITICAL: it may have already completed
                    # in a PRIOR watcher run (file is on disk, torrent was deleted) —
                    # in that case validate the existing file rather than wrongly
                    # assuming it never downloaded and re-queuing it. Only when no
                    # file exists do we treat it as "never took" and re-queue.
                    existing = _path_for_state_key(k, lib_paths)
                    if existing is not None and existing.exists():
                        info["missing"] = 0
                        completed.append((k, None))  # validate-only (already on disk)
                        continue
                    info["missing"] += 1
                    if info["missing"] >= MISSING_LIMIT:
                        logger.info("[Watcher] %s — release %r never appeared in qBit; re-queuing.", k, rel)
                        _handle_failure(k, None)
                    continue
                info["missing"] = 0

                progress = t.get("progress", 0) or 0
                tstate   = t.get("state", "")

                if tstate in self._ERROR_DL_STATES:
                    logger.info("[Watcher] %s — torrent errored (%s); blacklisting + retrying.", k, tstate)
                    try:
                        self.qbit.delete_torrent(t["hash"], delete_files=True)
                    except Exception:
                        pass
                    _handle_failure(k, None)
                    continue

                if progress < 1.0:
                    # qBit is holding this torrent in its OWN download queue
                    # (max_active_downloads reached — common when several season
                    # packs are queued at once). It isn't stalled, it just hasn't
                    # started, so DON'T start the stall clock or it'll be wrongly
                    # blacklisted/deleted before it ever gets a turn. Reset the
                    # baseline so the clock begins fresh once qBit activates it.
                    if tstate == "queuedDL":
                        info.pop("stall_progress", None)
                        info.pop("stall_progress_at", None)
                        info.pop("nudged_at", None)
                        continue

                    swarm_seeds = t.get("num_complete", t.get("num_seeds", 0)) or 0
                    conn_seeds  = t.get("num_seeds", 0) or 0
                    dlspeed     = t.get("dlspeed", 0) or 0

                    # Robust stall detection based on ACTUAL progress %, not dlspeed
                    # or last-activity. A dead-swarm torrent (0 connectable seeds)
                    # can keep trickling bytes from fellow peers — so qBit shows it
                    # "downloading" and last_activity keeps updating — yet it never
                    # gets the pieces only a seed has, sitting at the same % for
                    # hours. Only real % movement counts as progress.
                    if "stall_progress" not in info:
                        # First sighting: baseline the clock off the torrent's own
                        # last-activity so one that's ALREADY been idle for a while
                        # is actioned promptly instead of starting a fresh timer.
                        info["stall_progress"] = progress
                        info["stall_progress_at"] = t.get("last_activity") or t.get("added_on") or now
                        # Never-reset clock for the absolute-progress floor below.
                        info.setdefault("dl_started_at", t.get("added_on") or now)
                    elif progress > info["stall_progress"] + 0.01:   # advanced ≥1%
                        info["stall_progress"] = progress
                        info["stall_progress_at"] = now
                        info.pop("nudged_at", None)
                        continue

                    # A torrent with NO connected seeds and NO download speed is a
                    # dead swarm — give up on it fast rather than waiting the full
                    # patience window we grant genuinely-slow-but-seeded torrents.
                    is_dead = (conn_seeds <= 0 and dlspeed <= 0)
                    stall_after = DEAD_STALL_MIN if is_dead else STALL_MINUTES
                    grace_sec   = DEAD_GRACE_SEC if is_dead else NUDGE_GRACE_SEC

                    stuck_min = (now - info.get("stall_progress_at", now)) / 60
                    if stuck_min < stall_after:
                        continue  # still advancing (or only recently stopped)

                    nudged_at = info.get("nudged_at", 0)
                    if not nudged_at:
                        logger.info(
                            "[Watcher] %s — no %% progress for %.0fm (stuck at %.0f%%, %d connected / %d swarm seeds%s); reannounce + resume.",
                            k, stuck_min, progress * 100, conn_seeds, swarm_seeds,
                            ", dead swarm" if is_dead else "",
                        )
                        try:
                            if progress >= 0.95:
                                # Nearly done — re-verify on-disk pieces in case it's
                                # a checking hiccup rather than a dead swarm.
                                self.qbit.recheck_torrent(t["hash"])
                            self.qbit.reannounce_torrent(t["hash"])
                            self.qbit.resume_torrent(t["hash"])
                        except Exception as exc:
                            logger.debug("[Watcher] %s — nudge error: %s", k, exc)
                        info["nudged_at"] = now
                        info["nudge_progress"] = progress
                        continue

                    if now - nudged_at >= grace_sec:
                        # A large season pack sharing a limited swarm can advance
                        # legitimately at <1% per stall window — too slow to reset the
                        # coarse ≥1% clock, yet it IS downloading from real seeds and
                        # WILL finish. Don't kill it: if there are connectable seeds
                        # AND it moved at all during the grace period, treat it as
                        # slow-but-healthy, re-baseline the clock, and keep waiting.
                        moved = progress - info.get("nudge_progress", progress)
                        total_min = (now - info.get("dl_started_at", now)) / 60
                        floored = (progress < ABS_FLOOR_PROGRESS
                                   and total_min >= ABS_FLOOR_MIN)
                        if conn_seeds > 0 and moved > 0 and not floored:
                            logger.info(
                                "[Watcher] %s — slow but advancing (+%.2f%% during grace, now %.0f%%, %d seeds); staying patient.",
                                k, moved * 100, progress * 100, conn_seeds,
                            )
                            info["stall_progress"] = progress
                            info["stall_progress_at"] = now
                            info.pop("nudged_at", None)
                            info.pop("nudge_progress", None)
                            continue
                        if floored:
                            logger.info(
                                "[Watcher] %s — only %.0f%% after %.0fm of download time (micro-crawling on %d seeds); "
                                "giving up + retrying a different release.",
                                k, progress * 100, total_min, conn_seeds,
                            )
                        else:
                            logger.info(
                                "[Watcher] %s — still stuck at %.0f%% after reconnect; blacklisting + retrying a different release.",
                                k, progress * 100,
                            )
                        try:
                            self.qbit.delete_torrent(t["hash"], delete_files=True)
                        except Exception:
                            pass
                        _handle_failure(k, None)
                    continue  # nudged, waiting out the grace period

                # ── Completeness verification gate (progress >= 1.0) ──────────
                # qBittorrent reports 100% only after every piece's SHA-1 hash
                # verifies, so a genuinely-complete torrent is byte-perfect. But
                # before we MOVE the file out of qBit's management and validate
                # it, force a one-time re-hash of the on-disk data: this catches
                # pieces that were corrupted on disk AFTER download (SMB/NAS write
                # glitches), and the adopted-duplicate-torrent case where qBit may
                # have inherited a stale/partial file. Only once the recheck
                # confirms 100% with nothing left do we trust the file.
                if not info.get("verified_complete"):
                    amount_left = t.get("amount_left", 0) or 0
                    is_checking = tstate in CHECKING_STATES
                    if "recheck_at" not in info:
                        if not is_checking:
                            try:
                                self.qbit.recheck_torrent(t["hash"])
                            except Exception as exc:
                                logger.debug("[Watcher] %s — recheck request failed: %s", k, exc)
                        info["recheck_at"] = now
                        logger.info(
                            "[Watcher] %s — reached 100%%; re-hashing on-disk pieces before trusting the file.", k,
                        )
                        continue
                    if is_checking and (now - info["recheck_at"]) < RECHECK_TIMEOUT_SEC:
                        continue  # recheck still running — wait for it
                    if progress < 1.0 or amount_left > 0:
                        # Recheck found bad/missing pieces → qBit is re-downloading
                        # them. Reset stall tracking so the re-download gets full
                        # patience, and clear the recheck marker so we verify again
                        # once it returns to 100%.
                        logger.warning(
                            "[Watcher] %s — recheck found incomplete/corrupt data (%.0f%%, %d bytes left); "
                            "qBit re-downloading the bad pieces.", k, progress * 100, amount_left,
                        )
                        info.pop("recheck_at", None)
                        info.pop("stall_progress", None)
                        info.pop("stall_progress_at", None)
                        info.pop("nudged_at", None)
                        continue
                    info["verified_complete"] = True
                    logger.info("[Watcher] %s — 100%% verified on disk; proceeding to validate.", k)

                completed.append((k, t))

            # ── Phase 2: release locks, import + validate ───────────────────────
            if completed:
                # Season-pack members are imported surgically (only the wanted
                # episodes, each deep-validated); everything else uses the generic
                # flatten/rename + validate path.
                self.state = load_state()
                dt_now = self.state.get("downloaded_torrents", {})
                pack_groups: dict[str, list] = {}
                singles: list[tuple] = []
                for k, t in completed:
                    prel = (dt_now.get(k) or {}).get("pack_release")
                    if prel and t is not None:
                        pack_groups.setdefault(prel, []).append((k, t))
                    else:
                        singles.append((k, t))

                # Release every completed torrent (keep the files on disk).
                for k, t in completed:
                    if t is None:
                        continue
                    try:
                        self.qbit.delete_torrent(t["hash"], delete_files=False)
                    except Exception as exc:
                        logger.debug("[Watcher] %s — could not release torrent: %s", k, exc)

                # ── Auto-extract RAR'd releases ─────────────────────────────────
                # Some scene packs ship the video inside .rar parts. Extract them
                # now (after releasing qBit's file handles) so the import/rename/
                # validation below finds a real video instead of archive parts.
                try:
                    time.sleep(3)   # let Windows release handles before extracting
                    self._extract_completed_rars(completed, lib_paths)
                except Exception as exc:
                    logger.debug("[Watcher] post-download extraction error: %s", exc)

                # ── Pack groups: surgical import (consumes pack folders first) ──
                if pack_groups:
                    time.sleep(5)   # let Windows release file handles before moving
                    self.state = load_state()
                    for prel, members in pack_groups.items():
                        try:
                            imported = self._import_pack_group(prel, members, lib_paths)
                        except Exception as exc:
                            logger.warning("[Watcher] pack import error for %r: %s", prel, exc)
                            imported = set()
                        for k, t in members:
                            m = re.search(r"S(\d{1,2})E(\d{1,2})", k)
                            tag = f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else ""
                            if tag and tag in imported:
                                dt = self.state.setdefault("downloaded_torrents", {})
                                meta = dt.setdefault(k, {})
                                meta["validated_ok"] = True
                                meta["validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                                meta.pop("pack_release", None)
                                qpath = load_state().get("quarantine", {}).get(k)
                                if qpath:
                                    purge_quarantined_file(qpath)
                                    update_quarantine(k, remove=True)
                                track[k]["status"] = "passed"
                                logger.info("[Watcher] %s — PASSED (imported from season pack %r)", k, prel)
                            else:
                                logger.warning(
                                    "[Watcher] %s — season pack had no clean copy; trying an individual release.", k,
                                )
                                _handle_failure(k, None)
                    _ws_save()

                # ── Singles: generic flatten + per-file deep validation ────────
                show_dirs: set[str] = set()
                for k, t in singles:
                    if t is None:
                        continue
                    sd = _show_dir_for_torrent(t)
                    if sd:
                        show_dirs.add(sd)
                if show_dirs:
                    time.sleep(5)
                    for sd in show_dirs:
                        try:
                            rename_video_files(lib_paths, only_show_dirs={sd})
                        except Exception as exc:
                            logger.warning("[Watcher] rename error for %s: %s", sd, exc)

                self.state = load_state()
                for k, t in singles:
                    file_path = _path_for_state_key(k, lib_paths)
                    if file_path is None:
                        track[k]["missing"] = track[k].get("missing", 0) + 1
                        if track[k]["missing"] >= 2:
                            logger.info("[Watcher] %s — completed but no file found; re-queuing.", k)
                            _handle_failure(k, None)
                        continue

                    logger.info("[Watcher] %s — download complete, deep-validating %s", k, file_path.name)
                    ok, detail = validate_video_file(file_path, deep=True, full=True)
                    if ok:
                        dt = self.state.setdefault("downloaded_torrents", {})
                        meta = dt.setdefault(k, {})
                        meta["validated_ok"] = True
                        meta["validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                        # Replacement verified good → the quarantined broken
                        # original is no longer needed; delete it for real now.
                        qpath = load_state().get("quarantine", {}).get(k)
                        if qpath:
                            purge_quarantined_file(qpath)
                            update_quarantine(k, remove=True)
                        _ws_save()
                        track[k]["status"] = "passed"
                        logger.info("[Watcher] %s — PASSED validation (%s)", k, detail)
                    else:
                        logger.warning("[Watcher] %s — FAILED validation: %s → retrying a different release", k, detail)
                        _handle_failure(k, file_path)

            waiting   = sum(1 for v in track.values() if v["status"] == "waiting")
            passed    = sum(1 for v in track.values() if v["status"] == "passed")
            exhausted = sum(1 for v in track.values() if v["status"] == "exhausted")
            scan_note = " (still searching for more…)" if scan_active else ""
            update_scan_status(
                self.state, "running",
                detail=f"Validating downloads — {passed} ok, {waiting} in progress, {exhausted} unresolved{scan_note}",
            )
            _ws_save()

            # Only exit when there's nothing left to do AND the producer (a
            # concurrent catch-up) has finished queuing. While scan_active we
            # keep polling so newly-queued episodes get picked up & managed.
            if waiting == 0 and not scan_active:
                break
            if now - start > ABS_CAP_SEC:
                logger.warning("[Watcher] Safety cap reached (%dh) — stopping.", ABS_CAP_SEC // 3600)
                break
            time.sleep(POLL_SEC)

        # ── Final report ──────────────────────────────────────────────────────
        self.state = load_state()
        passed_keys = [k for k, v in track.items() if v["status"] == "passed"]
        unresolved  = [k for k, v in track.items() if v["status"] != "passed"]

        # Safety net: any unresolved key that still has a quarantined original
        # (e.g. the loop hit the time cap mid-download) gets its broken file
        # restored so no episode is silently lost.
        diskq = load_state().get("quarantine", {})
        for k in unresolved:
            qpath = diskq.get(k)
            if qpath:
                restore_quarantined_file(qpath)
                update_quarantine(k, remove=True)
        save_state(self.state)

        self._report_watch_results(passed_keys, unresolved)

        self.state.pop("pending_validation", None)
        update_scan_status(self.state, "idle", "Download validation complete")
        save_state(self.state)
        # Stop the background beat FIRST, otherwise it re-creates the heartbeat
        # file 15s after we delete it and keeps signalling "alive" for the rest
        # of the process's life (e.g. while a concurrent producer keeps running).
        _stop_heartbeat()
        try:
            HB_FILE.unlink()
        except Exception:
            pass
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info("[Watcher] Done — %d passed, %d unresolved.", len(passed_keys), len(unresolved))

    def _report_watch_results(self, passed_keys: list, unresolved: list) -> None:
        """Persist + notify the outcome of the validate-as-you-go watcher.

        The Discord summary now lists every episode that installed AND passed deep
        validation (grouped by show, with the NAS folder), so the user can see at a
        glance exactly which new episodes are confirmed working — plus any that
        exhausted all sources.
        """
        from validator import _path_for_state_key

        def _label(k: str) -> str:
            parts = k.split("::")
            return f"{parts[1]} {parts[2]}" if len(parts) >= 3 else k

        def _show_ep(k: str) -> tuple[str, str]:
            parts = k.split("::")
            return (parts[1], parts[2]) if len(parts) >= 3 else (k, "")

        if unresolved:
            self.state["unresolved_downloads"] = {
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "episodes":   [_label(k) for k in sorted(unresolved)],
            }
        else:
            self.state.pop("unresolved_downloads", None)
        # Fresh downloads are auto-handled now, so there is no delete-everything
        # popup to leave behind.
        self.state.pop("pending_download_review", None)
        save_state(self.state)

        # ── Group successful installs by show + resolve each show's NAS folder ──
        lib_paths = self._lib_paths()
        by_show: dict[str, list[str]] = {}
        show_path: dict[str, str] = {}
        for k in passed_keys:
            show, ep = _show_ep(k)
            by_show.setdefault(show, []).append(ep)
            if show not in show_path:
                try:
                    fp = _path_for_state_key(k, lib_paths)
                    if fp:
                        # …\Show\Season N\file  → show folder is two levels up
                        show_path[show] = str(fp.parent.parent)
                except Exception:
                    pass

        try:
            if not (passed_keys or unresolved):
                return
            blocks: list[str] = []
            if passed_keys:
                seg = [f"{len(passed_keys)} episode(s) installed and verified working:", ""]
                for show in sorted(by_show):
                    eps = sorted(set(by_show[show]))
                    seg.append(f"{show} — {len(eps)} episode(s)")
                    if show_path.get(show):
                        seg.append(show_path[show])
                    seg.append(", ".join(eps))
                    seg.append("")
                blocks.append("\n".join(seg).strip())
            if unresolved:
                eps  = ", ".join(_label(k) for k in sorted(unresolved)[:20])
                more = "" if len(unresolved) <= 20 else f" (+{len(unresolved) - 20} more)"
                blocks.append(
                    f"{len(unresolved)} episode(s) had no working release after trying every "
                    f"available torrent: {eps}{more}"
                )
            message = "\n\n".join(blocks)
            if len(message) > 3900:          # Discord embed description hard limit
                message = message[:3900] + "\n… (truncated)"
            color = 0x2ECC71 if passed_keys else 0xE67E22   # green if anything succeeded
            self.notifier.notify("Download validation complete", message, color)
        except Exception as exc:
            logger.debug("[Watcher] Notify failed: %s", exc)

    def _validate_and_fix_paths(self, paths: list[str], full: bool = True) -> dict:
        """Deep-validate every video file under the given UNC path(s). Auto-remove
        any genuinely corrupt EXISTING file, queue a DIFFERENT torrent for it, and
        arm the validate-as-you-go watcher to verify each replacement.

        This is the targeted "check these shows/seasons for corruption and fix
        them" entry point — it never walks the whole NAS, only the paths given.

        full=True  → decode the ENTIRE file (most accurate, slow).
        full=False → sample windows across the file (much faster; still catches
                     the scattered-corruption / stutter class). Best when a path
                     contains hundreds of files, most of which are healthy.
        """
        from validator import (
            validate_video_file, remove_broken_files,
            _state_key_for_file, VIDEO_EXTENSIONS,
            quarantine_broken_file, restore_quarantined_file,
        )

        self.state = load_state()
        lib_paths = self._lib_paths()
        summary: dict = {
            "checked": 0, "broken": 0, "removed": 0, "quarantined": 0, "requeued": 0,
            "exhausted": [], "broken_files": [], "aborted": False,
        }
        # GUARD: never delete-and-replace while the indexer is offline — that
        # only deletes corrupt files we then can't replace. Make sure Jackett is
        # up (auto-restart it if we can) before touching anything.
        if not ensure_jackett_up(self.config):
            logger.error(
                "[FixPaths] Aborting — Jackett indexer is unreachable, so removed "
                "files could not be replaced. Bring it up and re-run."
            )
            update_scan_status(
                self.state, "idle",
                "Aborted: indexer (Jackett) offline — nothing deleted",
            )
            save_state(self.state)
            summary["aborted"] = True
            return summary
        # Verify replacements AS THEY DOWNLOAD rather than all at the very end:
        # spin the validate-as-you-go watcher up the moment the first broken
        # file is re-queued, and feed it each subsequent replacement. This makes
        # confirmed "saved & verified" episodes appear while the scan is still
        # running, instead of after the whole library has been checked.
        self._concurrent_watch = True
        self._ignore_cooldown = True
        self._watcher_spawned = False

        files: list[Path] = []
        for p in paths:
            root = Path(p)
            try:
                if root.is_file() and root.suffix.lower() in VIDEO_EXTENSIONS:
                    files.append(root)
                elif root.is_dir():
                    for f in root.rglob("*"):
                        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                            files.append(f)
            except OSError as exc:
                logger.warning("[FixPaths] Cannot read %s: %s", p, exc)
        total = len(files)
        logger.info(
            "[FixPaths] Deep-validating %d file(s) under %d path(s) (%s decode)…",
            total, len(paths), "full" if full else "sampled",
        )

        requeue_keys: set[str] = set()
        pending_names: list[str] = []

        for idx, f in enumerate(files, 1):
            # Reload so validated_ok stamps the concurrent watcher writes for
            # replacements aren't clobbered by our own save below.
            self.state = load_state()
            update_scan_status(
                self.state, "running",
                detail=f"Checking {idx}/{total} for corruption — {f.name}",
            )
            save_state(self.state)
            ok, detail = validate_video_file(f, deep=True, full=full)
            summary["checked"] += 1
            if ok:
                # Record that this file passed a full-file deep decode so the
                # automatic re-validation pass (below) won't re-decode it every
                # run. Only stamp episodes we actually downloaded/track.
                vkey = _state_key_for_file(f.name)
                if vkey:
                    dt = self.state.setdefault("downloaded_torrents", {})
                    if vkey in dt:
                        dt[vkey]["validated_ok"] = True
                        dt[vkey]["validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                        save_state(self.state)
                continue
            summary["broken"] += 1
            summary["broken_files"].append({"file": str(f), "reason": detail})
            logger.warning("[FixPaths] BROKEN: %s — %s", f.name, detail)

            state_key = _state_key_for_file(f.name)
            self.state = load_state()

            if not state_key:
                # Non-standard name → we can't auto-redownload, so there's no
                # replacement to wait for. Delete as before (nothing to keep it
                # around for) and blacklist via remove_broken_files.
                try:
                    remove_broken_files([str(f)], state=self.state, library_paths=lib_paths)
                    summary["removed"] += 1
                except Exception as exc:
                    logger.warning("[FixPaths] removal error for %s: %s", f.name, exc)
                save_state(self.state)
                logger.info(
                    "[FixPaths] %s — non-standard name, removed but can't auto-redownload.", f.name
                )
                continue

            # ── Quarantine (don't delete yet) ─────────────────────────────────
            # Rename the broken file to a ".corrupt" sidecar so the canonical
            # name is free for a replacement to land, but the original bytes are
            # preserved. We only DELETE it once a replacement passes validation
            # (handled in the watcher); if every source is exhausted we RESTORE
            # it so the user never ends up with a missing episode. We also
            # blacklist the bad release here so the re-search avoids it.
            torrent_name = self.state.get("downloaded_torrents", {}).get(state_key, {}).get("torrent_name")
            if torrent_name:
                bl = self.state.setdefault("torrent_blacklist", {}).setdefault(state_key, [])
                if torrent_name not in bl:
                    bl.append(torrent_name)
            qpath = quarantine_broken_file(f)
            save_state(self.state)  # persists the blacklist edit (quarantine map untouched)
            if qpath:
                update_quarantine(state_key, qpath)  # atomic add — race-safe
                summary["quarantined"] += 1

            new_rel = self._requeue_one_episode(state_key, lib_paths)
            if new_rel:
                requeue_keys.add(state_key)
                pending_names.append(new_rel)
                summary["requeued"] += 1
                # Publish this replacement immediately and spin up the watcher
                # so it starts verifying while we keep scanning the rest.
                self._publish_pending(state_key, new_rel)
            else:
                # No replacement at all → restore the original so we don't lose
                # the episode, and clear the quarantine record.
                if qpath:
                    restore_quarantined_file(qpath)
                    update_quarantine(state_key, remove=True)
                summary["exhausted"].append(state_key)

        # Producer (this scan) is done. Signal the concurrent watcher that no
        # more episodes are coming so it can drain the remaining downloads,
        # verify them, and exit once everything is resolved.
        self.state = load_state()
        pv = self.state.get("pending_validation") or {}
        if requeue_keys or pv.get("keys"):
            pv.setdefault("torrents", [])
            pv.setdefault("keys", [])
            pv["torrents"] = sorted(set(pv["torrents"]) | set(pending_names))
            pv["keys"] = sorted(set(pv["keys"]) | requeue_keys)
            pv["scan_active"] = False
            self.state["pending_validation"] = pv
            update_scan_status(
                self.state, "running",
                detail=f"Verifying {len(pv['keys'])} replacement download(s)…",
            )
            save_state(self.state)
            if not self._watcher_spawned:
                self._spawn_download_watcher()
        else:
            update_scan_status(self.state, "idle", "Targeted validation complete")
            save_state(self.state)

        logger.info(
            "[FixPaths] Done — checked %d, broken %d, removed %d, requeued %d, exhausted %d",
            summary["checked"], summary["broken"], summary["removed"],
            summary["requeued"], len(summary["exhausted"]),
        )
        return summary

    def _revalidate_unverified_downloads(
        self, show_name: str, lib_paths: list
    ) -> None:
        """Deep full-file decode every episode WE downloaded for ``show_name``
        in a prior run that is on disk but was never stamped ``validated_ok``.

        This closes the slip-through gap: episodes grabbed before the full-file
        decode pipeline existed (or resolved via an on-disk shortcut) sit in the
        library un-checked because the scanner counts them as "present" and never
        re-examines them.

        REVIEW-ONLY — NOTHING ON DISK IS TOUCHED WITHOUT YOUR APPROVAL:
        passing files are stamped ``validated_ok`` so they aren't re-decoded
        every run. Broken files that already exist on disk are NOT deleted,
        quarantined or re-queued here — they are surfaced in
        ``pending_download_review`` so the web UI prompts you, and you approve
        the removal + re-download from the confirmation popup. (Only files this
        run freshly downloads can be auto-swapped, and that happens in the
        download watcher, never here.)

        SAFETY: only inspects keys present in ``downloaded_torrents`` (files this
        system grabbed). The user's own pre-existing library is never decoded or
        deleted here.
        """
        from validator import validate_downloaded_grouped, _path_for_state_key

        norm_show = clean_show_name(show_name).lower()
        dt = self.state.get("downloaded_torrents", {})
        candidates: list[str] = []
        for k, meta in dt.items():
            if not k.startswith("tv::"):
                continue
            if meta.get("validated_ok"):
                continue
            parts = k.split("::")
            if len(parts) < 3:
                continue
            if clean_show_name(parts[1]).lower() != norm_show:
                continue
            candidates.append(k)

        if not candidates:
            return

        logger.info(
            "[ReValidate] %s — deep-checking %d previously-downloaded, unverified "
            "episode(s) on disk (review-only — nothing is deleted without your "
            "approval).", show_name, len(candidates),
        )
        update_scan_status(
            self.state, "running",
            detail=f"Re-checking existing {show_name} downloads…",
        )
        save_state(self.state)

        try:
            review = validate_downloaded_grouped(
                lib_paths, state=self.state, only_keys=set(candidates),
                deep=True, full=True,
            )
        except Exception as exc:
            logger.warning(
                "[ReValidate] %s — validation error (non-fatal): %s", show_name, exc,
            )
            return

        broken_keys: set[str] = set()
        for show in review.get("shows", []):
            for season in show.get("seasons", []):
                for bf in season.get("broken_files", []):
                    sk = bf.get("state_key")
                    if sk:
                        broken_keys.add(sk)

        # Stamp the files that PASSED so they aren't re-decoded next run. This is
        # a pure bookkeeping update — no file on disk is modified.
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.state = load_state()
        dt = self.state.setdefault("downloaded_torrents", {})
        for k in candidates:
            if k in broken_keys:
                continue
            fp = _path_for_state_key(k, lib_paths)
            if fp is None or not fp.exists():
                continue
            meta = dt.setdefault(k, {})
            meta["validated_ok"] = True
            meta["validated_at"] = stamp

        broken = review.get("broken", 0)
        if broken > 0:
            # Flag for the user instead of acting. Merge into any review already
            # pending (e.g. a multi-show session) so nothing is overwritten.
            existing = self.state.get("pending_download_review")
            if not isinstance(existing, dict):
                existing = {}
            self.state["pending_download_review"] = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "checked":    existing.get("checked", 0) + review.get("checked", 0),
                "broken":     existing.get("broken", 0) + broken,
                "shows":      existing.get("shows", []) + review.get("shows", []),
            }
            logger.warning(
                "[ReValidate] %s — %d broken existing file(s) flagged for your "
                "approval (NOTHING deleted). Approve removal + re-download in the "
                "Download Review popup.", show_name, broken,
            )
        save_state(self.state)

    def catch_up_show(self, show_path: str) -> None:
        """Catch a SINGLE show up on every missing episode, then arm the
        validate-as-you-go watcher so each new download is verified and auto
        re-tried with a different torrent on failure.

        Reuses the normal per-episode discovery/queue pipeline (_process_show),
        so it honours episodes-only mode, scanner ownership (won't re-grab files
        already on disk) and the overwrite guard. Existing files on disk are
        NEVER deleted by this flow — only genuinely-broken NEW downloads are.
        """
        from scanner import scan_tv_library

        show_dir = Path(show_path)
        if not show_dir.exists():
            logger.error("[CatchUp] Path does not exist: %s", show_path)
            update_scan_status(self.state, "idle", f"Path not found: {show_path}")
            save_state(self.state)
            return
        lib_path  = str(show_dir.parent)
        show_name = clean_show_name(show_dir.name)

        # Per-run accumulators that _process_show writes into.
        self._run_tv_queued = {}
        self._run_tv_paths = {}
        self._run_movies_queued = []
        self._run_movie_paths = {}
        self._run_not_found = {}
        self._run_pending_torrents = []
        self._run_validation_keys = set()
        # Manage/validate downloads concurrently with the (often long) search
        # phase so stalled torrents are nudged/replaced immediately rather than
        # sitting idle until every season has been searched. Ignore the retry
        # backoff so every still-missing episode is attempted now.
        self._concurrent_watch = True
        self._ignore_cooldown = True

        update_scan_status(self.state, "running", f"Catching up {show_name}…")
        save_state(self.state)

        try:
            self.qbit.ensure_queue_settings()
            self.qbit.resume_incomplete_paused()
        except Exception as exc:
            logger.warning("[CatchUp] queue settings (non-fatal): %s", exc)
        try:
            self._recover_errored_torrents()
        except Exception:
            pass

        self._global_show_index = self._build_global_show_index()

        # Deep re-validate anything WE downloaded for this show in a prior run
        # that was never full-file verified (the gap that let S22E05 slip
        # through). Broken ones are removed here so the scan below treats them
        # as missing and queues a fresh release automatically.
        try:
            self._revalidate_unverified_downloads(show_name, self._lib_paths())
        except Exception as exc:
            logger.warning("[CatchUp] re-validation pass error (non-fatal): %s", exc)

        logger.info("[CatchUp] Scanning %s for %r…", lib_path, show_name)
        disk_inventory = scan_tv_library(lib_path)

        self._process_show(show_name, lib_path, disk_inventory)

        queued = len(self._run_validation_keys)
        not_found = sum(len(v) for v in self._run_not_found.values())
        logger.info(
            "[CatchUp] %s — queued %d missing episode(s); %d had no release.",
            show_name, queued, not_found,
        )

        if not self._arm_download_watcher():
            update_scan_status(
                self.state, "idle",
                f"{show_name}: nothing to download (already complete or all on cooldown)",
            )
            save_state(self.state)
            logger.info("[CatchUp] %s — nothing queued.", show_name)

    def _wait_for_review_decision(self, show_name: str, has_auto_flags: bool = False,
                                  poll_sec: int = 8) -> str:
        """Block until the user signs off on this series (manual-validation gate).

        Always waits for an EXPLICIT decision recorded in
        ``state["review_decision"]`` — set either by the dashboard popup
        (api_remove_broken / download-review-clear) or by ``campaign_review.py``
        when the user reports their manual-validation results in chat:

          "redownload" — corrupt episode(s) were found (by our validator OR by the
                         user) and have been removed + blacklisted, so re-download
                         with a DIFFERENT release and re-validate, STAYING here.
          "confirm"    — the user confirmed the series is good; move to the next.

        It never auto-advances on its own: the campaign halts here until the user
        acts. That is the whole point of per-series manual validation.
        """
        st = load_state()
        if has_auto_flags:
            detail = (f"{show_name}: validator flagged corrupt episode(s) AND it's "
                      f"ready for your manual validation — confirm 'next' or report "
                      f"bad episodes.")
        else:
            detail = (f"{show_name}: downloaded & auto-validated clean — ready for "
                      f"your manual validation. Confirm 'next' or report bad episodes.")
        update_scan_status(st, "running", detail=detail, show_name=show_name)
        # Compact marker the assistant / UI can read at a glance.
        st["awaiting_series_review"] = {
            "show":         show_name,
            "auto_flagged": bool(has_auto_flags),
            "since":        datetime.now().isoformat(timespec="seconds"),
        }
        save_state(st)
        logger.info(
            "[Campaign] %s — BLOCKING for your manual validation (auto_flagged=%s). "
            "Waiting for 'confirm' (next) or 'redownload' (fix episodes)…",
            show_name, has_auto_flags,
        )
        while True:
            time.sleep(poll_sec)
            st = load_state()
            dec = st.get("review_decision")
            if isinstance(dec, dict) and dec.get("action") in ("redownload", "confirm"):
                action = dec["action"]
                st.pop("review_decision", None)
                st.pop("awaiting_series_review", None)
                save_state(st)
                logger.info("[Campaign] %s — your decision: %s.", show_name, action)
                return action

    def catch_up_series_campaign(
        self, only_library_types: Optional[set] = None,
        block_for_review: bool = True,
        start_at: Optional[str] = None,
        library_path: Optional[str] = None,
    ) -> None:
        """Per-series catch-up across the selected libraries, one show at a time.

        For each show, in order:
          1. Download any missing episodes and validate each fresh download
             inline (auto-retry a different release on corruption, move on when
             a source is exhausted) — never touches existing files.
          2. Re-validate our PRIOR downloads for the show (review-only): any
             corrupt existing file is flagged into ``pending_download_review``,
             NOT deleted.
          3. If anything was flagged and ``block_for_review`` is set, WAIT for
             the user's decision in the Download Review popup:
               • approve corrupt episode(s) → they're removed + blacklisted, and
                 we STAY on this series, re-downloading them with a DIFFERENT
                 release and re-validating (repeat until clean or confirmed);
               • dismiss/confirm → the series is acceptable, move to the next one.

        ``only_library_types`` filters which libraries are walked (e.g.
        {"animation"} for the cartoon libraries). None = all TV-style libraries.
        """
        from scanner import scan_tv_library, clean_show_name

        lib_paths = self._lib_paths()
        try:
            self.qbit.ensure_queue_settings()
            self.qbit.resume_incomplete_paused()
        except Exception as exc:
            logger.warning("[Campaign] queue settings (non-fatal): %s", exc)
        self._global_show_index = self._build_global_show_index()

        tv_types = {"tv", "animation"}

        def _norm_path(p: str) -> str:
            return (p or "").replace("/", "\\").rstrip("\\").lower()

        want_path = _norm_path(library_path) if library_path else None
        libs = [
            l for l in self.config.get("libraries", [])
            if l.get("enabled", True)
            and l.get("type") in tv_types
            and (only_library_types is None or l.get("type") in only_library_types)
            and (want_path is None or _norm_path(l.get("path", "")) == want_path)
        ]
        if want_path is not None and not libs:
            logger.warning("[Campaign] --library-path %r matched no enabled TV library.", library_path)
            return

        # Build the ordered (lib_path, show_raw, inventory) work list.
        work: list[tuple] = []
        for lib in libs:
            lp = lib.get("path", "")
            if not lp:
                continue
            inv = scan_tv_library(lp)
            for show_raw in sorted(inv, key=lambda s: s.lower()):
                work.append((lp, show_raw, inv))

        # Optional resume point: skip everything before the first show whose
        # (cleaned) name contains `start_at` (case-insensitive).
        if start_at:
            needle = start_at.lower()
            start_idx = next(
                (i for i, (_, sr, _) in enumerate(work)
                 if needle in clean_show_name(sr).lower()),
                None,
            )
            if start_idx is None:
                logger.warning("[Campaign] --start-at %r matched no show; running all.",
                               start_at)
            else:
                skipped = work[:start_idx]
                work = work[start_idx:]
                logger.info("[Campaign] --start-at %r → starting at %r (skipped %d earlier show(s)).",
                            start_at, clean_show_name(work[0][1]), len(skipped))

        total = len(work)
        logger.info(
            "[Campaign] Per-series catch-up over %d show(s) across %d librar(ies) "
            "(block_for_review=%s).", total, len(libs), block_for_review,
        )

        # Mark the campaign active so the web endpoints route review actions back
        # to us (approve corrupt → re-download here; dismiss → next series)
        # instead of spawning a separate, conflicting run.
        self.state = load_state()
        self.state["campaign_active"] = True
        self.state.pop("review_decision", None)
        save_state(self.state)

        try:
            for idx, (lp, show_raw, inv) in enumerate(work, 1):
                show_name = clean_show_name(show_raw)
                logger.info("[Campaign] ── %d/%d: %s ──", idx, total, show_name)

                # Throttle: before starting this series, wait until qBittorrent's
                # in-flight queue has drained below the cap. This is what makes
                # the campaign work a couple of series at a time — download +
                # validate the current batch, let it clear, THEN pull the next —
                # instead of dumping the whole library into the queue at once.
                self._wait_for_queue_capacity(idx, total)

                # Per-series loop: download + validate, then re-validate our prior
                # downloads. If corruption is flagged and the user approves a fix,
                # we STAY on this series — re-download the corrupt episode(s) with a
                # DIFFERENT release and re-validate — repeating until nothing is
                # corrupt or the user confirms it's good enough to move on.
                attempt = 0
                while True:
                    attempt += 1
                    update_scan_status(
                        self.state, "running",
                        detail=(f"[{idx}/{total}] {show_name} — downloading & "
                                f"validating"
                                + (f" (pass {attempt})" if attempt > 1 else "") + "…"),
                    )
                    save_state(self.state)

                    # Start each pass with a CLEAN review slate so the broken-count
                    # read below reflects ONLY this show's files. _revalidate_unverified
                    # _downloads MERGES into any existing review, so a previous
                    # series' flags (or a prior run's leftovers, e.g. a Steven
                    # Universe review still sitting in state) would otherwise be
                    # miscounted as THIS show's corruption and falsely block it.
                    st = load_state()
                    if st.pop("pending_download_review", None) is not None:
                        save_state(st)
                    self.state = st

                    # Fresh inventory each pass (files may have been removed for
                    # re-download since the work list was built).
                    try:
                        inv = scan_tv_library(lp)
                    except Exception as exc:
                        logger.warning("[Campaign] %s — rescan error: %s", show_name, exc)

                    # Reset per-show accumulators; run watcher INLINE (no subprocess).
                    self._run_tv_queued = {}
                    self._run_tv_paths = {}
                    self._run_movies_queued = []
                    self._run_movie_paths = {}
                    self._run_not_found = {}
                    self._run_pending_torrents = []
                    self._run_validation_keys = set()
                    self._concurrent_watch = False
                    self._ignore_cooldown = True
                    self._watcher_spawned = True   # block _arm_download_watcher

                    # 1. Discover + queue missing episodes (incl. ones just removed
                    #    for re-download — the blacklist forces a DIFFERENT release).
                    try:
                        self._process_show(show_name, lp, inv)
                    except Exception as exc:
                        logger.warning("[Campaign] %s — discovery error: %s", show_name, exc)

                    # 2. Validate fresh downloads inline (auto-retry / move on).
                    if self._run_pending_torrents:
                        queued = len(self._run_validation_keys)
                        not_found = sum(len(v) for v in self._run_not_found.values())
                        logger.info(
                            "[Campaign] %s — queued %d episode(s); %d had no "
                            "release. Validating downloads…",
                            show_name, queued, not_found,
                        )
                        try:
                            self._arm_download_watcher()  # sets pending_validation
                            self.watch_downloads()        # blocks until resolved
                        except Exception as exc:
                            logger.warning("[Campaign] %s — watcher error: %s", show_name, exc)

                    # 3. Re-validate our PRIOR downloads for this show (review-only).
                    try:
                        self._revalidate_unverified_downloads(show_name, lib_paths)
                    except Exception as exc:
                        logger.warning("[Campaign] %s — re-validate error: %s", show_name, exc)

                    review = load_state().get("pending_download_review")
                    nbroken = review.get("broken", 0) if isinstance(review, dict) else 0
                    if nbroken:
                        logger.warning(
                            "[Campaign] %s — %d corrupt episode(s) auto-flagged.",
                            show_name, nbroken,
                        )
                    else:
                        logger.info("[Campaign] %s — auto-validation clean.", show_name)

                    if not block_for_review:
                        # Non-blocking: leave any review for the user and move on.
                        break

                    # 4. ALWAYS block for the user's manual validation of this
                    #    series — even when auto-validation found nothing — so the
                    #    user can catch issues our validator misses and report them.
                    action = self._wait_for_review_decision(show_name, has_auto_flags=bool(nbroken))
                    if action == "redownload":
                        # api_remove_broken already deleted + blacklisted the
                        # selected episode(s); loop to pull a DIFFERENT release
                        # and re-validate — staying on this series.
                        logger.info(
                            "[Campaign] %s — re-downloading the flagged episode(s) "
                            "with a different release…", show_name,
                        )
                        continue
                    # "confirm" (popup dismissed) → episodes acceptable; move on.
                    st = load_state()
                    st.pop("pending_download_review", None)
                    save_state(st)
                    logger.info(
                        "[Campaign] %s — confirmed working; moving to the next series.",
                        show_name,
                    )
                    break
        finally:
            self.state = load_state()
            self.state.pop("campaign_active", None)
            self.state.pop("review_decision", None)
            self.state.pop("awaiting_series_review", None)
            save_state(self.state)

        update_scan_status(self.state, "idle", "Per-series catch-up complete")
        save_state(self.state)
        logger.info("[Campaign] Per-series catch-up complete (%d show(s)).", total)

    def _validate_run_downloads(self, only_keys: set, lib_paths: Optional[list] = None) -> None:
        """
        Validate ONLY the episodes installed by this run (only_keys), with a full
        end-to-end decode, and write a dry-run report to
        state["pending_download_review"] for the web UI confirmation popup.

        Nothing is deleted here — the user confirms removals in the popup.
        """
        if not only_keys:
            return
        lib_paths = lib_paths or self._lib_paths()
        if not lib_paths:
            return
        try:
            from validator import validate_downloaded_grouped
            update_scan_status(self.state, "running", "Validating new downloads…")
            save_state(self.state)
            review = validate_downloaded_grouped(
                lib_paths, state=self.state, only_keys=set(only_keys), full=True
            )

            broken  = review.get("broken", 0)
            checked = review.get("checked", 0)
            if broken > 0:
                logger.warning(
                    "[Validator] %d of %d new download(s) failed validation — "
                    "flagged for your review (nothing deleted automatically).",
                    broken, checked,
                )
                for show in review.get("shows", []):
                    for season in show.get("seasons", []):
                        for bf in season.get("broken_files", []):
                            logger.warning("[Validator]   %s — %s", bf["file"], bf["reason"])
                self.state["pending_download_review"] = {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "checked":    checked,
                    "broken":     broken,
                    "shows":      review.get("shows", []),
                }
                # Interactive Discord ping with link buttons to act on it.
                try:
                    items = [
                        f"**{show.get('show', '?')}** — {bf.get('file', '?')} "
                        f"({bf.get('reason', 'corrupt')})"
                        for show in review.get("shows", [])
                        for season in show.get("seasons", [])
                        for bf in season.get("broken_files", [])
                    ]
                    self.notifier.download_review(
                        broken=broken, checked=checked, items=items,
                        web_url=self.config.get("web_public_url", ""),
                    )
                except Exception as _nx:
                    logger.debug("[Notify] review notification error: %s", _nx)
            else:
                if checked > 0:
                    logger.info("[Validator] All %d new download(s) passed validation.", checked)
                self.state.pop("pending_download_review", None)
            save_state(self.state)
        except Exception as val_exc:
            logger.warning("[Validator] Validation error (non-fatal): %s", val_exc)

    def _lib_paths(self) -> list[str]:
        """Return all enabled library paths from config."""
        return [
            lib.get("path", "")
            for lib in self.config.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
        ]

    # ------------------------------------------------------------------
    # TV
    # ------------------------------------------------------------------

    def _process_tv_library(self, lib: dict, lib_idx: int = 0, lib_total: int = 0) -> dict:
        lib_name = lib.get("name", "?")
        lib_path = lib.get("path", "")
        watchlist: list[str] = lib.get("watchlist", [])
        auto_discover: bool = lib.get("auto_discover", True)

        logger.info("=== Processing TV library: %s ===", lib_name)

        disk_inventory = scan_tv_library(lib_path)
        show_snapshot: dict = {}

        # Build the combined show list: watchlist + all shows already on disk.
        # NAS shows are processed via auto_discover to stay up-to-date but are
        # NOT saved to the watchlist — the watchlist is reserved for shows the
        # user explicitly wants to acquire/track (new content not yet on NAS).
        excluded = {
            normalise_show_name(s)
            for s in self.config.get("excluded_shows", [])
        }

        shows_to_process: list[str] = [
            s for s in watchlist
            if normalise_show_name(s) not in excluded
        ]
        if auto_discover:
            watchlist_normalised = {normalise_show_name(s) for s in watchlist}
            for disk_show in disk_inventory:
                clean = clean_show_name(disk_show)
                norm = normalise_show_name(clean)
                if norm not in watchlist_normalised and norm not in excluded:
                    shows_to_process.append(clean)
            logger.info(
                "[TV] %s — %d from watchlist + %d auto-discovered = %d total shows",
                lib_name,
                len(watchlist),
                len(shows_to_process) - len(watchlist),
                len(shows_to_process),
            )

        show_total = len(shows_to_process)
        for idx, show_entry in enumerate(shows_to_process, 1):
            show_name = show_entry.strip()
            logger.info("[TV] ── Show %d/%d: %s ──", idx, show_total, show_name)
            update_scan_status(
                self.state, "running",
                detail     = f"[{idx}/{show_total}] {show_name}…",
                lib_name   = lib_name,
                lib_idx    = lib_idx,
                lib_total  = lib_total,
                show_idx   = idx,
                show_total = show_total,
                show_name  = show_name,
            )
            save_state(self.state)
            snap = self._process_show(show_name, lib_path, disk_inventory)
            if snap:
                show_snapshot[show_name] = snap

        return {"type": "tv", "path": lib_path, "shows": show_snapshot}

    def _process_show(
        self, show_name: str, lib_path: str, disk_inventory: dict
    ) -> Optional[dict]:
        """Process a single show. Returns a snapshot dict for the UI."""
        logger.info("[TV] Processing show: %s", show_name)

        # Throttle: if the download queue is already at capacity, defer this
        # show. Existing in-flight downloads finish and validate first; the next
        # run/pass picks this show up. This is what keeps us working a couple of
        # series at a time instead of queuing the entire library at once.
        if self._in_flight_at_capacity():
            logger.info(
                "[TV] Deferring %r — %d download(s) already in flight (cap %d); "
                "will resume once some finish.",
                show_name, self._last_in_flight, self._max_in_flight,
            )
            return None

        # TVMaze — free, no API key needed
        tvmaze_data = self.tvmaze.get_all_episodes(show_name)
        if not tvmaze_data:
            logger.warning("[TV] TVMaze found nothing for %r", show_name)
            return None

        all_episodes: dict = tvmaze_data["episodes"]
        show_meta: dict = tvmaze_data["show"]

        # Get TMDB ID for Jellyfin folder naming (from TVMaze externals — free)
        tmdb_id: Optional[int] = (show_meta.get("externals") or {}).get("tmdb")
        poster_url: str = self.tvmaze.get_poster_url(show_name)

        # Find existing disk entry (case-insensitive fuzzy match)
        disk_key = self._match_disk_show(show_name, disk_inventory)
        disk_data = disk_inventory.get(disk_key, {}) if disk_key else {}

        season_queued_count: dict[int, int] = {}

        for season_num, episodes in sorted(all_episodes.items()):
            disk_season = disk_data.get(season_num, set())
            missing = [
                ep for ep in episodes
                if ep["episode_number"] not in disk_season
            ]

            # ── Filesystem safety check ─────────────────────────────────────
            # The scanner inventory might be slightly stale (or have missed a
            # file with an unusual name) — do a direct existence check on the
            # season folder BEFORE searching. If the episode file actually
            # exists on disk under ANY naming convention, do NOT re-download.
            if missing and disk_key:
                from renamer import _find_episode_file
                from scanner import find_existing_season_folder
                show_dir = Path(lib_path) / disk_key
                season_dir = find_existing_season_folder(show_dir, season_num)
                if season_dir:
                    filtered_missing = []
                    for ep in missing:
                        existing_file = _find_episode_file(season_dir, season_num, ep["episode_number"])
                        if existing_file:
                            logger.info(
                                "[TV] %s S%02dE%02d already on disk as %r — skipping (overwrite-safe)",
                                show_name, season_num, ep["episode_number"], existing_file.name,
                            )
                            # Also mark queued so it won't be re-considered
                            mark_queued(
                                self.state,
                                f"tv::{show_name}::S{season_num:02d}E{ep['episode_number']:02d}",
                            )
                        else:
                            filtered_missing.append(ep)
                    missing = filtered_missing

            if not missing:
                logger.debug("[TV] %s S%02d — all episodes present", show_name, season_num)
                continue

            logger.info(
                "[TV] %s S%02d — missing %d episode(s)", show_name, season_num, len(missing)
            )

            # ── Season-pack acquisition (integrity-checked, surgical) ───────────
            # SEASON PACK FIRST: whenever a season has missing episodes, the
            # season pack is the PRIMARY source. It's imported EPISODE-SELECTIVELY
            # — only the missing episodes it actually contains are pulled out
            # (each deep-validated first) and every other file is discarded, so a
            # pack can never create duplicates or overwrite a good file.
            #
            # Episodes the pack does NOT deliver (not present in it, or failed
            # validation) fall back to an INDIVIDUAL-episode torrent — handled
            # automatically by the watcher: it calls _handle_failure for every
            # un-imported pack member, which blacklists the pack for that episode
            # and searches a per-episode release instead. If no individual release
            # exists either, the episode is marked exhausted and we move on.
            #
            # Per-episode search in the loop below is therefore only the PRIMARY
            # path when no usable season pack exists at all (or packs are
            # disabled in config).
            season_packs_enabled = self.config.get("season_packs_enabled", False)
            pack_attempted = False
            if season_packs_enabled:
                if self._try_season_pack_fallback(
                    show_name, season_num, missing, disk_inventory,
                    lib_path, tmdb_id, poster_url,
                ):
                    continue  # pack is the source; watcher does per-episode fallback
                pack_attempted = True  # no pack found — don't re-search it below

            # Episodes that per-episode search can't satisfy fall through to a
            # pack fallback at the end of the season loop.
            pack_candidates: list[dict] = []

            # ── Season-batch pre-search ─────────────────────────────────────
            # Resolve as many of this season's missing episodes as possible from
            # ONE season-wide search (per source, run concurrently) instead of a
            # separate network search per episode. Episodes not found in the
            # pooled results fall back to an individual find_tv() below. This is
            # the big speed win for shows with many gaps in a season.
            season_batch: dict[int, object] = {}
            if len(missing) > 1:
                bl_map = {
                    ep["episode_number"]: self.state.get("torrent_blacklist", {}).get(
                        f"tv::{show_name}::S{season_num:02d}E{ep['episode_number']:02d}", []
                    )
                    for ep in missing
                }
                try:
                    season_batch = self.finder.find_tv_season_batch(
                        show_name, season_num,
                        [ep["episode_number"] for ep in missing], bl_map,
                    )
                except Exception as exc:
                    logger.warning("[TV] Season batch search error (non-fatal): %s", exc)
                    season_batch = {}

            for ep in missing:
                ep_num = ep["episode_number"]
                ep_title = ep.get("name", "")
                state_key = f"tv::{show_name}::S{season_num:02d}E{ep_num:02d}"

                if is_already_queued(self.state, state_key):
                    logger.debug("[TV] Already queued: %s", state_key)
                    continue

                schedule_hours = self.config.get("schedule_hours", 6)
                if not self._ignore_cooldown and not should_retry(self.state, state_key, schedule_hours):
                    attempts = self.state.get("retry_queue", {}).get(state_key, {}).get("attempts", 0)
                    logger.debug("[TV] Skipping %s — %d previous failures, on cooldown", state_key, attempts)
                    continue

                blacklist = self.state.get("torrent_blacklist", {}).get(state_key, [])
                # Prefer the release already resolved by the season-batch search;
                # only hit the network per-episode for the gaps it couldn't fill.
                result = season_batch.get(ep_num)
                if result is None:
                    result = self.finder.find_tv(show_name, season_num, ep_num, blacklist=blacklist)
                if not result:
                    logger.warning("[TV] No torrent found for %s S%02dE%02d", show_name, season_num, ep_num)
                    ep_tag = f"S{season_num:02d}E{ep_num:02d}"
                    # Group not-found by show for the end-of-run summary
                    self._run_not_found.setdefault(show_name, []).append(ep_tag)
                    add_to_retry_queue(
                        self.state, state_key,
                        f"{show_name} {ep_tag}",
                    )
                    # No individual torrent → maybe a season pack has it.
                    pack_candidates.append(ep)
                    continue

                if result.seeders < self._min_seeders:
                    logger.warning(
                        "[TV] Skipping %r — only %d seeders (min %d)",
                        result.name, result.seeders, self._min_seeders,
                    )
                    ep_tag = f"S{season_num:02d}E{ep_num:02d}"
                    self._run_not_found.setdefault(show_name, []).append(ep_tag)
                    add_to_retry_queue(self.state, state_key, f"{show_name} {ep_tag}")
                    # No adequately-seeded individual torrent → try a season pack.
                    pack_candidates.append(ep)
                    continue

                # Build save path — routes to existing show folder even if in another library
                save_path = self._resolve_show_save_path(
                    show_name, disk_inventory, lib_path, season_num, tmdb_id
                )
                rename = make_tv_filename(show_name, season_num, ep_num, ep_title)

                if self.dry_run:
                    logger.info(
                        "[DRY RUN] Would queue: %s → %s / %s",
                        result.name, show_folder, rename,
                    )
                else:
                    queued = self._queue_result(result, save_path)
                    if queued:
                        mark_queued(self.state, state_key)
                        season_queued_count[season_num] = season_queued_count.get(season_num, 0) + 1
                        record_history(self.state, {
                            "type": "tv",
                            "show": show_name,
                            "season": season_num,
                            "episode": ep_num,
                            "title": ep_title,
                            "release": result.name,
                            "size_gb": result.size_gb,
                            "seeders": result.seeders,
                            "poster_url": poster_url,
                            "tmdb_id": tmdb_id,
                        })
                        # Track torrent name per episode for validation blacklisting
                        dt = self.state.setdefault("downloaded_torrents", {})
                        dt[state_key] = {"release": result.name, "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                        self.notifier.episode_downloaded(show_name, season_num, ep_num, ep_title)
                        # Track for end-of-run Discord summary
                        ep_tag = f"S{season_num:02d}E{ep_num:02d}"
                        self._run_tv_queued.setdefault(show_name, []).append(ep_tag)
                        # Record the show folder path (parent of the season folder)
                        self._run_tv_paths.setdefault(show_name, str(Path(save_path).parent))
                        # Track this download for the post-download validation watcher
                        self._run_validation_keys.add(state_key)
                        self._track_pending_torrent(result.name)
                        # Concurrent mode (catch-up): publish immediately + spawn
                        # the watcher so this download is managed/validated while we
                        # keep searching the rest, instead of waiting until the end.
                        self._publish_pending(state_key, result.name)
                        # Brief pause between torrent downloads — private trackers
                        # rate-limit rapid successive .torrent file fetches.
                        time.sleep(3)

            # ── Season-pack FALLBACK ────────────────────────────────────────
            # Any episode with no usable individual torrent (e.g. Jimmy Neutron,
            # which only exists as season packs) gets one more shot via a pack —
            # imported surgically (only these episodes, each deep-validated).
            if season_packs_enabled and pack_candidates and not pack_attempted:
                self._try_season_pack_fallback(
                    show_name, season_num, pack_candidates, disk_inventory,
                    lib_path, tmdb_id, poster_url,
                )

        # Build snapshot for UI
        total_eps = sum(len(eps) for eps in all_episodes.values())
        on_disk = sum(len(eps) for eps in (disk_data or {}).values())
        missing_count = max(0, total_eps - on_disk)
        return {
            "show": show_name,
            "tmdb_id": tmdb_id,
            "poster_url": poster_url,
            "seasons": len(all_episodes),
            "total_episodes": total_eps,
            "on_disk": on_disk,
            "missing": missing_count,
            "queued_this_run": sum(season_queued_count.values()),
        }

    def _build_global_show_index(self) -> dict:
        """
        Scan every enabled TV/animation library path and build a map:
            normalised_show_name -> (lib_path, actual_folder_name)
        """
        index: dict = {}
        for lib in self.config.get("libraries", []):
            if not lib.get("enabled", True):
                continue
            if lib.get("type", "").lower() not in ("tv", "animation"):
                continue
            lib_path = lib.get("path", "")
            if not lib_path:
                continue
            try:
                inv = scan_tv_library(lib_path)
                for folder_name in inv:
                    key = normalise_show_name(folder_name)
                    if key and key not in index:
                        index[key] = (lib_path, folder_name)
            except Exception as e:
                logger.debug("[GlobalIndex] Could not scan %s: %s", lib_path, e)
        logger.info("[GlobalIndex] Indexed %d existing TV shows across all libraries", len(index))
        return index

    def _free_gb(self, path: str) -> float:
        """Return free gigabytes at a path, or 0 on error."""
        import shutil as _sh
        try:
            usage = _sh.disk_usage(path)
            return usage.free / 1e9
        except Exception:
            return 0.0

    def _pick_save_path(self, preferred_path: str, lib_type: str, min_free_gb: float = 5.0) -> str:
        """
        Return ``preferred_path`` if the drive has at least ``min_free_gb`` free.
        Otherwise scan all enabled libraries of the same type for a drive that does,
        and return the first alternative found.  Falls back to ``preferred_path``
        (so the download still gets queued even on a nearly-full drive).
        """
        if self._free_gb(preferred_path) >= min_free_gb:
            return preferred_path

        logger.warning(
            "[DiskCheck] Low space at %s (< %.0f GB free) — looking for alternate drive",
            preferred_path, min_free_gb,
        )
        same_type = ("tv", "animation") if lib_type in ("tv", "animation") else ("movie",)
        for lib in self.config.get("libraries", []):
            if not lib.get("enabled", True):
                continue
            if lib.get("type", "") not in same_type:
                continue
            alt = lib.get("path", "")
            if not alt or alt == preferred_path:
                continue
            free = self._free_gb(alt)
            if free >= min_free_gb:
                logger.info(
                    "[DiskCheck] Using alternate drive %s (%.1f GB free)", alt, free
                )
                return alt  # caller will append show/season folders
        logger.warning("[DiskCheck] No alternate drive found — using original path despite low space")
        return preferred_path

    def _match_disk_show(self, show_name: str, disk_inventory: dict) -> Optional[str]:
        """Find the closest matching show folder name on disk (case-insensitive)."""
        needle = normalise_show_name(show_name)
        for key in disk_inventory:
            if normalise_show_name(key) == needle:
                return key
        return None

    def _get_or_create_show_folder(
        self,
        show_name: str,
        disk_inventory: dict,
        lib_path: str,
        tmdb_id: Optional[int] = None,
    ) -> str:
        """
        Return the show folder name to use.
        Prefers existing folder on disk (preserves user's naming).
        Creates new folder using [tmdb-XXXXX] format if show is new.
        """
        disk_key = self._match_disk_show(show_name, disk_inventory)
        if disk_key:
            return disk_key
        return make_tv_show_folder(show_name, tmdb_id)

    def _resolve_show_save_path(
        self,
        show_name: str,
        disk_inventory: dict,
        lib_path: str,
        season_num: int,
        tmdb_id: Optional[int] = None,
    ) -> str:
        """
        Return the full save path for a season, respecting existing show locations
        across ALL configured library paths (not just the current one).

        Priority:
          1. Show folder already exists in the current lib_path → use it
          2. Show folder found in another library via global index → use THAT path
          3. Show is brand new → create in the current lib_path
        """
        # 1. Existing folder in the current library
        disk_key = self._match_disk_show(show_name, disk_inventory)
        if disk_key:
            show_dir = Path(lib_path) / disk_key
            existing = find_existing_season_folder(show_dir, season_num)
            if existing:
                return str(existing)
            zero_pad = detect_season_padding(show_dir)
            season_folder = make_tv_season_folder(season_num, zero_pad=zero_pad)
            return str(show_dir / season_folder)

        # 2. Existing folder in another library (global cross-library check)
        global_index = getattr(self, "_global_show_index", {})
        norm = normalise_show_name(show_name)
        if norm in global_index:
            existing_lib_path, existing_folder = global_index[norm]
            if existing_lib_path != lib_path:
                logger.info(
                    "[TV] %s already exists in another library (%s) — routing there",
                    show_name, existing_lib_path,
                )
            show_dir = Path(existing_lib_path) / existing_folder
            existing = find_existing_season_folder(show_dir, season_num)
            if existing:
                return str(existing)
            zero_pad = detect_season_padding(show_dir)
            season_folder = make_tv_season_folder(season_num, zero_pad=zero_pad)
            return str(show_dir / season_folder)

        # 3. New show — create in the current library, with disk-space fallback
        season_folder = make_tv_season_folder(season_num, zero_pad=False)
        new_folder = make_tv_show_folder(show_name, tmdb_id)
        lib_type = "tv"  # covers both tv and animation
        base_path = self._pick_save_path(lib_path, lib_type)
        return str(Path(base_path) / new_folder / season_folder)

    # ------------------------------------------------------------------
    # Movies
    # ------------------------------------------------------------------

    def _process_movie_library(self, lib: dict) -> dict:
        lib_name = lib.get("name", "?")
        lib_path = lib.get("path", "")
        watchlist: list[str] = lib.get("watchlist", [])

        logger.info("=== Processing Movie library: %s ===", lib_name)

        disk_inventory = scan_movie_library(lib_path)
        disk_titles_lower = {k.lower(): k for k in disk_inventory}
        movie_snapshot: list = []

        for entry in watchlist:
            title, year = parse_watchlist_movie(entry)
            title_year_key = f"{title} ({year})".lower() if year else title.lower()

            # Check if already on disk
            if title_year_key in disk_titles_lower:
                disk_entry = disk_inventory[disk_titles_lower[title_year_key]]
                if disk_entry.get("has_file"):
                    logger.debug("[Movie] Already on disk: %s", entry)
                    continue

            state_key = f"movie::{title}::{year or ''}"
            if is_already_queued(self.state, state_key):
                logger.debug("[Movie] Already queued: %s", state_key)
                continue

            schedule_hours = self.config.get("schedule_hours", 6)
            if not self._ignore_cooldown and not should_retry(self.state, state_key, schedule_hours):
                logger.debug("[Movie] Skipping %s — on cooldown after failures", state_key)
                continue

            logger.info("[Movie] Searching for: %s", entry)
            result = self.finder.find_movie(title, year)
            if not result:
                logger.warning("[Movie] No torrent found for: %s", entry)
                self._run_not_found.setdefault(entry, []).append("movie")
                continue

            if result.seeders < self._min_seeders:
                logger.warning(
                    "[Movie] Skipping %r — only %d seeders", result.name, result.seeders
                )
                self._run_not_found.setdefault(entry, []).append("movie")
                continue

            # Get TMDB ID + poster — works with or without API key
            tmdb_info = self.tmdb.get_full_movie_info(title, year)
            tmdb_id: Optional[int] = tmdb_info.get("tmdb_id") if tmdb_info else None
            resolved_year = year or (
                int(str(tmdb_info["year"])[:4])
                if tmdb_info and tmdb_info.get("year") else None
            )
            movie_poster = (tmdb_info or {}).get("poster_url", "")

            # Build save path using [tmdb-XXXXX] naming, with disk-space fallback
            folder_name = make_movie_folder(title, resolved_year or 0, tmdb_id)
            base_path = self._pick_save_path(lib_path, "movie")
            save_path = str(Path(base_path) / folder_name)

            if self.dry_run:
                logger.info("[DRY RUN] Would queue: %s → %s", result.name, folder_name)
            else:
                queued = self._queue_result(result, save_path)
                if queued:
                    mark_queued(self.state, state_key)
                    record_history(self.state, {
                        "type": "movie",
                        "title": title,
                        "year": resolved_year,
                        "tmdb_id": tmdb_id,
                        "release": result.name,
                        "size_gb": result.size_gb,
                        "seeders": result.seeders,
                        "poster_url": movie_poster,
                    })
                    self.notifier.movie_downloaded(title, resolved_year)
                    year_str = f" ({resolved_year})" if resolved_year else ""
                    movie_label = f"{title}{year_str}"
                    self._run_movies_queued.append(movie_label)
                    self._run_movie_paths[movie_label] = save_path
                    time.sleep(3)

            movie_snapshot.append({
                "title": title,
                "year": resolved_year,
                "tmdb_id": tmdb_id,
                "poster_url": movie_poster,
                "on_disk": title_year_key in disk_titles_lower
                           and disk_inventory.get(disk_titles_lower.get(title_year_key, ""), {}).get("has_file"),
                "queued": not self.dry_run and not is_already_queued(self.state, state_key),
            })

        # Also include movies already on disk not in watchlist
        for key, info in disk_inventory.items():
            if not any(key.lower() == f"{parse_watchlist_movie(e)[0]} ({parse_watchlist_movie(e)[1]})".lower()
                       for e in watchlist):
                movie_snapshot.append({
                    "title": info["title"],
                    "year": info["year"],
                    "tmdb_id": info.get("id_value") if info.get("id_type") == "tmdb" else None,
                    "on_disk": info.get("has_file", False),
                    "queued": False,
                })

        return {"type": "movie", "path": lib_path, "movies": movie_snapshot}

    # ------------------------------------------------------------------
    # qBittorrent dispatch
    # ------------------------------------------------------------------

    def _track_pending_torrent(self, release_name: str) -> None:
        """Remember a torrent (by release name) queued this run so the
        post-download watcher knows to wait for it to finish."""
        if release_name and release_name not in self._run_pending_torrents:
            self._run_pending_torrents.append(release_name)

    def _qbit_offline(self) -> bool:
        """True when qBittorrent can't reach the BitTorrent network (VPN down,
        dropped tunnel, etc.). Cached for 15s. When offline, every torrent reads
        0 seeds, so we must NOT cull/blacklist and should NOT queue new work into
        a dead client. Fails OPEN (returns False) if qBit can't be reached, so a
        transient API hiccup doesn't wedge the pipeline."""
        now = time.time()
        if now - self._conn_cache_ts > 15:
            try:
                h = self.qbit.network_health()
                self._conn_up = (not h.get("known", False)) or bool(h.get("up", True))
                if not self._conn_up:
                    logger.warning(
                        "[Connectivity] qBittorrent offline — status=%s, dht_nodes=%s "
                        "(VPN/tunnel likely down).", h.get("status"), h.get("dht_nodes"),
                    )
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug("[Connectivity] check failed (assuming up): %s", exc)
                self._conn_up = True
            self._conn_cache_ts = now
        return not self._conn_up

    def _in_flight_at_capacity(self) -> bool:
        """True when qBittorrent already holds >= max_in_flight_downloads
        incomplete torrents, so we should defer starting new shows. The count
        is cached briefly so a tight show loop doesn't hammer the qBit API.

        Counts EVERY incomplete torrent (downloading, queued, fetching
        metadata, stalled, …) — not just the actively-downloading ones — since
        queued torrents are exactly the backlog we're trying to bound.
        """
        cap = self._max_in_flight
        if cap <= 0:
            return False
        now = time.time()
        if now - self._in_flight_cache_ts > 15:
            try:
                torrents = self.qbit.get_torrents()
                self._last_in_flight = sum(
                    1 for t in torrents if (t.get("progress", 0) or 0) < 1.0
                )
                self._in_flight_cache_ts = now
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug("[Downloader] in-flight check failed (allowing): %s", exc)
                return False
        return self._last_in_flight >= cap

    def _wait_for_queue_capacity(self, idx: int = 0, total: int = 0,
                                 poll_sec: int = 30) -> None:
        """Block until the in-flight download queue drops below the cap. The
        CompletionWatcher (rename/cull/complete) keeps draining the queue in the
        background, so this returns once there's room for the next series. No-op
        when the cap is disabled or the queue already has room."""
        # Pause the whole campaign while qBittorrent is offline — there's no
        # point queuing the next series into a client that can't reach the
        # network (it'd just pile up at 0 seeds). Resumes automatically when
        # connectivity returns.
        self._conn_cache_ts = 0.0
        conn_announced = False
        while self._qbit_offline():
            if not conn_announced:
                logger.warning(
                    "[Campaign] qBittorrent offline (VPN/tunnel down) — pausing the "
                    "campaign. Will resume automatically once connectivity returns."
                )
                conn_announced = True
            try:
                update_scan_status(
                    self.state, "running",
                    detail=(f"[{idx}/{total}] Paused — qBittorrent offline "
                            "(VPN/connection down). Waiting to reconnect…"),
                )
                save_state(self.state)
            except Exception:  # pylint: disable=broad-except
                pass
            time.sleep(poll_sec)
            self._conn_cache_ts = 0.0
        if conn_announced:
            logger.info("[Campaign] qBittorrent back online — resuming.")

        if self._max_in_flight <= 0:
            return
        # Force a fresh read on entry.
        self._in_flight_cache_ts = 0.0
        announced = False
        while self._in_flight_at_capacity():
            if not announced:
                logger.info(
                    "[Campaign] Queue full — %d download(s) in flight (cap %d). "
                    "Waiting for downloads to finish/cull before starting the next series…",
                    self._last_in_flight, self._max_in_flight,
                )
                announced = True
            try:
                update_scan_status(
                    self.state, "running",
                    detail=(f"[{idx}/{total}] Waiting for queue to drain — "
                            f"{self._last_in_flight} in flight (cap {self._max_in_flight})…"),
                )
                save_state(self.state)
            except Exception:  # pylint: disable=broad-except
                pass
            time.sleep(poll_sec)
            self._in_flight_cache_ts = 0.0  # force re-check next loop
        if announced:
            logger.info("[Campaign] Queue has room (%d in flight) — resuming.",
                        self._last_in_flight)

    def _queue_result(self, result, save_path: str) -> bool:
        """Send a TorrentResult to qBittorrent. Returns True on success.

        Hard overwrite-safety guard: if the target save_path already contains
        a video file matching the SxxExx tag in the torrent name, the queue
        request is refused. The validator + scanner should have caught this
        upstream, but this is the final safety net.
        """
        # ── Overwrite safety ─────────────────────────────────────────────────
        try:
            import re as _re
            se = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", result.name)
            if se:
                season, episode = int(se.group(1)), int(se.group(2))
                save_dir = Path(save_path)
                if save_dir.exists():
                    from renamer import _find_episode_file
                    existing = _find_episode_file(save_dir, season, episode)
                    if existing:
                        logger.warning(
                            "[Downloader] OVERWRITE BLOCKED: %s already exists at %s — refusing to queue %r",
                            existing.name, save_dir, result.name,
                        )
                        return False
        except Exception as _safe_exc:
            logger.debug("[Downloader] Overwrite check non-fatal error: %s", _safe_exc)

        # Always prefer .torrent file over magnet on private trackers —
        # .torrent files carry the tracker announce URL with the user's passkey.
        # Magnet links on private trackers have no passkey and get 0 peers.
        if result.torrent_url:
            extra_headers: dict = {}
            for source in self.finder._sources:
                if source.name == result.source_name:
                    extra_headers = source.get_download_headers()
                    break
            return self.qbit.add_torrent_url(
                result.torrent_url, save_path, extra_headers=extra_headers
            )
        if result.magnet:
            logger.warning(
                "[Downloader] No .torrent URL — falling back to magnet (may stall on private tracker)"
            )
            return self.qbit.add_magnet(result.magnet, save_path)
        logger.error("[Downloader] Result has no magnet or torrent URL: %r", result.name)
        return False

    # ------------------------------------------------------------------
    # Inventory command
    # ------------------------------------------------------------------

    def print_inventory(self, library_filter: Optional[str] = None) -> None:
        libraries = self.config.get("libraries", [])
        for lib in libraries:
            if not lib.get("enabled", True):
                continue
            if library_filter and lib.get("name", "").lower() != library_filter.lower():
                continue
            lib_type = lib.get("type", "").lower()
            lib_name = lib.get("name", "?")
            lib_path = lib.get("path", "")
            print(f"\n{'='*60}")
            print(f" Library: {lib_name}  ({lib_type.upper()})  →  {lib_path}")
            print(f"{'='*60}")
            if lib_type in ("tv", "animation"):
                inventory = scan_tv_library(lib_path)
                for show, seasons in sorted(inventory.items()):
                    total_eps = sum(len(eps) for eps in seasons.values())
                    print(f"  📺 {show}  ({len(seasons)} seasons, {total_eps} episodes on disk)")
                    for season_num, episodes in sorted(seasons.items()):
                        ep_list = ", ".join(f"E{e:02d}" for e in sorted(episodes))
                        print(f"       Season {season_num:02d}: {ep_list}")
            elif lib_type == "movie":
                inventory = scan_movie_library(lib_path)
                for key, info in sorted(inventory.items()):
                    status = "✅" if info.get("has_file") else "📁 (folder only)"
                    print(f"  🎬 {key}  {status}")


# ---------------------------------------------------------------------------
# Pre-flight check
# ---------------------------------------------------------------------------

def run_preflight_check(config: dict) -> bool:
    """
    Test every external connection before a real run.
    Prints a clear pass/fail for each component and returns True only if all pass.
    """
    import requests as req
    from pathlib import Path

    ok_all = True

    def _ok(label: str):
        print(f"  \033[32m✔\033[0m  {label}")

    def _fail(label: str, detail: str = ""):
        nonlocal ok_all
        ok_all = False
        detail_str = f"  → {detail}" if detail else ""
        print(f"  \033[31m✘\033[0m  {label}{detail_str}")

    def _warn(label: str, detail: str = ""):
        print(f"  \033[33m⚠\033[0m  {label}" + (f"  → {detail}" if detail else ""))

    print("\n── Pre-flight check ──────────────────────────────────────────")

    # 1. Config placeholders
    print("\n[1] Config values")
    tmdb_key = config.get("tmdb", {}).get("api_key", "")
    if tmdb_key and "your_tmdb" not in tmdb_key:
        _ok("TMDB API key is set (optional — enhances movie posters)")
    else:
        _warn("TMDB API key", "not set — movie TMDB IDs scraped from web, TV uses TVMaze (both work fine)")

    sources = config.get("torrent_sources", [])
    for src in sources:
        if not src.get("enabled"):
            continue
        # Jackett authenticates with an API key, not username/password.
        if src.get("type") == "jackett":
            if src.get("api_key") and "your_" not in src.get("api_key", ""):
                _ok(f"{src['name']} API key is set")
            else:
                _fail(f"{src['name']} API key", "api_key still set to placeholder")
            continue
        u, p = src.get("username", ""), src.get("password", "")
        if u and "your_" not in u and p and "your_" not in p:
            _ok(f"{src['name']} credentials are set")
        else:
            _fail(f"{src['name']} credentials", "username/password still set to placeholder")

    # 2. TVMaze (free, no key) + optional TMDB API
    print("\n[2] TVMaze (free episode data — no API key needed)")
    try:
        r = req.get("https://api.tvmaze.com/shows/1", timeout=8)
        if r.ok:
            _ok("TVMaze API reachable")
        else:
            _fail("TVMaze", f"status {r.status_code}")
    except Exception as exc:
        _fail("TVMaze", str(exc))

    print("\n[2b] TMDB API (optional — improves movie poster quality)")
    if tmdb_key and "your_tmdb" not in tmdb_key:
        try:
            r = req.get(
                "https://api.themoviedb.org/3/configuration",
                params={"api_key": tmdb_key},
                timeout=10,
            )
            if r.status_code == 200:
                _ok("TMDB API key is valid")
            elif r.status_code == 401:
                _fail("TMDB API", "invalid API key (401)")
            else:
                _fail("TMDB API", f"unexpected status {r.status_code}")
        except Exception as exc:
            _fail("TMDB API", str(exc))
    else:
        _warn("TMDB API", "no key configured — movie TMDB IDs will be scraped from web (still works)")

    # 3. SceneTime login
    print("\n[3] SceneTime")
    for src_cfg in sources:
        if not src_cfg.get("enabled"):
            continue
        # Jackett is probed separately in [3b] — it's not a SceneTime login.
        if src_cfg.get("type") == "jackett":
            continue
        if "your_" in src_cfg.get("username", "") or "your_" in src_cfg.get("password", ""):
            _warn(f"{src_cfg['name']}", "skipped — credentials not configured")
            continue
        from sources.scenetime import SceneTimeSource
        src = SceneTimeSource(src_cfg)
        if src.login():
            _ok(f"{src_cfg['name']} login successful")
        else:
            _fail(f"{src_cfg['name']} login", "check username/password in config.json")

    # 3b. Jackett (Torznab API — authenticated by API key)
    print("\n[3b] Jackett (indexer aggregator — API key auth)")
    jk = _jackett_config(config)
    if not jk:
        _warn("Jackett", "no enabled Jackett source configured (optional)")
    elif jackett_reachable(jk):
        _ok(f"Jackett reachable — {jk.get('url', 'http://127.0.0.1:9117')}")
    else:
        _fail(
            "Jackett",
            f"not reachable at {jk.get('url', 'http://127.0.0.1:9117')} — "
            "check it's running and the api_key/url are correct",
        )

    # 4. qBittorrent
    print("\n[4] qBittorrent")
    qcfg = config.get("qbittorrent", {})
    qurl = qcfg.get("url", "http://localhost:8080").rstrip("/").replace("//localhost:", "//127.0.0.1:")
    bypass = qcfg.get("bypass_auth", False)
    try:
        session = req.Session()
        # qBittorrent 4.6+ requires Referer/Origin to pass CSRF check
        session.headers.update({
            "Referer": qurl + "/",
            "Origin":  qurl,
        })
        if not bypass and qcfg.get("username"):
            login_resp = session.post(
                f"{qurl}/api/v2/auth/login",
                data={"username": qcfg.get("username"), "password": qcfg.get("password", "")},
                timeout=8,
            )
            if login_resp.text.strip().lower() != "ok.":
                _fail("qBittorrent", f"login rejected: {login_resp.text.strip()[:80]}")
                raise SystemExit
        r = session.get(f"{qurl}/api/v2/app/version", timeout=8)
        if r.ok:
            _ok(f"qBittorrent reachable — v{r.text.strip()}")
        elif r.status_code == 403:
            _fail(
                "qBittorrent",
                "403 Forbidden — in qBittorrent go to Tools → Options → Web UI → "
                "check 'Bypass authentication for clients on localhost'",
            )
        else:
            _fail("qBittorrent", f"status {r.status_code} — is Web UI enabled on port {qurl.split(':')[-1]}?")
    except req.ConnectionError:
        _fail("qBittorrent", f"connection refused at {qurl} — is qBittorrent running?")
    except SystemExit:
        pass
    except Exception as exc:
        _fail("qBittorrent", str(exc))

    # 5. NAS / library paths
    print("\n[5] Library paths")
    for lib in config.get("libraries", []):
        if not lib.get("enabled", True):
            continue
        p = Path(lib.get("path", ""))
        name = lib.get("name", "?")
        if p.exists():
            _ok(f"{name} → {p}")
        else:
            _fail(f"{name}", f"path not found: {p}  (is the NAS mounted / UNC path accessible?)")

    # 6. Notifications (optional)
    print("\n[6] Notifications (optional)")
    notif = config.get("notifications", {})
    dw = notif.get("discord_webhook", "")
    ntfy = notif.get("ntfy_topic", "")
    if dw and "your_webhook" not in dw:
        try:
            r = req.post(dw, json={"content": "✅ Jellyfin Downloader pre-flight check passed"}, timeout=8)
            if r.ok:
                _ok("Discord webhook — test message sent")
            else:
                _warn("Discord webhook", f"status {r.status_code}")
        except Exception as exc:
            _warn("Discord webhook", str(exc))
    else:
        _warn("Discord webhook", "not configured (optional)")

    if ntfy and "your_ntfy" not in ntfy:
        _ok(f"Ntfy topic set: {ntfy}")
    else:
        _warn("Ntfy", "not configured (optional)")

    print("\n──────────────────────────────────────────────────────────────")
    if ok_all:
        print("\033[32m  All checks passed — ready to run!\033[0m\n")
    else:
        print("\033[31m  Some checks failed — fix the issues above before running.\033[0m\n")

    return ok_all


def run_test_search(config: dict, query: str) -> None:
    """
    Do a live search on all enabled sources, apply quality filters,
    and print every result with pass/fail reason. Useful for tuning.
    """
    quality = config.get("quality", {})
    sources = build_sources(config.get("torrent_sources", []))
    finder = TorrentFinder(sources, quality)

    print(f"\n── Test search: {query!r} ──────────────────────────────────────")
    found_any = False
    for source in sources:
        print(f"\n  Source: {source.name}")
        results = source.search(query)
        if not results:
            print("    (no results)")
            continue
        found_any = True
        for i, r in enumerate(results[:20]):
            ok, reason = finder.passes_filter(r.name, r.size_gb, "tv")
            status = "\033[32m✔ PASS\033[0m" if ok else f"\033[31m✘ FAIL ({reason})\033[0m"
            size_str = f"{r.size_gb:.2f} GB" if r.size_gb else "? GB"
            print(f"    [{i+1:02d}] {status}")
            print(f"         {r.name}")
            print(f"         {size_str}  •  {r.seeders} seeds  •  {source.name}")
    if not found_any:
        print("\n  No results from any source.")
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Jellyfin Media Auto-Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python downloader.py --check
  python downloader.py --dry-run
  python downloader.py --run-now
  python downloader.py --inventory
  python downloader.py --test-search "The Bear S03E01 1080p"
  python downloader.py --run-now --library "TV Shows"
        """,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run a pre-flight check of all connections (SceneTime, TMDB, qBit, NAS paths)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without actually queueing anything",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run the downloader immediately instead of waiting for the schedule",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="Manage/validate downloads concurrently with the scan, ignore retry "
             "cooldowns, and retry failures immediately (the Download Missing button)",
    )
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="Scan and print the current library inventory, then exit",
    )
    parser.add_argument(
        "--library",
        metavar="NAME",
        default=None,
        help='Only process the named library (e.g. "TV Shows")',
    )
    parser.add_argument(
        "--test-search",
        metavar="QUERY",
        default=None,
        help='Do a live test search and print results with pass/fail quality filter (e.g. "The Bear S03E01 1080p")',
    )
    parser.add_argument(
        "--debug-login",
        action="store_true",
        help="Show full SceneTime login request/response to diagnose auth issues",
    )
    parser.add_argument(
        "--extract",
        action="store_true",
        help="Scan all library paths for RAR archives and extract them (requires 7-Zip)",
    )
    parser.add_argument(
        "--rename",
        action="store_true",
        help="Rename all scene-style video filenames to clean Show Name - SxxExx format",
    )
    parser.add_argument(
        "--rename-dry-run",
        action="store_true",
        help="Preview what --rename would do without actually renaming anything",
    )
    parser.add_argument(
        "--watch-downloads",
        action="store_true",
        help="Watch the last run's queued torrents to completion, then validate them (internal)",
    )
    parser.add_argument(
        "--rss-grab",
        action="store_true",
        help="Lightweight RSS poll: grab newly-aired episodes of monitored shows "
             "now (used by the frequent auto-poll between full scans)",
    )
    parser.add_argument(
        "--validate-paths",
        nargs="+",
        metavar="UNC_PATH",
        help="Deep-validate specific show/season UNC path(s); auto-remove corrupt "
             "files and re-download a different torrent, then verify the replacement",
    )
    parser.add_argument(
        "--sampled",
        action="store_true",
        help="With --validate-paths: use the fast sampled decode (windows across "
             "each file) instead of decoding every file end-to-end. Much faster on "
             "large libraries; still catches stutter/corruption.",
    )
    parser.add_argument(
        "--catch-up-show",
        metavar="SHOW_UNC_PATH",
        help="Catch a single show (given its NAS folder path) up on all missing "
             "episodes, then validate each new download and auto-retry failures",
    )
    parser.add_argument(
        "--catch-up-series",
        action="store_true",
        help="Per-series campaign: process shows one at a time — download missing "
             "+ validate (auto-retry), then re-validate our prior downloads and "
             "flag any corrupt ones for your approval, pausing between series.",
    )
    parser.add_argument(
        "--scope",
        choices=["cartoons", "all"],
        default="cartoons",
        help="With --catch-up-series: which libraries to cover (default: cartoons "
             "= the Adult Animations & Cartoons libraries).",
    )
    parser.add_argument(
        "--no-block",
        action="store_true",
        help="With --catch-up-series: don't pause for approval between series; "
             "let the review popups queue up instead.",
    )
    parser.add_argument(
        "--start-at",
        default=None,
        help="With --catch-up-series: skip ahead to this show (case-insensitive "
             "substring match) and process from there — useful for resuming after "
             "a code change without re-doing earlier series.",
    )
    parser.add_argument(
        "--library-path",
        default=None,
        help="With --catch-up-series: restrict the campaign to a SINGLE library "
             "folder (exact NAS path), e.g. \\\\192.168.0.181\\Jellyfin4\\Tv Shows.",
    )
    args = parser.parse_args()

    config = load_config()

    # Surface config problems early — but not on the frequent internal subprocess
    # modes (watcher / RSS poll), which would just spam the logs + re-probe NAS.
    if not (args.watch_downloads or args.rss_grab):
        cfg_warnings = validate_config(config)
        for _w in cfg_warnings:
            logger.warning("[Config] %s", _w)
        if cfg_warnings and args.check:
            print(f"\n[!] {len(cfg_warnings)} config warning(s) — see above. Continuing.\n")

    if args.check:
        run_preflight_check(config)
        return

    if args.debug_login:
        print("\n── SceneTime login debug ─────────────────────────────────────")
        sources_cfg = config.get("torrent_sources", [])
        for src_cfg in sources_cfg:
            if not src_cfg.get("enabled"):
                continue
            from sources.scenetime import SceneTimeSource
            src = SceneTimeSource(src_cfg)
            src._discover_login_url()
            src.login(debug=True)
        return

    if args.extract:
        from extractor import scan_and_extract, find_extractor
        tool = find_extractor()
        if not tool:
            print("\n[!] No extraction tool found.")
            print("    Please install 7-Zip from https://7-zip.org then re-run --extract")
            return
        print(f"\n── RAR Extraction ({'using ' + tool[1]}) ──────────────────────────")
        lib_paths = [
            lib.get("path", "")
            for lib in config.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
        ]
        result = scan_and_extract(lib_paths, delete_rar=True)
        print(f"    Extracted: {result.get('extracted', 0)}  Failed/skipped: {result.get('failed', 0)}")
        return

    if args.rename or args.rename_dry_run:
        from renamer import rename_video_files, cleanup_library
        dry = args.rename_dry_run
        lib_paths = [
            lib.get("path", "")
            for lib in config.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
        ]
        tag = "DRY RUN — " if dry else ""
        print(f"\n── {tag}File Rename & Cleanup ─────────────────────────────────")

        # Release file locks by removing completed torrents from qBittorrent
        if not dry:
            try:
                from qbit_client import QBitClient
                qb_cfg = config.get("qbittorrent", {})
                qbit = QBitClient(
                    qb_cfg.get("url", ""),
                    qb_cfg.get("username", ""),
                    qb_cfg.get("password", ""),
                    bypass_auth=qb_cfg.get("bypass_auth", True),
                )
                completed = [t for t in qbit.get_torrents() if t.get("progress", 0) >= 1.0]
                for t in completed:
                    qbit.delete_torrent(t["hash"], delete_files=False)
                if completed:
                    print(f"    Released {len(completed)} completed torrent(s) from qBittorrent")
                    print("    Waiting 8s for Windows to release file handles…")
                    time.sleep(8)
            except Exception as e:
                print(f"    Warning: could not release torrents from qBittorrent: {e}")

        result = rename_video_files(lib_paths, dry_run=dry)
        print(f"    Renamed : {result['renamed']}  Skipped: {result['skipped']}  Errors: {result['errors']}")
        clean = cleanup_library(lib_paths, dry_run=dry)
        print(f"    Deleted : {clean['deleted_files']} file(s), {clean['deleted_folders']} folder(s)")
        return

    if args.test_search:
        run_test_search(config, args.test_search)
        return

    if args.watch_downloads:
        dl = Downloader(config)
        dl.watch_downloads()
        return

    if args.rss_grab:
        if not acquire_run_lock("rss-grab"):
            return
        try:
            dl = Downloader(config)
            dl.run_rss_grab()
        finally:
            release_run_lock()
        return

    if args.catch_up_show:
        if not acquire_run_lock("catch-up-show", wait=True):
            return
        try:
            dl = Downloader(config)
            dl.catch_up_show(args.catch_up_show)
        finally:
            release_run_lock()
        return

    if args.catch_up_series:
        if not acquire_run_lock("catch-up-series", wait=True):
            return
        try:
            dl = Downloader(config)
            only_types = {"animation"} if args.scope == "cartoons" else None
            dl.catch_up_series_campaign(
                only_library_types=only_types,
                block_for_review=not args.no_block,
                start_at=args.start_at,
                library_path=args.library_path,
            )
        finally:
            release_run_lock()
        return

    if args.validate_paths:
        dl = Downloader(config)
        summary = dl._validate_and_fix_paths(args.validate_paths, full=not args.sampled)
        print(
            f"\n── Targeted validation ──\n"
            f"   Checked : {summary['checked']}\n"
            f"   Broken  : {summary['broken']}\n"
            f"   Quarantined (kept until replaced): {summary['quarantined']}\n"
            f"   Removed : {summary['removed']}\n"
            f"   Requeued: {summary['requeued']}\n"
            f"   No working release (original restored): {len(summary['exhausted'])}\n"
        )
        return

    dl = Downloader(config, dry_run=args.dry_run)

    if args.inventory:
        dl.print_inventory(library_filter=args.library)
        return

    if args.dry_run:
        logger.info("DRY RUN — nothing will actually be queued in qBittorrent")

    if args.run_now or args.dry_run:
        # Dry runs don't queue, so they don't need the single-writer lock.
        if args.dry_run:
            dl.run(library_filter=args.library, aggressive=args.aggressive)
            return
        if not acquire_run_lock("run-now"):
            return
        try:
            dl.run(library_filter=args.library, aggressive=args.aggressive)
        finally:
            release_run_lock()
        return

    # Scheduled mode
    interval_hours = config.get("schedule_hours", 6)
    logger.info(
        "Scheduled mode — running every %d hour(s). Press Ctrl+C to stop.", interval_hours
    )

    def job():
        logger.info("Scheduled run starting…")
        dl.run(library_filter=args.library)
        logger.info("Scheduled run complete. Next run in %d hour(s).", interval_hours)

    job()  # run immediately on first start
    schedule.every(interval_hours).hours.do(job)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
