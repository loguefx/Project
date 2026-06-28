#!/usr/bin/env python3
"""
Jellyfin Downloader — Web Dashboard
Run with: python web.py  (defaults to http://localhost:5000)
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests as req
from flask import Flask, jsonify, render_template, request

from runtime_paths import (
    DATA_DIR, RESOURCE_DIR, EXE_DIR, IS_FROZEN, child_argv,
)
from version import __version__ as APP_VERSION

# DATA_DIR holds all writable runtime files; when frozen it lives in a stable,
# update-proof location so updates never wipe config/state. RESOURCE_DIR holds
# the bundled Jinja templates (unpacked by PyInstaller when frozen).
BASE_DIR = DATA_DIR
STATE_FILE        = BASE_DIR / "state.json"
CONFIG_FILE       = BASE_DIR / "config.json"
LOG_FILE          = BASE_DIR / "downloader.log"
POSTER_CACHE      = BASE_DIR / "poster_cache.json"
TVMAZE_DISK_CACHE = BASE_DIR / "tvmaze_cache.json"
LIBRARY_CACHE     = BASE_DIR / "library_cache.json"

LIBRARY_CACHE_TTL = 3600  # 1 hour before background refresh kicks in

# Scan progress state shared between threads
_scan_progress: dict = {"running": False, "pct": 0, "label": "", "started_at": None}

app = Flask(__name__, template_folder=str(RESOURCE_DIR / "templates"))
logger = logging.getLogger("web")
log = logging.getLogger("web")

_SCRAPE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"queued": [], "history": [], "last_run": None, "retry_queue": {}}


def save_state(state: dict) -> None:
    """Persist state.json, stripping any stray control characters that would
    corrupt the JSON (same safe-write pattern used by the watcher)."""
    import re as _re_ss
    text = json.dumps(state, indent=2)
    text = _re_ss.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# ---------------------------------------------------------------------------
# Poster cache
# ---------------------------------------------------------------------------

def _load_poster_cache() -> dict:
    if POSTER_CACHE.exists():
        try:
            return json.loads(POSTER_CACHE.read_text("utf-8"))
        except Exception:
            pass
    return {}

def _save_poster_cache(cache: dict) -> None:
    POSTER_CACHE.write_text(json.dumps(cache, indent=2), "utf-8")

def get_poster_url(name: str, tmdb_id: int | None = None, media_type: str = "tv") -> str:
    """Unified poster helper — TV by show name, movies by tmdb_id or name scrape."""
    if media_type == "movie" and tmdb_id:
        return _fetch_movie_poster(tmdb_id)
    if media_type == "movie":
        return _fetch_movie_poster_by_name(name)
    return _fetch_tv_poster(name)

def _fetch_tv_poster(show_name: str) -> str:
    cache = _load_poster_cache()
    key = f"tv:{show_name.lower()}"
    if key in cache:
        return cache[key]
    try:
        r = req.get(
            "https://api.tvmaze.com/singlesearch/shows",
            params={"q": show_name},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            img = data.get("image") or {}
            url = img.get("original") or img.get("medium") or ""
            if url:
                cache[key] = url
                _save_poster_cache(cache)
                return url
    except Exception as exc:
        log.debug("TV poster fetch failed for %r: %s", show_name, exc)
    return ""

def _fetch_movie_poster(tmdb_id: int) -> str:
    cache = _load_poster_cache()
    key = f"movie:{tmdb_id}"
    if key in cache:
        return cache[key]
    # Try TMDB API first (if key configured)
    cfg = load_config()
    api_key = cfg.get("tmdb", {}).get("api_key", "")
    if api_key and "your_tmdb" not in api_key:
        try:
            r = req.get(
                f"https://api.themoviedb.org/3/movie/{tmdb_id}",
                params={"api_key": api_key},
                timeout=10,
            )
            if r.ok:
                pp = r.json().get("poster_path") or ""
                if pp:
                    url = f"https://image.tmdb.org/t/p/w300{pp}"
                    cache[key] = url
                    _save_poster_cache(cache)
                    return url
        except Exception:
            pass
    # Fallback: scrape TMDB og:image
    try:
        r = req.get(
            f"https://www.themoviedb.org/movie/{tmdb_id}",
            headers={"User-Agent": _SCRAPE_UA, "Accept-Language": "en-US"},
            timeout=12,
        )
        m = re.search(r'<meta property="og:image"\s+content="([^"]+)"', r.text)
        if m:
            url = m.group(1)
            cache[key] = url
            _save_poster_cache(cache)
            return url
    except Exception as exc:
        log.debug("Movie poster scrape failed for tmdb:%s: %s", tmdb_id, exc)
    return ""


def _fetch_movie_poster_by_name(title_year: str) -> str:
    """Fetch movie poster by 'Title (Year)' string when no TMDB ID is known."""
    cache = _load_poster_cache()
    key = f"movie_name:{title_year.lower()}"
    if key in cache:
        return cache[key]
    import re as _re
    m = _re.match(r"^(.+?)\s*\((\d{4})\)$", title_year)
    title = m.group(1).strip() if m else title_year
    year  = m.group(2) if m else ""
    cfg = load_config()
    api_key = cfg.get("tmdb", {}).get("api_key", "")
    if api_key and "your_tmdb" not in api_key:
        try:
            r = req.get(
                "https://api.themoviedb.org/3/search/movie",
                params={"api_key": api_key, "query": title, "year": year},
                timeout=10,
            )
            if r.ok:
                results = r.json().get("results", [])
                if results:
                    pp = results[0].get("poster_path") or ""
                    if pp:
                        url = f"https://image.tmdb.org/t/p/w300{pp}"
                        cache[key] = url
                        _save_poster_cache(cache)
                        return url
        except Exception:
            pass
    return ""


def get_qbit_torrents() -> list[dict]:
    """Fetch active torrent list from qBittorrent."""
    try:
        import requests as req
        cfg = load_config().get("qbittorrent", {})
        base = cfg.get("url", "http://localhost:8080").rstrip("/")
        bypass = cfg.get("bypass_auth", False)
        username = cfg.get("username", "")
        password = cfg.get("password", "")

        session = req.Session()
        if not bypass and username:
            session.post(
                f"{base}/api/v2/auth/login",
                data={"username": username, "password": password},
                timeout=5,
            )

        resp = session.get(f"{base}/api/v2/torrents/info", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.debug("qBittorrent unavailable: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.get("/api/history")
def api_history():
    state = load_state()
    history = state.get("history", [])
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    return jsonify({
        "total": len(history),
        "items": history[offset: offset + limit],
    })


@app.get("/api/stats")
def api_stats():
    state = load_state()
    history = state.get("history", [])
    config = load_config()

    tv_count = sum(1 for h in history if h.get("type") == "tv")
    movie_count = sum(1 for h in history if h.get("type") == "movie")

    watched_shows: set[str] = set()
    watched_movies: set[str] = set()
    for lib in config.get("libraries", []):
        if lib.get("type") in ("tv", "animation"):
            watched_shows.update(lib.get("watchlist", []))
        elif lib.get("type") == "movie":
            watched_movies.update(lib.get("watchlist", []))

    return jsonify({
        "episodes_queued": tv_count,
        "movies_queued": movie_count,
        "shows_watched": len(watched_shows),
        "movies_watched": len(watched_movies),
        "last_run": state.get("last_run"),
        "schedule_hours": config.get("schedule_hours", 6),
    })


_qbit_health_cache: dict = {"ts": 0.0, "data": None}


@app.get("/api/qbit-health")
def api_qbit_health():
    """Live qBittorrent connectivity check for the dashboard's VPN-down banner.
    Cached ~20s so frequent polling doesn't hammer qBit."""
    now = time.time()
    if _qbit_health_cache["data"] is not None and now - _qbit_health_cache["ts"] < 20:
        return jsonify(_qbit_health_cache["data"])
    try:
        qbit = _make_qbit()
        h = qbit.network_health()
        data = {
            "online": bool(h.get("up", True)),
            "known": bool(h.get("known", False)),
            "status": h.get("status", ""),
            "dht_nodes": h.get("dht_nodes", 0),
        }
    except Exception as exc:  # pylint: disable=broad-except
        log.debug("[qbit-health] failed: %s", exc)
        data = {"online": True, "known": False, "status": "unreachable", "dht_nodes": 0}
    _qbit_health_cache.update({"ts": now, "data": data})
    return jsonify(data)


@app.get("/api/config-warnings")
def api_config_warnings():
    """Surface config.json sanity-check warnings (unreachable library paths,
    missing source credentials, placeholder webhook, feature-flag mismatches,
    etc.) so the dashboard can show a banner instead of silently misbehaving."""
    try:
        from downloader import validate_config
        warnings = validate_config(load_config())
    except Exception as exc:
        log.warning("[Config] validation failed: %s", exc)
        warnings = []
    return jsonify({"warnings": warnings, "count": len(warnings)})


@app.get("/api/queue")
def api_queue():
    torrents = get_qbit_torrents()
    simplified = []
    for t in torrents:
        eta_raw = t.get("eta", 0)
        # qBit returns 8640000 for infinite ETA
        eta_sec = None if (eta_raw is None or eta_raw >= 8640000) else int(eta_raw)
        simplified.append({
            "name": t.get("name", ""),
            "hash": t.get("hash", ""),
            "state": t.get("state", ""),
            "progress": round(t.get("progress", 0) * 100, 1),
            "size_gb": round(t.get("size", 0) / 1024 ** 3, 2),
            "downloaded_gb": round(t.get("downloaded", 0) / 1024 ** 3, 2),
            "dlspeed_mb": round(t.get("dlspeed", 0) / 1024 ** 2, 2),
            "upspeed_mb": round(t.get("upspeed", 0) / 1024 ** 2, 2),
            "eta_sec": eta_sec,
            "num_seeds": t.get("num_complete", 0),
            "num_leechs": t.get("num_incomplete", 0),
            "save_path": t.get("save_path", ""),
            "category": t.get("category", ""),
        })
    return jsonify(simplified)


@app.post("/api/qbit/delete")
def api_qbit_delete():
    """Delete a torrent from qBittorrent by hash."""
    body = request.json or {}
    torrent_hash = body.get("hash", "")
    delete_files = body.get("delete_files", False)
    if not torrent_hash:
        return jsonify({"error": "hash required"}), 400
    try:
        qbit = _make_qbit()
        ok = qbit.delete_torrent(torrent_hash, delete_files=delete_files)
        return jsonify({"ok": ok})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/tvmaze/search")
def api_tvmaze_search():
    """Search TVMaze for show name suggestions (used by Settings autocomplete)."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    try:
        import requests as req
        r = req.get(
            "https://api.tvmaze.com/search/shows",
            params={"q": q},
            timeout=5,
        )
        r.raise_for_status()
        results = []
        for item in r.json()[:8]:
            show = item.get("show", {})
            name = show.get("name", "")
            year = ""
            premiered = show.get("premiered", "") or ""
            if premiered:
                year = premiered[:4]
            network = (show.get("network") or show.get("webChannel") or {}).get("name", "")
            poster = (show.get("image") or {}).get("medium", "")
            results.append({
                "name": name,
                "year": year,
                "network": network,
                "poster": poster,
            })
        return jsonify(results)
    except Exception as exc:
        log.debug("TVMaze search failed: %s", exc)
        return jsonify([])


@app.get("/api/scan-status")
def api_scan_status():
    state = load_state()
    ss = state.get("scan_status", {})
    return jsonify({
        "status":       ss.get("status", "idle"),
        "detail":       ss.get("detail", ""),
        "updated_at":   ss.get("updated_at"),
        "lib_name":     ss.get("lib_name", ""),
        "lib_idx":      ss.get("lib_idx", 0),
        "lib_total":    ss.get("lib_total", 0),
        "show_idx":     ss.get("show_idx", 0),
        "show_total":   ss.get("show_total", 0),
        "show_name":    ss.get("show_name", ""),
        "last_run":     state.get("last_run"),
        "schedule_hours": load_config().get("schedule_hours", 6),
    })


@app.get("/api/library")
def api_library():
    """Return the cached library snapshot from the last scan."""
    state = load_state()
    snapshot = state.get("library_snapshot", {})
    return jsonify({
        "data":       snapshot.get("data", {}),
        "updated_at": snapshot.get("updated_at"),
    })


@app.get("/api/group-reliability")
def api_group_reliability():
    """
    Return what the system has learned about release-group reliability.
    Every time validator deletes a corrupt download, it increments a counter
    for the release group that produced it. The torrent finder then uses these
    counts to deprioritise (≥3) or fully exclude (≥5) groups going forward.
    """
    from torrent_finder import TorrentFinder
    state = load_state()
    counts = state.get("group_failure_count", {}) or {}
    blacklist = state.get("torrent_blacklist", {}) or {}

    deprio_threshold = TorrentFinder.GROUP_DEPRIO_THRESHOLD
    block_threshold  = TorrentFinder.GROUP_BLOCK_THRESHOLD

    groups = []
    for grp, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if n >= block_threshold:
            status = "blocked"
        elif n >= deprio_threshold:
            status = "deprioritised"
        else:
            status = "ok"
        groups.append({"group": grp, "failures": n, "status": status})

    return jsonify({
        "groups":            groups,
        "deprio_threshold":  deprio_threshold,
        "block_threshold":   block_threshold,
        "total_blacklisted": sum(len(v) for v in blacklist.values() if isinstance(v, list)),
    })


def _parse_state_key(key: str) -> dict:
    """Turn a state key (tv::Show::S03E05 / tv::Show::S03::pack / movie::T::Year)
    into a friendly label + structured fields for the progress view."""
    parts = (key or "").split("::")
    kind = parts[0] if parts else ""
    if kind == "tv" and len(parts) >= 3:
        show = parts[1]
        tag = parts[2]
        m = re.match(r"[sS](\d+)[eE](\d+)", tag)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            return {"kind": "tv", "show": show, "season": s, "episode": e,
                    "label": f"{show} · S{s:02d}E{e:02d}"}
        sm = re.match(r"[sS](\d+)", tag)
        if sm:
            s = int(sm.group(1))
            return {"kind": "tv", "show": show, "season": s, "episode": None,
                    "label": f"{show} · Season {s} (pack)"}
        return {"kind": "tv", "show": show, "season": None, "episode": None,
                "label": f"{show} · {tag}"}
    if kind == "movie" and len(parts) >= 3:
        return {"kind": "movie", "show": parts[1], "season": None, "episode": None,
                "label": f"{parts[1]} ({parts[2]})"}
    return {"kind": kind or "?", "show": key, "season": None, "episode": None, "label": key}


@app.get("/api/download-progress")
def api_download_progress():
    """Per-episode download health, joined from the bookkeeping the pipeline
    already maintains — so the user can see real wins (downloaded + passed
    validation) vs. churn (corrupt release removed, retrying) vs. give-ups.

    Status precedence per episode:
      exhausted  – all release sources tried, still no good copy
      validated  – downloaded AND passed full integrity validation (a real win)
      search_failed – no torrent found yet (in the retry/backoff queue)
      retrying   – a previous release was removed as corrupt; trying another
      downloading – in flight (downloading or awaiting validation)
    """
    state = load_state()
    downloaded = state.get("downloaded_torrents", {}) or {}
    queued = set(state.get("queued", []) or [])
    blacklist = state.get("torrent_blacklist", {}) or {}
    quarantine = state.get("quarantine", {}) or {}
    retry_queue = state.get("retry_queue", {}) or {}

    # unresolved_downloads.episodes are label strings ("Show Name S03E05"); map
    # them back to a likely tv state key so we can join attempts/blacklist.
    unresolved_keys: set[str] = set()
    extra_unresolved: list[str] = []
    unresolved = state.get("unresolved_downloads", {}) or {}
    for item in unresolved.get("episodes", []) or []:
        if isinstance(item, dict):
            k = item.get("state_key") or item.get("key")
            if k:
                unresolved_keys.add(k)
                continue
            item = item.get("label") or item.get("show") or ""
        if isinstance(item, str) and item.strip():
            m = re.match(r"^(.*?)\s+([sS]\d+(?:[eE]\d+)?.*)$", item.strip())
            if m:
                unresolved_keys.add(f"tv::{m.group(1)}::{m.group(2)}")
            else:
                extra_unresolved.append(item.strip())

    # Best-effort live qBit progress, matched by release name.
    qbit_by_name: dict = {}
    try:
        for t in get_qbit_torrents():
            nm = (t.get("name") or "").strip()
            if nm:
                qbit_by_name[nm] = t
    except Exception:  # pylint: disable=broad-except
        qbit_by_name = {}

    def _match_qbit(release: str):
        if not release:
            return None
        t = qbit_by_name.get(release)
        if t is None:
            rl = release.lower()
            for nm, cand in qbit_by_name.items():
                low = nm.lower()
                if rl in low or low in rl:
                    t = cand
                    break
        if not t:
            return None
        prog = t.get("progress", 0) or 0
        return {
            "state": t.get("state", ""),
            "progress": round(prog * 100, 1),
            "dlspeed_mb": round((t.get("dlspeed", 0) or 0) / 1024 ** 2, 2),
            "num_seeds": t.get("num_complete", 0),
        }

    all_keys = set(downloaded) | queued | set(retry_queue) | unresolved_keys

    episodes = []
    for key in all_keys:
        info = _parse_state_key(key)
        dt = downloaded.get(key, {}) or {}
        attempts = len(blacklist.get(key, []) or [])
        in_quarantine = key in quarantine
        validated = bool(dt.get("validated_ok"))

        # Live qBit lookup for anything that might still be in flight.
        qb = None
        if not validated and key not in unresolved_keys and key not in retry_queue:
            qb = _match_qbit(dt.get("release", ""))
        actively_downloading = bool(qb and qb.get("progress", 100) < 100)

        if key in unresolved_keys:
            status = "exhausted"
        elif validated:
            status = "validated"
        elif key in retry_queue:
            status = "search_failed"
        elif attempts > 0 or in_quarantine:
            status = "retrying"
        elif actively_downloading:
            status = "downloading"
        else:
            # In our books but not actively downloading and not integrity-checked:
            # either finished and awaiting validation, or finished long ago unstamped.
            status = "pending"

        rq = retry_queue.get(key, {}) or {}
        episodes.append({
            "key": key,
            "label": info["label"],
            "show": info["show"],
            "season": info["season"],
            "episode": info["episode"],
            "kind": info["kind"],
            "status": status,
            "release": dt.get("release", "") or rq.get("label", ""),
            "queued_at": dt.get("queued_at", ""),
            "validated_at": dt.get("validated_at", ""),
            "failed_attempts": attempts,
            "search_attempts": rq.get("attempts", 0),
            "qbit": qb,
        })

    # Labels we couldn't map back to a state key — still surface them as exhausted.
    for lbl in extra_unresolved:
        episodes.append({
            "key": lbl, "label": lbl, "show": lbl, "season": None, "episode": None,
            "kind": "tv", "status": "exhausted", "release": "", "queued_at": "",
            "validated_at": "", "failed_attempts": 0, "search_attempts": 0, "qbit": None,
        })

    summary = {"validated": 0, "pending": 0, "downloading": 0, "retrying": 0,
               "search_failed": 0, "exhausted": 0}
    for e in episodes:
        summary[e["status"]] = summary.get(e["status"], 0) + 1
    # True total of releases ever discarded as corrupt (independent of current keys).
    summary["corrupt_removed"] = sum(
        len(v) for v in blacklist.values() if isinstance(v, list))

    # Bucket by status; keep the small actionable buckets in full and cap the
    # large ones (the summary still carries the true totals). Optional ?status=
    # filter returns one bucket uncapped for drill-down.
    buckets: dict = {}
    for e in episodes:
        buckets.setdefault(e["status"], []).append(e)
    for st, items in buckets.items():
        if st == "validated":
            items.sort(key=lambda e: (e["validated_at"] or e["queued_at"] or ""), reverse=True)
        elif st in ("downloading", "retrying"):
            items.sort(key=lambda e: (e["qbit"] or {}).get("progress", 0), reverse=True)

    want = (request.args.get("status") or "").strip()
    if want and want in buckets:
        items = buckets[want]
        return jsonify({
            "summary": summary, "total_episodes": len(episodes),
            "shown": len(items), "episodes": items, "status_filter": want,
            "qbit_online": bool(qbit_by_name), "last_run": state.get("last_run"),
        })

    caps = {"search_failed": 150, "pending": 200, "validated": 100}
    order = ["exhausted", "retrying", "downloading", "search_failed", "pending", "validated"]
    trimmed = []
    for st in order:
        items = buckets.get(st, [])
        cap = caps.get(st)
        trimmed.extend(items if cap is None else items[:cap])

    return jsonify({
        "summary": summary,
        "total_episodes": len(episodes),
        "shown": len(trimmed),
        "episodes": trimmed,
        "qbit_online": bool(qbit_by_name),
        "last_run": state.get("last_run"),
    })


@app.get("/api/library/live")
def api_library_live():
    """
    Scan the NAS paths right now and return inventory.
    May take a few seconds — used when user clicks 'Refresh Library'.
    """
    from scanner import scan_tv_library, scan_movie_library
    config = load_config()
    result = {}
    for lib in config.get("libraries", []):
        if not lib.get("enabled", True):
            continue
        name = lib.get("name", "?")
        path = lib.get("path", "")
        ltype = lib.get("type", "")
        if ltype in ("tv", "animation"):
            inv = scan_tv_library(path)
            result[name] = {
                "type": "tv",
                "path": path,
                "shows": {
                    show: {
                        "seasons": len(seasons),
                        "episodes": sum(len(eps) for eps in seasons.values()),
                        "season_detail": {
                            str(sn): sorted(list(eps))
                            for sn, eps in seasons.items()
                        },
                    }
                    for show, seasons in inv.items()
                },
            }
        elif ltype == "movie":
            inv = scan_movie_library(path)
            result[name] = {
                "type": "movie",
                "path": path,
                "movies": [
                    {
                        "title": info["title"],
                        "year":  info["year"],
                        "tmdb_id": info.get("id_value") if info.get("id_type") == "tmdb" else None,
                        "has_file": info.get("has_file", False),
                    }
                    for info in inv.values()
                ],
            }
    return jsonify(result)


@app.get("/api/poster/tv/<path:show_name>")
def api_poster_tv(show_name: str):
    url = _fetch_tv_poster(show_name)
    return jsonify({"url": url})


@app.get("/api/poster/movie/<int:tmdb_id>")
def api_poster_movie(tmdb_id: int):
    url = _fetch_movie_poster(tmdb_id)
    return jsonify({"url": url})


@app.get("/api/poster/movie-by-name/<path:movie_name>")
def api_poster_movie_by_name(movie_name: str):
    url = _fetch_movie_poster_by_name(movie_name)
    return jsonify({"url": url})


@app.get("/api/show/detail/<path:show_name>")
def api_show_detail(show_name: str):
    """
    Returns full season/episode breakdown for a show.
    Merges disk inventory with TVMaze aired episode list.
    """
    from scanner import scan_tv_library, clean_show_name
    from tvmaze_client import TVMazeClient

    config = load_config()
    disk_seasons: dict = {}

    # Find the library path for this show (match by clean name)
    for lib in config.get("libraries", []):
        if lib.get("type") != "tv" or not lib.get("enabled", True):
            continue
        inv = scan_tv_library(lib.get("path", ""))
        for raw_k, v in inv.items():
            clean_k = clean_show_name(raw_k)
            if clean_k.lower() == show_name.lower() or show_name.lower() in clean_k.lower():
                disk_seasons = {str(sn): sorted(list(eps)) for sn, eps in v.items()}
                break
        if disk_seasons:
            break

    # Get expected episodes from TVMaze
    client = TVMazeClient()
    tvmaze_data = client.get_all_episodes(show_name)
    expected_seasons: dict = {}
    poster_url = ""
    show_meta: dict = {}

    if tvmaze_data:
        show_meta = tvmaze_data["show"]
        img = show_meta.get("image") or {}
        poster_url = img.get("original") or img.get("medium") or ""
        for sn, eps in tvmaze_data["episodes"].items():
            expected_seasons[str(sn)] = [e["episode_number"] for e in eps]

    # Merge
    all_seasons = sorted(set(list(disk_seasons.keys()) + list(expected_seasons.keys())), key=int)
    seasons = []
    for sn in all_seasons:
        on_disk = disk_seasons.get(sn, [])
        expected = expected_seasons.get(sn, [])
        missing = [e for e in expected if e not in on_disk]
        seasons.append({
            "season": int(sn),
            "on_disk": on_disk,
            "expected": expected,
            "missing": missing,
            "complete": len(missing) == 0 and len(expected) > 0,
        })

    return jsonify({
        "show": show_name,
        "poster_url": poster_url,
        "tvmaze_id": show_meta.get("id"),
        "premiered": show_meta.get("premiered", "")[:4] if show_meta.get("premiered") else "",
        "status": show_meta.get("status", ""),
        "network": (show_meta.get("network") or show_meta.get("webChannel") or {}).get("name", ""),
        "seasons": seasons,
        "total_on_disk": sum(len(s["on_disk"]) for s in seasons),
        "total_expected": sum(len(s["expected"]) for s in seasons),
        "total_missing": sum(len(s["missing"]) for s in seasons),
    })


@app.get("/api/library/inventory")
def api_library_inventory():
    """
    Fast NAS-only scan — no external API calls.
    Returns disk inventory grouped by library.
    """
    from scanner import scan_tv_library, scan_movie_library, clean_show_name
    config = load_config()
    result = {}
    for lib in config.get("libraries", []):
        if not lib.get("enabled", True):
            continue
        name  = lib.get("name", "?")
        path  = lib.get("path", "")
        ltype = lib.get("type", "")
        if ltype in ("tv", "animation"):
            inv = scan_tv_library(path)
            shows_out = {}
            for raw_show, seasons in inv.items():
                display = clean_show_name(raw_show)
                shows_out[display] = {
                    "seasons": len(seasons),
                    "episodes": sum(len(eps) for eps in seasons.values()),
                    "season_detail": {
                        str(sn): sorted(list(eps))
                        for sn, eps in seasons.items()
                    },
                    "on_nas": True,
                    "watching": display in [w for w in lib.get("watchlist", [])],
                }
            # Include watchlist shows not yet on NAS
            for wl_show in lib.get("watchlist", []):
                if wl_show not in shows_out:
                    shows_out[wl_show] = {
                        "seasons": 0,
                        "episodes": 0,
                        "season_detail": {},
                        "on_nas": False,
                        "watching": True,
                    }
            result[name] = {
                "type": "tv",
                "path": path,
                "shows": shows_out,
            }
        elif ltype == "movie":
            inv = scan_movie_library(path)
            on_nas_keys = set()
            movies_out = []
            for info in inv.values():
                title_year = f"{info['title']} ({info['year']})" if info.get("year") else info.get("title", "")
                on_nas_keys.add(title_year)
                movies_out.append({
                    "title":    info["title"],
                    "year":     info["year"],
                    "tmdb_id":  int(info["id_value"]) if info.get("id_type") == "tmdb" and info.get("id_value") else None,
                    "has_file": info.get("has_file", False),
                    "on_nas":   True,
                    "watching": title_year in lib.get("watchlist", []),
                })
            # Include watchlist movies not yet on NAS
            for wl_movie in lib.get("watchlist", []):
                if wl_movie not in on_nas_keys:
                    # Parse "Title (Year)" format
                    import re as _re
                    m = _re.match(r"^(.+?)\s*\((\d{4})\)$", wl_movie)
                    movies_out.append({
                        "title":    m.group(1).strip() if m else wl_movie,
                        "year":     int(m.group(2)) if m else None,
                        "tmdb_id":  None,
                        "has_file": False,
                        "on_nas":   False,
                        "watching": True,
                    })
            result[name] = {
                "type": "movie",
                "path": path,
                "movies": movies_out,
            }
    return jsonify(result)


# ---------------------------------------------------------------------------
# Libraries overview — per-library inventory with TVMaze "up to date" check
# ---------------------------------------------------------------------------

# In-process cache: {show_name_lower: {"total_episodes": N, "latest_season": N, "ts": float}}
_tvmaze_status_cache: dict = {}
_TVMAZE_CACHE_TTL = 3600  # 1 hour

_TVMAZE_DISK_CACHE_TTL = 86400  # 24 hours — saves repeated API calls for large libraries
_tvmaze_disk_cache: dict = {}
_tvmaze_disk_cache_loaded = False
_tvmaze_disk_lock = threading.Lock()

def _load_tvmaze_disk_cache() -> None:
    global _tvmaze_disk_cache, _tvmaze_disk_cache_loaded
    if _tvmaze_disk_cache_loaded:
        return
    try:
        if TVMAZE_DISK_CACHE.exists():
            _tvmaze_disk_cache = json.loads(TVMAZE_DISK_CACHE.read_text("utf-8"))
    except Exception:
        _tvmaze_disk_cache = {}
    _tvmaze_disk_cache_loaded = True

def _save_tvmaze_disk_cache() -> None:
    try:
        TVMAZE_DISK_CACHE.write_text(json.dumps(_tvmaze_disk_cache, indent=2), "utf-8")
    except Exception:
        pass

def _tvmaze_show_status(show_name: str, force_refresh: bool = False) -> dict:
    """
    Return TVMaze totals for a show.
    Cached in memory (1 hr) AND on disk (24 hr) to avoid hammering the API.
    """
    import time as _t
    key = show_name.lower()

    # 1. In-memory cache (fast)
    if not force_refresh:
        cached = _tvmaze_status_cache.get(key)
        if cached and (_t.time() - cached.get("ts", 0)) < _TVMAZE_CACHE_TTL:
            return cached

    # 2. Disk cache (survives server restarts)
    with _tvmaze_disk_lock:
        _load_tvmaze_disk_cache()
        disk = _tvmaze_disk_cache.get(key)
        if not force_refresh and disk and (_t.time() - disk.get("ts", 0)) < _TVMAZE_DISK_CACHE_TTL:
            _tvmaze_status_cache[key] = disk
            return disk

    # 3. Live API call
    from tvmaze_client import TVMazeClient
    try:
        data = TVMazeClient().get_all_episodes(show_name)
        if not data:
            return {}
        seasons: dict = {}
        ep_titles: dict = {}  # {season: {ep_num: title}}
        for s_num, eps in data["episodes"].items():
            seasons[s_num] = len(eps)
            ep_titles[s_num] = {ep["episode_number"]: ep.get("name", "") for ep in eps}
        show_obj   = data.get("show", {})
        show_status = show_obj.get("status", "")  # "Ended", "Running", "In Development", etc.
        result = {
            "total_episodes": sum(seasons.values()),
            "latest_season":  max(seasons.keys()) if seasons else 0,
            "seasons":        seasons,
            "ep_titles":      ep_titles,
            "show_status":    show_status,
            "ts":             _t.time(),
        }
        _tvmaze_status_cache[key] = result
        with _tvmaze_disk_lock:
            _tvmaze_disk_cache[key] = result
            _save_tvmaze_disk_cache()
        return result
    except Exception:
        return {}


@app.get("/api/libraries/scan-status")
def api_libraries_scan_status():
    """Poll for background NAS scan progress."""
    return jsonify(_scan_progress)


def _load_library_cache() -> dict | None:
    """Return cached library data if it exists and is fresh enough."""
    try:
        if LIBRARY_CACHE.exists():
            raw = json.loads(LIBRARY_CACHE.read_text("utf-8"))
            return raw
    except Exception:
        pass
    return None


def _save_library_cache(data: list) -> None:
    try:
        LIBRARY_CACHE.write_text(
            json.dumps({"ts": time.time(), "data": data}, indent=2),
            "utf-8",
        )
    except Exception:
        pass


_lib_scan_lock = threading.Lock()


def _run_library_scan_bg() -> None:
    """Run a full library NAS scan + TVMaze enrichment in background, update cache."""
    global _scan_progress
    if not _lib_scan_lock.acquire(blocking=False):
        return  # already running
    try:
        import concurrent.futures
        from scanner import scan_tv_library, scan_movie_library, clean_show_name
        from pathlib import Path as _Path
        import re as _re2

        _scan_progress = {"running": True, "pct": 0, "label": "Loading config…", "started_at": time.time()}
        config       = load_config()
        enabled_libs = [lib for lib in config.get("libraries", []) if lib.get("enabled", True)]
        total        = max(len(enabled_libs), 1)

        # Step 1: parallel NAS scan
        _scan_progress["label"] = f"Scanning {total} library path(s)…"

        def _scan_lib(lib):
            ltype = lib.get("type", "")
            path  = lib.get("path", "")
            try:
                if ltype in ("tv", "animation"):
                    return lib, scan_tv_library(path)
                elif ltype == "movie":
                    return lib, scan_movie_library(path)
            except Exception as e:
                logger.warning("[LibScan] Failed %s: %s", path, e)
            return lib, {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            scan_results = list(pool.map(_scan_lib, enabled_libs))

        _scan_progress["pct"] = 40
        _scan_progress["label"] = "Fetching episode counts from TVMaze…"

        # Step 2: collect shows needing TVMaze refresh
        _load_tvmaze_disk_cache()
        shows_to_refresh: list[str] = []
        for lib, inv in scan_results:
            if lib.get("type") not in ("tv", "animation"):
                continue
            for raw_show in inv:
                display = clean_show_name(raw_show)
                key = display.lower()
                disk = _tvmaze_disk_cache.get(key)
                if not disk or (time.time() - disk.get("ts", 0)) > _TVMAZE_DISK_CACHE_TTL:
                    shows_to_refresh.append(display)
            for wl_show in lib.get("watchlist", []):
                key = wl_show.lower()
                disk = _tvmaze_disk_cache.get(key)
                if not disk or (time.time() - disk.get("ts", 0)) > _TVMAZE_DISK_CACHE_TTL:
                    shows_to_refresh.append(wl_show)

        done_count = [0]
        mz_total   = max(len(shows_to_refresh), 1)

        def _refresh_mz(show_name):
            try:
                _tvmaze_show_status(show_name)
            except Exception:
                pass
            done_count[0] += 1
            _scan_progress["pct"] = 40 + int(50 * done_count[0] / mz_total)
            _scan_progress["label"] = f"TVMaze: {done_count[0]}/{mz_total} shows…"

        if shows_to_refresh:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(_refresh_mz, shows_to_refresh))

        _scan_progress["pct"] = 90
        _scan_progress["label"] = "Building response…"

        # Step 3: assemble result
        result = []
        for lib, inv in scan_results:
            name  = lib.get("name", "?")
            path  = lib.get("path", "")
            ltype = lib.get("type", "")
            lib_out: dict = {"name": name, "path": path, "type": ltype, "items": []}

            if ltype in ("tv", "animation"):
                watchlist_set = {w.lower() for w in lib.get("watchlist", [])}
                processed: set = set()

                for raw_show, seasons in sorted(inv.items()):
                    display     = clean_show_name(raw_show)
                    disk_total  = sum(len(eps) for eps in seasons.values())
                    disk_seasons = {str(sn): sorted(list(eps)) for sn, eps in seasons.items()}
                    poster      = get_poster_url(display)
                    mz          = _tvmaze_disk_cache.get(display.lower(), {})

                    if mz:
                        mz_total_ep = mz.get("total_episodes")
                        mz_season   = mz.get("latest_season")
                        # Normalise TVMaze season keys to int (JSON stores them as str)
                        mz_seasons  = {int(k): v for k, v in mz.get("seasons", {}).items()}
                        missing_eps = max(0, mz_total_ep - disk_total) if mz_total_ep else None
                        up_to_date  = missing_eps == 0 if missing_eps is not None else None
                    else:
                        mz_total_ep = mz_season = missing_eps = up_to_date = None
                        mz_seasons = {}

                    # Normalise disk season keys to int so sort never mixes int/str
                    int_seasons = {int(k): v for k, v in seasons.items()}
                    season_status = []
                    for s_num in sorted(set(list(int_seasons.keys()) + list(mz_seasons.keys()))):
                        on_disk  = len(int_seasons.get(s_num, set()))
                        expected = mz_seasons.get(s_num, 0)
                        season_status.append({
                            "season": s_num, "on_disk": on_disk, "expected": expected,
                            "complete": on_disk >= expected if expected else True,
                        })

                    lib_out["items"].append({
                        "title":             display,
                        "folder":            raw_show,
                        "path":              str(_Path(path) / raw_show),
                        "on_nas":            True,
                        "watching":          display.lower() in watchlist_set,
                        "seasons_on_disk":   len(seasons),
                        "episodes_on_disk":  disk_total,
                        "total_seasons":     mz_season,
                        "total_episodes":    mz_total_ep,
                        "missing_episodes":  missing_eps,
                        "up_to_date":        up_to_date,
                        "season_detail":     disk_seasons,
                        "season_status":     season_status,
                        "poster":            poster,
                    })
                    processed.add(display.lower())

                for wl_show in lib.get("watchlist", []):
                    if wl_show.lower() not in processed:
                        poster = get_poster_url(wl_show)
                        mz     = _tvmaze_disk_cache.get(wl_show.lower(), {})
                        lib_out["items"].append({
                            "title":             wl_show,
                            "folder":            None,
                            "path":              None,
                            "on_nas":            False,
                            "watching":          True,
                            "seasons_on_disk":   0,
                            "episodes_on_disk":  0,
                            "total_seasons":     mz.get("latest_season"),
                            "total_episodes":    mz.get("total_episodes"),
                            "missing_episodes":  mz.get("total_episodes"),
                            "up_to_date":        False,
                            "season_detail":     {},
                            "season_status":     [],
                            "poster":            poster,
                        })

            elif ltype == "movie":
                watchlist_set = set(lib.get("watchlist", []))
                on_nas_keys: set = set()

                for info in sorted(inv.values(), key=lambda x: x.get("title", "")):
                    title_year = f"{info['title']} ({info['year']})" if info.get("year") else info.get("title", "")
                    on_nas_keys.add(title_year)
                    tmdb_id = int(info["id_value"]) if info.get("id_type") == "tmdb" and info.get("id_value") else None
                    poster  = get_poster_url(title_year, tmdb_id=tmdb_id, media_type="movie")
                    lib_out["items"].append({
                        "title":    info["title"],
                        "year":     info["year"],
                        "folder":   _Path(info["path"]).name if info.get("path") else None,
                        "path":     info.get("path"),
                        "on_nas":   True,
                        "has_file": info.get("has_file", False),
                        "watching": title_year in watchlist_set,
                        "poster":   poster,
                        "tmdb_id":  tmdb_id,
                    })

                for wl_movie in lib.get("watchlist", []):
                    if wl_movie not in on_nas_keys:
                        m = _re2.match(r"^(.+?)\s*\((\d{4})\)$", wl_movie)
                        poster = get_poster_url(wl_movie, media_type="movie")
                        lib_out["items"].append({
                            "title":    m.group(1).strip() if m else wl_movie,
                            "year":     int(m.group(2)) if m else None,
                            "folder":   None,
                            "path":     None,
                            "on_nas":   False,
                            "has_file": False,
                            "watching": True,
                            "poster":   poster,
                            "tmdb_id":  None,
                        })

            result.append(lib_out)

        _save_library_cache(result)
        _scan_progress = {"running": False, "pct": 100, "label": "Done", "started_at": None}
        logger.info("[LibScan] Background scan complete — %d libraries cached", len(result))

    except Exception as exc:
        logger.error("[LibScan] Background scan error: %s", exc, exc_info=True)
        # Save whatever partial results were built before the crash
        if result:
            try:
                _save_library_cache(result)
                logger.info("[LibScan] Saved %d partial library results after error", len(result))
            except Exception:
                pass
        _scan_progress = {"running": False, "pct": -1, "label": f"Scan error: {exc}", "started_at": None}
    finally:
        _lib_scan_lock.release()


@app.get("/api/libraries/overview")
def api_libraries_overview():
    """
    Always returns immediately — never blocks the UI.
    - If cache exists and is fresh: return it.
    - If cache is stale or missing: start background scan and return whatever
      is cached now (may be empty on first run). Frontend polls scan-status.
    Pass ?force=1 to discard cache and force a fresh background scan.
    """
    force = request.args.get("force", "0") == "1"
    cached = _load_library_cache()
    cache_age = time.time() - cached.get("ts", 0) if cached else float("inf")
    cache_data = cached.get("data", []) if cached else []

    need_scan = force or not cached or cache_age > LIBRARY_CACHE_TTL

    if need_scan and not _scan_progress.get("running"):
        threading.Thread(target=_run_library_scan_bg, daemon=True, name="LibScanBG").start()

    return jsonify({"data": cache_data, "scanning": _scan_progress.get("running", False)})


# ---------------------------------------------------------------------------
# Config save helper
# ---------------------------------------------------------------------------

def save_config(config: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Config / Settings API
# ---------------------------------------------------------------------------

@app.get("/api/config/settings")
def api_config_settings():
    """Return the current config (passwords redacted) for the settings UI."""
    config = load_config()
    safe = json.loads(json.dumps(config))  # deep copy
    for s in safe.get("torrent_sources", []):
        if s.get("password"):
            s["password"] = "••••••••"
    if safe.get("qbittorrent", {}).get("password"):
        safe["qbittorrent"]["password"] = "••••••••"
    return jsonify(safe)


@app.post("/api/config/notifications")
def api_config_save_notifications():
    """Save Discord webhook and Ntfy topic."""
    body = request.json or {}
    config = load_config()
    notif  = config.setdefault("notifications", {})
    webhook = body.get("discord_webhook", "").strip()
    ntfy    = body.get("ntfy_topic", "").strip()
    if webhook:
        notif["discord_webhook"] = webhook
    if ntfy is not None:
        notif["ntfy_topic"] = ntfy
    save_config(config)
    # Quick test-fire to confirm the webhook works
    test = body.get("test", False)
    if test and webhook and "your_webhook" not in webhook:
        try:
            import requests as _r
            _r.post(webhook, json={"embeds": [{
                "title":       "✅ Jellyfin Downloader Connected",
                "description": "Discord notifications are working!",
                "color":       0x57F287,
                "footer":      {"text": "Jellyfin Downloader"},
            }]}, timeout=8)
        except Exception:
            pass
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Library type catalogue — fixed set the UI must use
# ---------------------------------------------------------------------------
LIBRARY_TYPE_OPTIONS = [
    {"label": "TV Shows",                      "value": "tv"},
    {"label": "Movies",                        "value": "movie"},
    {"label": "Adult Animations & Cartoons",   "value": "animation"},
]

_LABEL_TO_TYPE = {opt["label"]: opt["value"] for opt in LIBRARY_TYPE_OPTIONS}
_TYPE_TO_LABEL = {opt["value"]: opt["label"] for opt in LIBRARY_TYPE_OPTIONS}


def _type_for_label(label: str) -> str | None:
    """Map a display label (e.g. 'Adult Animations & Cartoons') → internal type string."""
    return _LABEL_TO_TYPE.get(label)


@app.get("/api/config/library-type-options")
def api_library_type_options():
    return jsonify(LIBRARY_TYPE_OPTIONS)


@app.post("/api/config/libraries")
def api_config_save_libraries():
    """Save the libraries array (paths, names, types, auto_discover, enabled)."""
    data = request.json or {}
    libraries = data.get("libraries", [])
    config = load_config()
    # Merge: keep existing watchlist and other per-lib keys not sent from UI
    # Key is now (name, path) since multiple libs can share a name (same type, different drives)
    existing_by_key = {(lib.get("name", ""), lib.get("path", "")): lib
                       for lib in config.get("libraries", [])}
    merged = []
    for lib in libraries:
        name = lib.get("name", "")
        path = lib.get("path", "")
        base = dict(existing_by_key.get((name, path),
                    # fallback: match just by path for renamed entries
                    next((v for v in existing_by_key.values() if v.get("path") == path), {})))
        base.update({
            "name":          name,
            "type":          _type_for_label(name) or lib.get("type", base.get("type", "tv")),
            "path":          path,
            "enabled":       lib.get("enabled", base.get("enabled", True)),
            "auto_discover": lib.get("auto_discover", base.get("auto_discover", True)),
        })
        if "watchlist" not in base:
            base["watchlist"] = []
        merged.append(base)
    config["libraries"] = merged
    try:
        save_config(config)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/config/sources")
def api_config_save_sources():
    """Save the torrent_sources array (SceneTime, Jackett, Prowlarr, etc.)."""
    data = request.json or {}
    sources = data.get("torrent_sources", [])
    config = load_config()
    existing = {s.get("name", "").lower(): s for s in config.get("torrent_sources", [])}

    updated = []
    for src in sources:
        name_key = src.get("name", "").lower()
        merged = dict(existing.get(name_key, {}))  # start with existing (keeps real password)
        for k, v in src.items():
            # Don't overwrite password with redacted placeholder
            if k == "password" and set(v) <= {"•"}:
                continue
            merged[k] = v
        # Ensure type field is set based on name for new sources
        if "type" not in merged:
            n = merged.get("name", "").lower()
            if "jackett" in n:
                merged["type"] = "jackett"
            elif "prowlarr" in n:
                merged["type"] = "prowlarr"
            else:
                merged["type"] = "scenetime"
        updated.append(merged)

    config["torrent_sources"] = updated
    try:
        save_config(config)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/config/excluded-shows")
def api_config_save_excluded():
    """Save the excluded_shows list — shows the downloader will permanently skip."""
    data = request.json or {}
    shows = [s.strip() for s in data.get("excluded_shows", []) if s.strip()]
    config = load_config()
    config["excluded_shows"] = shows
    try:
        save_config(config)
        return jsonify({"ok": True, "excluded_shows": shows})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/config/watchlist")
def api_config_save_watchlist():
    """Save watchlist entries for each library by name."""
    data = request.json or {}
    # data = {"TV Shows": ["The Bear", "Severance"], "Movies": ["Dune Part Two (2024)"]}
    config = load_config()
    for lib in config.get("libraries", []):
        lib_name = lib.get("name", "")
        if lib_name in data:
            lib["watchlist"] = data[lib_name]
    try:
        save_config(config)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/watchlist")
def api_watchlist():
    """Return configured watchlist + NAS-discovered shows from last inventory."""
    config = load_config()
    state  = load_state()
    snapshot = state.get("library_snapshot", {})

    result = []
    for lib in config.get("libraries", []):
        lname    = lib.get("name", "?")
        ltype    = lib.get("type", "")
        watched  = lib.get("watchlist", [])

        # Collect NAS-discovered shows (from last inventory scan)
        discovered = []
        snap = snapshot.get(lname, {})
        if ltype in ("tv", "animation"):
            for show in snap.get("shows", {}).keys():
                if show not in watched:
                    discovered.append(show)
        elif ltype == "movie":
            for m in snap.get("movies", []):
                title_year = f"{m['title']} ({m['year']})" if m.get("year") else m.get("title", "")
                if title_year not in watched:
                    discovered.append(title_year)

        result.append({
            "name":       lname,
            "type":       ltype,
            "watched":    sorted(watched),
            "discovered": sorted(discovered),
        })
    return jsonify(result)


@app.get("/api/watchlist/details")
def api_watchlist_details():
    """
    Watchlist = everything that still needs attention:
    1. All NAS shows with missing episodes (regardless of explicit watchlist)
    2. Explicit watchlist shows not yet on NAS ("Searching")
    3. Explicit watchlist movies not yet on NAS

    Ended shows that have zero missing episodes are excluded (nothing to do).
    """
    try:
        return _api_watchlist_details_inner()
    except Exception as exc:
        logger.error("[Watchlist] Error: %s", exc, exc_info=True)
        return jsonify([])


def _api_watchlist_details_inner():
    config = load_config()
    _load_tvmaze_disk_cache()

    # Use library_cache for NAS data (fast; avoids live scan)
    cached_libs: list = []
    cached = _load_library_cache()
    if cached:
        cached_libs = cached.get("data", [])

    # Build lookup: lib_name → items list
    cache_by_name: dict = {lib["name"]: lib for lib in cached_libs}

    result = []

    for lib in config.get("libraries", []):
        if not lib.get("enabled", True):
            continue
        lname  = lib.get("name", "?")
        ltype  = lib.get("type", "")
        wl_set = {e.strip() for e in lib.get("watchlist", [])}
        cached_lib = cache_by_name.get(lname, {})

        if ltype in ("tv", "animation"):
            seen: set = set()

            # ── 1. NAS shows with missing episodes ──────────────────────────
            for item in cached_lib.get("items", []):
                if not item.get("on_nas"):
                    continue
                title        = item["title"]
                missing_eps  = item.get("missing_episodes") or 0
                total_eps    = item.get("total_episodes") or 0
                disk_eps     = item.get("episodes_on_disk", 0)
                seasons_disk = item.get("season_status", [])
                poster       = item.get("poster") or get_poster_url(title)
                mz           = _tvmaze_disk_cache.get(title.lower(), {})
                show_status  = mz.get("show_status", "")   # "Ended", "Running", etc.
                is_ended     = show_status.lower() in ("ended", "to be determined")

                # Build per-season missing detail
                seasons_info = []
                for s in seasons_disk:
                    miss = max(0, (s.get("expected") or 0) - (s.get("on_disk") or 0))
                    if miss:
                        seasons_info.append({
                            "season": s["season"],
                            "have":   s.get("on_disk", 0),
                            "total":  s.get("expected", 0),
                            "missing": miss,
                        })

                # Determine status
                if missing_eps == 0 and is_ended:
                    status = "ended_complete"   # nothing to do — will be hidden in UI
                elif missing_eps == 0:
                    status = "up_to_date"
                else:
                    status = "missing"

                seen.add(title.lower())
                result.append({
                    "lib_name":    lname,
                    "lib_type":    ltype,
                    "name":        title,
                    "poster":      poster,
                    "on_nas":      True,
                    "status":      status,
                    "show_status": show_status,
                    "missing_eps": missing_eps,
                    "total_eps":   total_eps,
                    "seasons":     seasons_info,
                    "on_watchlist": title.lower() in {w.lower() for w in wl_set},
                })

            # ── 2. Explicit watchlist shows NOT on NAS yet ───────────────────
            for entry in lib.get("watchlist", []):
                name = entry.strip()
                if name.lower() in seen:
                    continue
                mz          = _tvmaze_disk_cache.get(name.lower(), {})
                show_status = mz.get("show_status", "")
                poster      = get_poster_url(name, media_type="tv")
                result.append({
                    "lib_name":    lname,
                    "lib_type":    ltype,
                    "name":        name,
                    "poster":      poster,
                    "on_nas":      False,
                    "status":      "searching",
                    "show_status": show_status,
                    "missing_eps": mz.get("total_episodes", 0),
                    "total_eps":   mz.get("total_episodes", 0),
                    "seasons":     [],
                    "on_watchlist": True,
                })
                seen.add(name.lower())

        elif ltype == "movie":
            on_nas_titles: set = {
                item["title"].lower()
                for item in cached_lib.get("items", [])
                if item.get("on_nas")
            }
            for entry in lib.get("watchlist", []):
                name = entry.strip()
                import re as _re
                m = _re.match(r"^(.+?)\s*\((\d{4})\)$", name)
                title = m.group(1).strip() if m else name
                on_nas = title.lower() in on_nas_titles
                poster = get_poster_url(name, media_type="movie")
                result.append({
                    "lib_name":    lname,
                    "lib_type":    "movie",
                    "name":        name,
                    "poster":      poster,
                    "on_nas":      on_nas,
                    "status":      "on_nas" if on_nas else "searching",
                    "show_status": "",
                    "missing_eps": 0,
                    "total_eps":   0,
                    "seasons":     [],
                    "on_watchlist": True,
                })

    return jsonify(result)


# ---------------------------------------------------------------------------
# Discovery — RSS feed cross-referenced with NAS
# ---------------------------------------------------------------------------

def _run_discovery(force_refresh: bool = False) -> dict:
    """Core logic: load/fetch RSS, scan NAS, return enriched list."""
    from discovery import DiscoveryEngine
    from scanner import scan_tv_library, scan_movie_library

    config = load_config()

    # Collect RSS URL(s)
    rss_url = ""
    for src in config.get("torrent_sources", []):
        if src.get("enabled") and src.get("rss_feed"):
            rss_url = src["rss_feed"]
            break

    engine = DiscoveryEngine(rss_url)
    cache  = engine.load_cache()

    if force_refresh or not cache.get("fetched_at"):
        if not rss_url:
            return {"error": "No RSS feed URL configured", "items": [], "fetched_at": None}
        raw_items = engine.fetch()
        groups    = engine.group_by_title(raw_items)
    else:
        # Rebuild groups from cached items
        raw_items = []
        for cached in cache.get("items", []):
            for rel in cached.get("releases", []):
                raw_items.append(rel)
        groups = engine.group_by_title(raw_items)

    # NAS scan — skip paths that are temporarily unreachable
    tv_inventories    = []
    movie_inventories = []
    for lib in config.get("libraries", []):
        if not lib.get("enabled", True):
            continue
        path  = lib.get("path", "")
        ltype = lib.get("type", "")
        try:
            if ltype in ("tv", "animation"):
                tv_inventories.append(scan_tv_library(path))
            elif ltype == "movie":
                movie_inventories.append(scan_movie_library(path))
        except Exception as scan_err:
            logger.warning("[Discovery] Skipping unreachable library %s: %s", path, scan_err)

    enriched = engine.enrich_with_nas(groups, tv_inventories, movie_inventories)

    if force_refresh:
        engine.save_cache(enriched)

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat() if force_refresh else cache.get("fetched_at"),
        "items": enriched,
    }


@app.get("/api/discover")
def api_discover():
    """Return cached discovery data (fast, no external calls)."""
    try:
        return _api_discover_inner()
    except Exception as exc:
        logger.error("[Discover] Unexpected error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc), "items": [], "fetched_at": None, "needs_refresh": True}), 200

def _api_discover_inner():
    from discovery import DiscoveryEngine
    from scanner import scan_tv_library, scan_movie_library

    config = load_config()
    rss_url = ""
    for src in config.get("torrent_sources", []):
        if src.get("enabled") and src.get("rss_feed"):
            rss_url = src["rss_feed"]
            break
    engine = DiscoveryEngine(rss_url)
    cache  = engine.load_cache()
    if not cache.get("items"):
        return jsonify({"fetched_at": None, "items": [], "needs_refresh": True})

    # Re-enrich cached releases against current NAS (fast path)
    from scanner import scan_tv_library, scan_movie_library
    raw_items = [rel for item in cache["items"] for rel in item.get("releases", [])]
    groups    = engine.group_by_title(raw_items)
    tv_inv, mov_inv = [], []
    for lib in config.get("libraries", []):
        if not lib.get("enabled", True):
            continue
        lib_path = lib.get("path", "")
        try:
            if lib.get("type") in ("tv", "animation"):
                tv_inv.append(scan_tv_library(lib_path))
            elif lib.get("type") == "movie":
                mov_inv.append(scan_movie_library(lib_path))
        except Exception as scan_err:
            logger.warning("[Discover] Skipping unreachable library %s: %s", lib_path, scan_err)
    enriched = engine.enrich_with_nas(groups, tv_inv, mov_inv)
    return jsonify({"fetched_at": cache.get("fetched_at"), "items": enriched, "needs_refresh": False})


@app.post("/api/discover/refresh")
def api_discover_refresh():
    """Fetch fresh RSS data and rebuild the discovery cache."""
    try:
        result = _run_discovery(force_refresh=True)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc), "items": []}), 500


@app.post("/api/discover/add-watchlist")
def api_discover_add_watchlist():
    """Add a discovered show/movie to the first matching library watchlist."""
    data     = request.json or {}
    name     = data.get("name", "").strip()
    media_type = data.get("type", "tv")
    if not name:
        return jsonify({"ok": False, "error": "No name provided"}), 400

    config = load_config()
    for lib in config.get("libraries", []):
        if lib.get("type") == media_type:
            wl = lib.setdefault("watchlist", [])
            if name not in wl:
                wl.append(name)
            save_config(config)
            return jsonify({"ok": True, "library": lib.get("name")})
    return jsonify({"ok": False, "error": "No matching library found"}), 404


@app.post("/api/discover/download")
def api_discover_download():
    """
    Trigger the downloader for a specific show/movie discovered via RSS.
    Adds it to the watchlist first so the downloader picks it up.
    """
    data  = request.json or {}
    name  = data.get("name", "").strip()
    mtype = data.get("type", "tv")
    if not name:
        return jsonify({"ok": False, "error": "No name provided"}), 400

    # Auto-add to watchlist
    config = load_config()
    for lib in config.get("libraries", []):
        if lib.get("type") == mtype:
            wl = lib.setdefault("watchlist", [])
            if name not in wl:
                wl.append(name)
            save_config(config)
            break

    # Trigger downloader subprocess
    python = sys.executable
    cmd    = child_argv("--run-now")
    try:
        subprocess.Popen(cmd, cwd=str(BASE_DIR))
        return jsonify({"ok": True, "message": f"Added '{name}' to watchlist and triggered downloader"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/logs")
def api_logs():
    """Return the last N lines of the log file."""
    lines = int(request.args.get("lines", 100))
    if not LOG_FILE.exists():
        return jsonify({"lines": []})
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return jsonify({"lines": [l.rstrip() for l in all_lines[-lines:]]})


@app.post("/api/resend-summary")
def api_resend_summary():
    """Resend the last scan summary to Discord."""
    state = load_state()
    summary = state.get("last_scan_summary")
    if not summary:
        return jsonify({"status": "error", "detail": "No scan summary saved yet — run a scan first."}), 404
    config  = load_config()
    notif   = config.get("notifications", {})
    webhook = notif.get("discord_webhook", "")
    ntfy    = notif.get("ntfy_topic", "")
    from notifier import Notifier
    notifier = Notifier(discord_webhook=webhook, ntfy_topic=ntfy)
    try:
        notifier.scan_summary(
            tv_added=summary.get("tv_added", {}),
            tv_paths=summary.get("tv_paths", {}),
            movies_added=summary.get("movies_added", []),
            movie_paths=summary.get("movie_paths", {}),
            tv_queued={},
            not_found=summary.get("not_found", {}),
            duration_sec=summary.get("duration_sec", 0),
        )
        return jsonify({"status": "sent", "sent_at": summary.get("sent_at", "")})
    except Exception as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 500


# A run heartbeat (scan_status.updated_at) older than this is treated as a
# crashed/abandoned run so a stuck "running" flag can never block forever.
_RUN_STALE_MINUTES = 20


def active_run_status() -> Optional[dict]:
    """Return the scan_status dict when a downloader run (scheduled scan,
    catch-up campaign, aggressive run, or RSS poll) is genuinely in progress,
    else None. A run is "in progress" when scan_status.status == 'running' and
    its heartbeat is fresh; a stale heartbeat means the process died and the
    flag is abandoned, so we don't block new runs on it.

    This is the single source of truth for the "only one writer at a time" rule
    so the UI never launches a second downloader on top of a running one."""
    try:
        ss = (load_state().get("scan_status") or {})
    except Exception:
        return None
    if ss.get("status") != "running":
        return None
    ts = ss.get("updated_at")
    if ts:
        try:
            updated = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            if (datetime.now() - updated).total_seconds() > _RUN_STALE_MINUTES * 60:
                return None  # heartbeat stale → previous run died; allow a new one
        except ValueError:
            pass
    return ss


@app.post("/api/run")
def api_run():
    """Trigger an immediate downloader run in a subprocess.

    The "Download Missing" button runs in --aggressive mode: it manages and
    validates downloads concurrently with the scan, ignores retry cooldowns, and
    retries failed/stalled torrents immediately (a different release) instead of
    deferring to the next scheduled scan.

    Refuses to start if a run is already active (scheduled scan, campaign, or a
    prior click) — running two downloaders at once corrupts shared state.json
    and double-queues torrents. Pass {"force": true} to override a run you know
    is dead.
    """
    body = request.json or {}
    if not body.get("force"):
        active = active_run_status()
        if active:
            return jsonify({
                "status": "busy",
                "detail": active.get("detail") or "A download run is already in progress.",
                "scan_status": active,
            }), 409
    dry = body.get("dry_run", False)
    python = sys.executable
    cmd = child_argv("--run-now")
    if dry:
        cmd.append("--dry-run")
    else:
        cmd.append("--aggressive")
    try:
        subprocess.Popen(cmd, cwd=str(BASE_DIR))
        return jsonify({"status": "started", "dry_run": dry})
    except Exception as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 500


@app.get("/api/check")
def api_check():
    """Run pre-flight check and return results as JSON."""
    import io
    import contextlib

    results = []
    config = load_config()

    def _test(label, fn):
        try:
            ok, detail = fn()
            results.append({"label": label, "ok": ok, "detail": detail})
        except Exception as exc:
            results.append({"label": label, "ok": False, "detail": str(exc)})

    import requests as req

    # TMDB
    tmdb_key = config.get("tmdb", {}).get("api_key", "")
    if tmdb_key and "your_tmdb" not in tmdb_key:
        try:
            r = req.get("https://api.themoviedb.org/3/configuration",
                        params={"api_key": tmdb_key}, timeout=8)
            results.append({"label": "TMDB API", "ok": r.ok,
                             "detail": "OK" if r.ok else f"Status {r.status_code}"})
        except Exception as exc:
            results.append({"label": "TMDB API", "ok": False, "detail": str(exc)})
    else:
        results.append({"label": "TMDB API", "ok": False, "detail": "API key not configured"})

    # qBittorrent
    qcfg = config.get("qbittorrent", {})
    qurl = qcfg.get("url", "http://localhost:8080").rstrip("/").replace("//localhost:", "//127.0.0.1:")
    try:
        s = req.Session()
        s.headers.update({"Referer": qurl + "/", "Origin": qurl})
        if not qcfg.get("bypass_auth") and qcfg.get("username"):
            s.post(f"{qurl}/api/v2/auth/login",
                   data={"username": qcfg["username"], "password": qcfg.get("password","")},
                   timeout=5)
        r = s.get(f"{qurl}/api/v2/app/version", timeout=5)
        results.append({"label": "qBittorrent", "ok": r.ok,
                         "detail": f"v{r.text.strip()}" if r.ok else f"Status {r.status_code}"})
    except Exception as exc:
        results.append({"label": "qBittorrent", "ok": False, "detail": str(exc)})

    # Library paths
    from pathlib import Path
    for lib in config.get("libraries", []):
        if not lib.get("enabled", True):
            continue
        p = Path(lib.get("path", ""))
        results.append({"label": f"Path: {lib.get('name','?')}",
                         "ok": p.exists(),
                         "detail": str(p) if p.exists() else f"Not found: {p}"})

    return jsonify(results)


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", app_version=APP_VERSION)


# ---------------------------------------------------------------------------
def _make_qbit():
    """Create a QBitClient from the current config."""
    from qbit_client import QBitClient
    config = load_config()
    qbit_cfg = config.get("qbittorrent", {})
    return QBitClient(
        url=qbit_cfg.get("host", "http://127.0.0.1:8083"),
        username=qbit_cfg.get("username", ""),
        password=qbit_cfg.get("password", ""),
        bypass_auth=qbit_cfg.get("bypass_auth", False),
    )


@app.post("/api/qbit/reannounce")
def api_qbit_reannounce():
    """Force-reannounce all stalled torrents in qBittorrent."""
    try:
        qbit = _make_qbit()
        count = qbit.reannounce_stalled()
        return jsonify({"reannounced": count})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/qbit/recover-stalled")
def api_recover_stalled():
    """Remove stuck/stalled torrents and clear their state so they retry on next run."""
    try:
        import re as _re
        qbit = _make_qbit()
        # Use the same detection as the background watcher (catches forcedDL too)
        stalled_zero = qbit.get_zero_seed_stalled(min_stall_minutes=5)
        removed = 0
        cleared_keys = []
        state = load_state()
        queued = state.get("queued", [])

        for t in stalled_zero:
            name = t.get("name", "")
            if qbit.delete_torrent(t["hash"], delete_files=False):
                removed += 1
                _processed_hashes.add(t["hash"])
                se_match = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", name)
                if se_match:
                    season  = int(se_match.group(1))
                    episode = int(se_match.group(2))
                    to_remove = [
                        k for k in queued
                        if f"S{season:02d}E{episode:02d}".lower() in k.lower()
                    ]
                    for k in to_remove:
                        queued.remove(k)
                        cleared_keys.append(k)

        state["queued"] = queued
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        return jsonify({
            "removed": removed,
            "cleared_keys": len(cleared_keys),
            "message": f"Removed {removed} stuck torrent(s). They will be re-searched on the next run."
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/qbit/stalled")
def api_qbit_stalled():
    """Return list of stalled torrents with their tracker messages for diagnosis."""
    qbit = _make_qbit()
    stalled = qbit.get_torrents(filter_state="stalledDL")
    result = []
    for t in stalled[:20]:  # limit to 20 for UI
        trackers = qbit.get_tracker_messages(t["hash"])
        tracker_msgs = [
            {"url": tr.get("url", ""), "msg": tr.get("msg", ""), "status": tr.get("status", 0)}
            for tr in trackers
            if tr.get("url", "").startswith("http")  # skip DHT/PEX rows
        ]
        result.append({
            "name": t.get("name", ""),
            "hash": t.get("hash", ""),
            "state": t.get("state", ""),
            "progress": t.get("progress", 0),
            "num_seeds": t.get("num_seeds", 0),
            "num_leechs": t.get("num_leechs", 0),
            "tracker_msgs": tracker_msgs,
        })
    return jsonify({"count": len(stalled), "torrents": result})


@app.get("/api/diskspace")
def api_diskspace():
    """Return free/used/total disk space for each configured library path."""
    import shutil
    cfg = load_config()
    results = []
    seen_paths: set[str] = set()
    for lib in cfg.get("libraries", []):
        if not lib.get("enabled", True):
            continue
        path = lib.get("path", "")
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        try:
            usage = shutil.disk_usage(path)
            results.append({
                "library": lib.get("name", path),
                "path": path,
                "total_gb":  round(usage.total / 1e9, 1),
                "used_gb":   round(usage.used  / 1e9, 1),
                "free_gb":   round(usage.free  / 1e9, 1),
                "used_pct":  round(usage.used  / usage.total * 100, 1),
            })
        except OSError as e:
            results.append({"library": lib.get("name", path), "path": path, "error": str(e)})
    return jsonify(results)


@app.get("/api/retry-queue")
def api_retry_queue():
    """Return items that failed to find a torrent and are pending retry."""
    state = load_state()
    rq = state.get("retry_queue", {})
    items = [
        {"key": k, **v}
        for k, v in sorted(rq.items(), key=lambda x: x[1].get("last_tried", ""), reverse=True)
    ]
    return jsonify({"count": len(items), "items": items})


@app.delete("/api/retry-queue/<path:key>")
def api_retry_queue_delete(key: str):
    """Remove an item from the retry queue (give up on it)."""
    state = load_state()
    state.setdefault("retry_queue", {}).pop(key, None)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    return jsonify({"ok": True})


@app.delete("/api/retry-queue-show/<path:show_name>")
def api_retry_queue_delete_show(show_name: str):
    """Remove all retry-queue entries for a given show name."""
    state = load_state()
    rq = state.setdefault("retry_queue", {})
    prefix = f"tv::{show_name}::"
    keys_to_delete = [k for k in list(rq.keys()) if k.startswith(prefix)]
    for k in keys_to_delete:
        rq.pop(k, None)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    return jsonify({"ok": True, "removed": len(keys_to_delete)})


# Entry point
@app.post("/api/rename")
def api_rename():
    """Rename scene-style filenames to clean format and delete junk files."""
    try:
        from renamer import rename_video_files, cleanup_library
        dry_run = request.args.get("dry", "false").lower() == "true"
        cfg = load_config()
        lib_paths = [
            lib.get("path", "")
            for lib in cfg.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
        ]

        # Release file locks: remove completed torrents from qBittorrent first
        released = 0
        if not dry_run:
            try:
                qbit = _make_qbit()
                completed = [t for t in qbit.get_torrents() if t.get("progress", 0) >= 1.0]
                for t in completed:
                    qbit.delete_torrent(t["hash"], delete_files=False)
                released = len(completed)
                if released:
                    time.sleep(8)  # give Windows time to release file handles
            except Exception:
                pass

        result = rename_video_files(lib_paths, dry_run=dry_run)
        clean  = cleanup_library(lib_paths, dry_run=dry_run)
        return jsonify({
            **result,
            "deleted_files":   clean["deleted_files"],
            "deleted_folders": clean["deleted_folders"],
            "released_from_qbit": released,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/validate")
def api_validate():
    """
    Incremental validation: check ONLY the files our downloader installed.
    Used by the downloader pipeline after each scan.  Auto-deletes corrupt
    tracked files and queues them for re-search.
    """
    try:
        from validator import validate_downloaded_only
        cfg = load_config()
        lib_paths = [
            lib.get("path", "")
            for lib in cfg.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
        ]
        state = load_state()
        result = validate_downloaded_only(
            lib_paths, state=state, delete_corrupt=True,
        )
        if result["corrupt"]:
            save_state(state)
        return jsonify({
            "checked":  result["checked"],
            "corrupt":  len(result["corrupt"]),
            "deleted":  sum(1 for c in result["corrupt"] if c["deleted"]),
            "cleared":  result["cleared"],
            "details":  [
                {"file": Path(c["path"]).name, "reason": c["reason"], "deleted": c["deleted"]}
                for c in result["corrupt"]
            ],
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Find-broken — background job with progress reporting
# ---------------------------------------------------------------------------

# In-memory state for the currently-running scan. Lives only inside the web
# process — survives until the next /api/find-broken POST replaces it.
_find_broken_lock   = threading.Lock()
_find_broken_state: dict = {
    "running":         False,
    "started_at":      None,
    "finished_at":     None,
    "phase":           "idle",          # idle | enumerate | scan | done | error
    "deep":            False,            # whether the deep decode check is on
    "total":           0,                # files to validate
    "checked":         0,                # files validated so far
    "broken":          0,                # broken count so far
    "current_lib":     "",
    "current_lib_idx": 0,
    "lib_total":       0,
    "current_show":    "",
    "current_file":    "",
    "error":           "",
    "result":          None,             # final summary dict when phase == "done"
    "ffprobe_ok":      False,
    "ffprobe_path":    "",
}


def _run_find_broken(lib_paths: list[str], deep: bool = False) -> None:
    """Background worker — populates _find_broken_state as it goes."""
    from validator import find_broken_in_library, _find_ffprobe
    try:
        ffprobe_path = _find_ffprobe()
        with _find_broken_lock:
            _find_broken_state["ffprobe_path"] = ffprobe_path or ""
            _find_broken_state["ffprobe_ok"]   = bool(ffprobe_path)

        def _on_progress(p: dict) -> None:
            with _find_broken_lock:
                _find_broken_state.update({
                    "phase":           p.get("phase", _find_broken_state["phase"]),
                    "total":           p.get("total", _find_broken_state["total"]),
                    "checked":         p.get("checked", _find_broken_state["checked"]),
                    "broken":          p.get("broken", _find_broken_state["broken"]),
                    "current_lib":     p.get("current_lib", _find_broken_state["current_lib"]),
                    "current_lib_idx": p.get("current_lib_idx", _find_broken_state["current_lib_idx"]),
                    "lib_total":       p.get("lib_total", _find_broken_state["lib_total"]),
                    "current_show":    p.get("current_show", _find_broken_state["current_show"]),
                    "current_file":    p.get("current_file", _find_broken_state["current_file"]),
                })

        state = load_state()
        result = find_broken_in_library(
            lib_paths, state=state, progress_cb=_on_progress, deep=deep
        )

        with _find_broken_lock:
            _find_broken_state.update({
                "running":     False,
                "finished_at": datetime.utcnow().isoformat(timespec="seconds"),
                "phase":       "done",
                "result":      {
                    "checked": result["checked"],
                    "broken":  result["broken"],
                    "shows":   result["shows"],
                },
            })
    except Exception as exc:  # pylint: disable=broad-except
        with _find_broken_lock:
            _find_broken_state.update({
                "running":     False,
                "finished_at": datetime.utcnow().isoformat(timespec="seconds"),
                "phase":       "error",
                "error":       f"{type(exc).__name__}: {exc}",
            })


@app.post("/api/find-broken")
def api_find_broken():
    """
    Kick off a background full-library validation scan. Returns immediately
    with {started: true}. Poll /api/find-broken/status for progress and the
    final report grouped by show/season.
    """
    body = request.get_json(silent=True) or {}
    deep = bool(body.get("deep", False))
    with _find_broken_lock:
        if _find_broken_state["running"]:
            return jsonify({
                "started": False,
                "already_running": True,
                "phase":           _find_broken_state["phase"],
            })
        cfg = load_config()
        lib_paths = [
            lib.get("path", "")
            for lib in cfg.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
        ]
        # Reset state and mark as starting
        _find_broken_state.update({
            "running":         True,
            "started_at":      datetime.utcnow().isoformat(timespec="seconds"),
            "finished_at":     None,
            "phase":           "enumerate",
            "deep":            deep,
            "total":           0,
            "checked":         0,
            "broken":          0,
            "current_lib":     "",
            "current_lib_idx": 0,
            "lib_total":       len(lib_paths),
            "current_show":    "",
            "current_file":    "",
            "error":           "",
            "result":          None,
        })

    t = threading.Thread(target=_run_find_broken, args=(lib_paths, deep), daemon=True)
    t.start()
    return jsonify({"started": True, "lib_total": len(lib_paths), "deep": deep})


@app.get("/api/find-broken/status")
def api_find_broken_status():
    """Return progress + final report (if done) for the background find-broken scan."""
    with _find_broken_lock:
        snap = dict(_find_broken_state)
    return jsonify(snap)


@app.post("/api/validate-paths")
def api_validate_paths():
    """
    Targeted deep-validate + auto-fix for specific show/season UNC path(s).
    Body: { "paths": ["\\\\NAS\\...\\Show\\Season 1", ...] }

    Spawns the detached downloader worker, which deep-decodes every video file
    under those paths, auto-removes the genuinely corrupt ones, re-downloads a
    DIFFERENT torrent for each, and verifies the replacement (validate-as-you-go).
    Progress shows in the normal scan-status card; nothing blocks the UI.
    """
    body  = request.get_json(silent=True) or {}
    paths = body.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    paths = [p.strip() for p in paths if p and p.strip()]
    if not paths:
        return jsonify({"error": "No paths provided"}), 400

    try:
        import os
        cmd = child_argv("--validate-paths", *paths)
        kwargs: dict = {"cwd": str(BASE_DIR)}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        return jsonify({"started": True, "paths": paths})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


_find_dupes_lock = threading.Lock()
_find_dupes_state: dict = {"running": False, "result": None, "error": "", "finished_at": None}


def _run_find_duplicates(lib_paths: list) -> None:
    """Background worker: scan libraries for duplicate episode files."""
    try:
        from duplicates import find_duplicate_episodes
        dupes = find_duplicate_episodes(lib_paths)
        with _find_dupes_lock:
            _find_dupes_state.update({
                "running": False,
                "result": dupes,
                "error": "",
                "finished_at": datetime.utcnow().isoformat(timespec="seconds"),
            })
    except Exception as exc:  # pylint: disable=broad-except
        with _find_dupes_lock:
            _find_dupes_state.update({
                "running": False,
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": datetime.utcnow().isoformat(timespec="seconds"),
            })


_split_scan_lock = threading.Lock()
_split_scan_state: dict = {"running": False, "result": None, "error": "", "finished_at": None}


def _season_sort_key(folder_name: str):
    """Sort season folders numerically when possible ('Season 2' before
    'Season 20'), pushing specials/non-numeric to the end alphabetically."""
    m = re.search(r"(\d+)", folder_name or "")
    return (0, int(m.group(1))) if m else (1, (folder_name or "").lower())


def _norm_show_key(name: str) -> str:
    """Collapse a show folder name to a comparison key so the SAME show in
    different libraries groups together: drop a trailing '(YYYY)', lowercase,
    and reduce every run of non-alphanumerics to a single space. Mirrors the
    downloader's matching without importing that heavy module."""
    name = re.sub(r"\s*\(\d{4}\)\s*$", "", name or "")
    name = re.sub(r"[^0-9a-zA-Z]+", " ", name).lower().strip()
    return name


def _run_split_series_scan(libs: list[dict]) -> None:
    """Background worker: find TV/animation shows whose folders are split across
    more than one library path (e.g. the same show living under both Jellyfin1
    and Jellyfin6). Reports each split show with all of its locations so the
    user can manually consolidate them."""
    try:
        from scanner import clean_show_name

        groups: dict[str, dict] = {}
        for lib in libs:
            lib_name = lib.get("name", "")
            lib_path = lib.get("path", "")
            root = Path(lib_path)
            if not lib_path or not root.exists():
                continue
            try:
                show_dirs = [d for d in root.iterdir() if d.is_dir()]
            except OSError:
                continue

            for show_dir in show_dirs:
                display = clean_show_name(show_dir.name)
                key = _norm_show_key(display)
                if not key:
                    continue

                # Only enumerate the show's immediate subfolders (one directory
                # listing) — NOT every file inside each season. Descending into
                # season folders to count files is brutally slow over UNC/SMB,
                # and the season list alone is enough to pick the bigger copy.
                seasons: list[str] = []
                try:
                    seasons = [sub.name for sub in show_dir.iterdir() if sub.is_dir()]
                except OSError:
                    pass

                location = {
                    "library": lib_name,
                    "library_path": lib_path,
                    "folder": show_dir.name,
                    "path": str(show_dir),
                    "seasons": sorted(seasons, key=_season_sort_key),
                    "season_count": len(seasons),
                }
                grp = groups.setdefault(key, {"display": display, "locations": []})
                grp["locations"].append(location)

        split: list[dict] = []
        for grp in groups.values():
            locations = grp["locations"]
            # A show is "split" when its folders span 2+ distinct physical paths.
            if len({loc["path"] for loc in locations}) < 2:
                continue
            # Recommend consolidating INTO the copy that already has the most
            # season folders — least to move.
            ordered = sorted(
                locations,
                key=lambda loc: loc["season_count"],
                reverse=True,
            )
            split.append({
                "show": grp["display"],
                "locations": ordered,
                "location_count": len(ordered),
                "recommended_path": ordered[0]["path"],
            })

        split.sort(key=lambda s: s["show"].lower())
        with _split_scan_lock:
            _split_scan_state.update({
                "running": False,
                "result": split,
                "error": "",
                "finished_at": datetime.utcnow().isoformat(timespec="seconds"),
            })
    except Exception as exc:  # pylint: disable=broad-except
        with _split_scan_lock:
            _split_scan_state.update({
                "running": False,
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": datetime.utcnow().isoformat(timespec="seconds"),
            })


@app.post("/api/split-series/scan")
def api_split_series_scan():
    """Kick off a background scan of the configured TV/animation libraries to
    find shows whose episodes are split across multiple library paths. Poll
    /api/split-series/status for the result."""
    with _split_scan_lock:
        if _split_scan_state["running"]:
            return jsonify({"started": False, "already_running": True})
        cfg = load_config()
        libs = [
            {"name": lib.get("name", ""), "path": lib.get("path", "")}
            for lib in cfg.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
            and lib.get("type", "").lower() in ("tv", "animation")
        ]
        _split_scan_state.update({
            "running": True, "result": None, "error": "", "finished_at": None,
        })
    t = threading.Thread(target=_run_split_series_scan, args=(libs,), daemon=True)
    t.start()
    return jsonify({"started": True, "lib_total": len(libs)})


@app.get("/api/split-series/status")
def api_split_series_status():
    """Return progress + result for the background split-series scan."""
    with _split_scan_lock:
        return jsonify(dict(_split_scan_state))


@app.post("/api/find-duplicates")
def api_find_duplicates():
    """Kick off a FAST (filename-only) duplicate-episode scan across the TV /
    animation libraries. Finds single-episode files already contained in a
    combined multi-episode file (and exact duplicate copies). Poll
    /api/find-duplicates/status for the result."""
    with _find_dupes_lock:
        if _find_dupes_state["running"]:
            return jsonify({"started": False, "already_running": True})
        cfg = load_config()
        lib_paths = [
            lib.get("path", "")
            for lib in cfg.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
            and lib.get("type", "").lower() in ("tv", "animation")
        ]
        _find_dupes_state.update({
            "running": True, "result": None, "error": "", "finished_at": None,
        })
    t = threading.Thread(target=_run_find_duplicates, args=(lib_paths,), daemon=True)
    t.start()
    return jsonify({"started": True, "lib_total": len(lib_paths)})


@app.get("/api/find-duplicates/status")
def api_find_duplicates_status():
    """Return progress + result for the background duplicate scan."""
    with _find_dupes_lock:
        return jsonify(dict(_find_dupes_state))


@app.post("/api/remove-duplicates")
def api_remove_duplicates():
    """Remove a user-confirmed list of duplicate files.
    Body: { "paths": ["\\\\NAS\\...\\file.mkv", ...] }"""
    try:
        from duplicates import remove_duplicate_files
        body = request.get_json(silent=True) or {}
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            return jsonify({"error": "No paths provided"}), 400
        result = remove_duplicate_files(paths)
        return jsonify({
            "ok": True,
            "removed": len(result["removed"]),
            "errors": result["errors"],
        })
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"error": str(exc)}), 500


@app.post("/api/remove-broken")
def api_remove_broken():
    """
    Remove a user-confirmed list of broken video files.
    Body: { "paths": ["\\\\NAS\\...\\file.mkv", ...] }
    Smart cleanup: if every file in a season folder is being removed, the
    whole folder goes; otherwise just the individual files.
    """
    try:
        from validator import remove_broken_files
        body = request.get_json(silent=True) or {}
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            return jsonify({"error": "No paths provided"}), 400

        cfg = load_config()
        lib_paths = [
            lib.get("path", "")
            for lib in cfg.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
        ]
        state = load_state()
        result = remove_broken_files(paths, state=state, library_paths=lib_paths)
        # Clear any pending post-download review now that the user acted on it.
        state.pop("pending_download_review", None)

        # If a per-series campaign is driving, DON'T spawn a separate run (it
        # would fight the campaign). Instead signal the campaign to STAY on the
        # current series and re-download the just-removed (and now blacklisted)
        # episode(s) with a different release, then re-validate.
        campaign = bool(state.get("campaign_active"))
        if campaign:
            state["review_decision"] = {
                "action": "redownload",
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        save_state(state)

        # Auto re-download: the removed episodes had their cooldown cleared and
        # their bad release blacklisted, so a fresh run will pull a DIFFERENT
        # torrent for each one. (Skipped while a campaign is active.)
        triggered = False
        if not campaign and result.get("requeued", 0) > 0:
            try:
                python = sys.executable
                cmd = child_argv("--run-now")
                subprocess.Popen(cmd, cwd=str(BASE_DIR))
                triggered = True
            except Exception as exc:
                app.logger.warning("Auto re-download trigger failed: %s", exc)
        result["redownload_triggered"] = triggered
        result["campaign"] = campaign
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/download-review")
def api_download_review():
    """
    Return the post-download validation review written by the downloader
    after the last "Download Missing" run. Contains ONLY the episodes our
    system just installed that failed validation, grouped by show/season —
    same structure as /api/find-broken so the frontend reuses the modal.

    Returns {"broken": 0} when there's nothing to review.
    """
    state = load_state()
    review = state.get("pending_download_review")
    if not review or not review.get("broken"):
        return jsonify({"broken": 0, "checked": review.get("checked", 0) if review else 0,
                        "shows": [], "created_at": review.get("created_at") if review else None})
    return jsonify({
        "broken":     review.get("broken", 0),
        "checked":    review.get("checked", 0),
        "shows":      review.get("shows", []),
        "created_at": review.get("created_at"),
    })


@app.post("/api/download-review/clear")
def api_download_review_clear():
    """Dismiss the pending post-download review (after user confirms/cancels).

    When a per-series campaign is active, dismissing the popup means "this series
    is good — move on", so we record a 'confirm' decision the campaign is waiting
    for. (Approving removals instead records 'redownload' in api_remove_broken.)
    """
    state = load_state()
    state.pop("pending_download_review", None)
    if state.get("campaign_active"):
        state["review_decision"] = {
            "action": "confirm",
            "at": datetime.now().isoformat(timespec="seconds"),
        }
    save_state(state)
    return jsonify({"ok": True})


def _review_action_page(title: str, message: str) -> str:
    """Minimal mobile-friendly confirmation page shown after a Discord link
    button is tapped (the buttons open these GET endpoints in a browser)."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#1e1f22;"
        "color:#e3e5e8;display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;margin:0}.c{max-width:440px;text-align:center;padding:36px 28px;"
        "background:#2b2d31;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.4)}"
        "h2{margin:0 0 12px}p{color:#b5bac1;line-height:1.5}a{color:#5865f2;"
        "text-decoration:none;font-weight:600}</style></head><body><div class='c'>"
        f"<h2>{title}</h2><p>{message}</p><p><a href='/'>Open dashboard →</a></p>"
        "</div></body></html>"
    )


@app.get("/review/keep")
def review_keep():
    """Discord link-button target: keep the flagged file(s) as-is, stop retrying."""
    state = load_state()
    review = state.get("pending_download_review") or {}
    n = review.get("broken", 0)
    state.pop("pending_download_review", None)
    if state.get("campaign_active"):
        state["review_decision"] = {
            "action": "confirm",
            "at": datetime.now().isoformat(timespec="seconds"),
        }
    save_state(state)
    return _review_action_page(
        "✅ Files kept",
        f"Kept {n} flagged file(s) as-is. No re-download was triggered.",
    )


@app.get("/review/redownload")
def review_redownload():
    """Discord link-button target: remove the flagged corrupt file(s), blacklist
    the bad release, and re-download a different one (mirrors the dashboard's
    'Re-download' action)."""
    state = load_state()
    review = state.get("pending_download_review") or {}
    paths = [
        bf.get("path")
        for show in review.get("shows", [])
        for season in show.get("seasons", [])
        for bf in season.get("broken_files", [])
        if bf.get("path")
    ]
    cfg = load_config()
    lib_paths = [
        lib.get("path", "")
        for lib in cfg.get("libraries", [])
        if lib.get("enabled", True) and lib.get("path")
    ]
    removed = requeued = 0
    try:
        if paths:
            from validator import remove_broken_files
            result = remove_broken_files(paths, state=state, library_paths=lib_paths)
            removed = result.get("removed", 0)
            requeued = result.get("requeued", 0)
    except Exception as exc:
        app.logger.warning("[Review] redownload removal failed: %s", exc)

    state.pop("pending_download_review", None)
    campaign = bool(state.get("campaign_active"))
    if campaign:
        state["review_decision"] = {
            "action": "redownload",
            "at": datetime.now().isoformat(timespec="seconds"),
        }
    save_state(state)

    if not campaign and requeued > 0:
        try:
            subprocess.Popen(
                child_argv("--run-now"),
                cwd=str(BASE_DIR),
            )
        except Exception as exc:
            app.logger.warning("[Review] auto re-download trigger failed: %s", exc)

    return _review_action_page(
        "🔁 Re-downloading",
        f"Removed {removed} corrupt file(s); re-downloading {requeued} episode(s) "
        "with a different release. Check the dashboard for progress.",
    )


@app.post("/api/extract")
def api_extract():
    """Scan all library paths for RAR archives and extract them."""
    try:
        from extractor import scan_and_extract, find_extractor
        tool = find_extractor()
        if not tool:
            return jsonify({
                "ok": False,
                "reason": "7-Zip not installed. Download from https://7-zip.org"
            }), 503
        cfg = load_config()
        lib_paths = [
            lib.get("path", "")
            for lib in cfg.get("libraries", [])
            if lib.get("enabled", True) and lib.get("path")
        ]
        result = scan_and_extract(lib_paths, delete_rar=True)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "reason": str(exc)}), 500


# ---------------------------------------------------------------------------
# Background completion watcher — rename+cleanup the moment a torrent hits 100%
# ---------------------------------------------------------------------------

_completion_watcher_started = False
_completion_watcher_lock = threading.Lock()
# hashes we've already post-processed this session
_processed_hashes: set[str] = set()
# hash -> number of times we've tried to resume an errored torrent (give up after N)
_error_resume_attempts: dict[str, int] = {}
_MAX_ERROR_RESUME_ATTEMPTS = 3
# Escape hatch for torrents that get stuck in 'error' for hours: a recheck
# briefly knocks them out of the error state (so the attempt counter never
# accumulates to the limit) and then they immediately re-error — ping-ponging
# forever. If a torrent has existed this long and is still erroring at low
# progress, it never recovers, so force-remove + re-search it.
_ERROR_HARD_AGE_HOURS = 3
# The full-library queued-key prune is expensive (whole-NAS scan); throttle it so
# it never blocks the fast error/stall recovery that must run every cycle.
_last_prune_ts: float = 0.0
_PRUNE_INTERVAL_SEC = 1800  # 30 minutes
# After culling dead torrents we kick off a re-search run so the affected episodes
# get replaced with a seeded release — debounced so we don't spawn runs constantly.
_last_requeue_run_ts: float = 0.0
_REQUEUE_RUN_INTERVAL_SEC = 900  # 15 minutes

# qBittorrent connectivity tracking (VPN-down detection). When qBit can't reach
# the BitTorrent network, every torrent shows 0 seeds — we must NOT cull/blacklist
# them (they aren't dead, they're stranded), and we alert the user once.
_qbit_offline: bool = False
_qbit_offline_strikes: int = 0
_QBIT_OFFLINE_STRIKES_TO_ALERT = 2  # consecutive 90s cycles (~3 min) before alerting


def _record_qbit_connectivity(health: dict) -> bool:
    """Update the persisted qBit connectivity state for the dashboard banner and
    fire a one-time Discord/ntfy alert on transitions. Returns True if qBit is
    considered DOWN (so callers skip culling/queuing).

    A reading is only treated as DOWN after a couple of consecutive failures, so
    a momentary blip (or qBit restarting) doesn't trigger false alarms.
    """
    global _qbit_offline, _qbit_offline_strikes
    if not health.get("known", False):
        # qBit itself unreachable — don't flip the BitTorrent-offline flag on
        # that alone (qBit may just be restarting); leave prior state as-is.
        return _qbit_offline

    if health.get("up", True):
        was_offline = _qbit_offline
        _qbit_offline = False
        _qbit_offline_strikes = 0
        if was_offline:
            logger.info("[Connectivity] qBittorrent is back online (status=%s, dht=%s)",
                        health.get("status"), health.get("dht_nodes"))
            _persist_qbit_connectivity(True, health)
            _send_connectivity_alert(online=True, health=health)
        return False

    # Reading says offline.
    _qbit_offline_strikes += 1
    if _qbit_offline_strikes < _QBIT_OFFLINE_STRIKES_TO_ALERT:
        return _qbit_offline  # not confirmed yet
    if not _qbit_offline:
        _qbit_offline = True
        logger.warning(
            "[Connectivity] qBittorrent appears OFFLINE — status=%s, dht_nodes=%s. "
            "This usually means the VPN/tunnel is down. Pausing culls + queuing so we "
            "don't blacklist healthy releases as 'dead'.",
            health.get("status"), health.get("dht_nodes"),
        )
        _persist_qbit_connectivity(False, health)
        _send_connectivity_alert(online=False, health=health)
    return True


def _persist_qbit_connectivity(online: bool, health: dict) -> None:
    """Write connectivity status into state.json so the dashboard can show a banner."""
    try:
        st = load_state()
        st["qbit_connectivity"] = {
            "online": online,
            "status": health.get("status", ""),
            "dht_nodes": health.get("dht_nodes", 0),
            "checked_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("[Connectivity] persist failed: %s", exc)


def _send_connectivity_alert(online: bool, health: dict) -> None:
    try:
        notif = load_config().get("notifications", {})
        from notifier import Notifier
        n = Notifier(discord_webhook=notif.get("discord_webhook", ""),
                     ntfy_topic=notif.get("ntfy_topic", ""))
        if online:
            n.notify("✅ qBittorrent back online",
                     f"Connectivity restored (status={health.get('status')}, "
                     f"DHT nodes={health.get('dht_nodes')}). Downloads resuming.")
        else:
            n.notify("⚠️ qBittorrent OFFLINE — downloads stalled",
                     "qBittorrent can't reach the BitTorrent network "
                     f"(status={health.get('status')}, DHT nodes={health.get('dht_nodes')}). "
                     "This is usually an expired/dropped VPN. Culling + queuing are paused so "
                     "healthy releases aren't blacklisted as dead. Reconnect the VPN to resume.")
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("[Connectivity] alert failed: %s", exc)


def _match_queued_keys(queued: list, torrent_name: str) -> list:
    """
    Return the queued state-keys that correspond to a torrent, matched by BOTH
    the SxxExx tag AND the show name parsed from the release name.

    Matching only on "S03E01" is wrong — many shows share that tag, so a single
    dead torrent would wrongly unqueue every show's S03E01. We also require the
    show portion of the key to match the show parsed from the release name.
    """
    import re as _r
    m = _r.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", torrent_name)
    if not m:
        return []
    tag = f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}"
    nshow = _r.sub(r"[^a-z0-9]", "", torrent_name[:m.start()].lower())
    out = []
    for k in queued:
        parts = k.split("::")
        if len(parts) < 3 or tag not in parts[2].lower():
            continue
        kshow = _r.sub(r"[^a-z0-9]", "", parts[1].lower())
        # Require a real show-name overlap (avoid cross-show false matches).
        if nshow and kshow and (nshow == kshow or nshow in kshow or kshow in nshow):
            out.append(k)
    return out


def _completion_watcher() -> None:
    """
    Poll qBittorrent every 90 seconds.
    - Newly completed (100%) torrents → rename + cleanup files
    - Zero-seed stalled torrents (>10 min) → remove from qBit + clear state so
      the next scheduled run re-searches with a different/fallback release
    """
    logger.info("[CompletionWatcher] Started — watching for completed and stalled torrents")
    while True:
        try:
            time.sleep(90)
            cfg = load_config()
            qbit = _make_qbit()
            torrents = qbit.get_torrents()
            # Set when a dead torrent is culled this cycle → trigger a re-search run
            # so the affected episode is replaced with a seeded release.
            _dead_removed = False

            # Keep qBittorrent's configured queue honoured, and make sure no
            # incomplete torrent is left stuck paused — so downloads keep flowing
            # up to whatever limit is set in qBittorrent.
            try:
                qbit.ensure_queue_settings()
                qbit.resume_incomplete_paused()
            except Exception:
                pass

            # ── 0. Connectivity gate (VPN-down detection) ──────────────────
            # If qBittorrent can't reach the BitTorrent network, EVERY torrent
            # reads as 0 seeds. Culling+blacklisting then would wrongly mark
            # perfectly healthy releases as "dead". Detect that state and skip
            # the seed-based cull until connectivity returns.
            try:
                _qbit_down = _record_qbit_connectivity(qbit.network_health())
            except Exception:
                _qbit_down = False

            # ── 1. Errored / missing-file torrents (FAST — runs every cycle) ──
            #  (a) files intact, transient I/O error  → resume to recover progress
            #  (b) files missing, or still erroring after N resumes → remove + re-queue
            errored = [
                t for t in torrents
                if t.get("state", "") in ("error", "missingFiles")
                and t["hash"] not in _processed_hashes
            ]
            if errored:
                import re as _ree
                import os
                try:
                    with open(STATE_FILE, encoding="utf-8") as f:
                        est = json.load(f)
                except Exception:
                    est = {"queued": []}
                equeued: list = est.get("queued", [])
                edt: dict = est.get("downloaded_torrents", {})
                eblk: dict = est.setdefault("torrent_blacklist", {})
                resumed = removed = 0

                for t in errored:
                    h = t["hash"]
                    name = t.get("name", "")
                    cp = t.get("content_path") or ""
                    files_present = bool(cp) and os.path.exists(cp)
                    attempts = _error_resume_attempts.get(h, 0)

                    # Escape hatch: a torrent stuck erroring for hours at low
                    # progress ping-pongs out of 'error' on each recheck, so the
                    # attempt counter never reaches the limit and it lingers
                    # forever. Force-remove + re-search it instead of rechecking.
                    progress = t.get("progress", 0) or 0
                    added_on = t.get("added_on") or 0
                    stuck_hours = (time.time() - added_on) / 3600 if added_on else 0
                    stuck_too_long = stuck_hours >= _ERROR_HARD_AGE_HOURS and progress < 0.5

                    # Recoverable: data still on disk and we haven't given up yet.
                    # Force a re-check first so qBit re-validates pieces and
                    # re-downloads any corrupt/truncated ones (fixes "End of file"
                    # and partial-write errors that a plain resume can't).
                    if t.get("state") != "missingFiles" and files_present \
                            and attempts < _MAX_ERROR_RESUME_ATTEMPTS \
                            and not stuck_too_long:
                        _error_resume_attempts[h] = attempts + 1
                        try:
                            qbit.recheck_torrent(h)
                        except Exception:
                            pass
                        if qbit.resume_torrent(h):
                            resumed += 1
                            logger.info(
                                "[CompletionWatcher] Rechecked + resumed errored torrent (attempt %d/%d): %s",
                                attempts + 1, _MAX_ERROR_RESUME_ATTEMPTS, name,
                            )
                        continue

                    # Unrecoverable: missing files, exhausted resume attempts,
                    # or stuck erroring for hours (ping-pong loop).
                    qbit.delete_torrent(h, delete_files=True)
                    _processed_hashes.add(h)
                    _error_resume_attempts.pop(h, None)
                    removed += 1
                    if stuck_too_long:
                        logger.info(
                            "[CompletionWatcher] Force-removed torrent stuck in error "
                            "%.1fh at %.0f%% (never recovered): %s",
                            stuck_hours, progress * 100, name,
                        )
                    else:
                        logger.info("[CompletionWatcher] Removed unrecoverable errored torrent: %s", name)

                    for k in _match_queued_keys(equeued, name):
                        equeued.remove(k)
                        edt.pop(k, None)
                        # Blacklist the bad release so the re-search grabs a
                        # different one instead of re-downloading the corrupt file.
                        ebl = eblk.setdefault(k, [])
                        if name and name not in ebl:
                            ebl.append(name)
                        _dead_removed = True
                        logger.info("[CompletionWatcher] Unqueued %s for re-download (blacklisted bad release)", k)

                if removed:
                    est["queued"] = equeued
                    est["downloaded_torrents"] = edt
                    with open(STATE_FILE, "w", encoding="utf-8") as f:
                        json.dump(est, f, indent=2)
                if resumed or removed:
                    logger.info(
                        "[CompletionWatcher] Error recovery: resumed %d, removed %d",
                        resumed, removed,
                    )

            # ── 2. Zero-seed stalled torrents (FAST) ──────────────────────
            # Skip entirely when qBit is offline — the 0-seed readings are an
            # artifact of the dead connection, not dead releases.
            stalled_zero = [] if _qbit_down else [
                t for t in qbit.get_zero_seed_stalled(min_stall_minutes=10)
                if t["hash"] not in _processed_hashes
            ]
            if _qbit_down:
                logger.warning(
                    "[CompletionWatcher] qBittorrent offline — skipping zero-seed cull "
                    "(not blacklisting healthy releases while the connection/VPN is down)."
                )
            if stalled_zero:
                logger.info(
                    "[CompletionWatcher] %d stuck torrent(s) (0 speed/seeds) — removing for retry",
                    len(stalled_zero),
                )
                import re as _re
                try:
                    with open(STATE_FILE, encoding="utf-8") as f:
                        st = json.load(f)
                except Exception:
                    st = {"queued": []}
                queued: list = st.get("queued", [])
                blk: dict = st.setdefault("torrent_blacklist", {})

                for t in stalled_zero:
                    name = t.get("name", "")
                    qbit.delete_torrent(t["hash"], delete_files=False)
                    _processed_hashes.add(t["hash"])
                    logger.info("[CompletionWatcher] Removed stuck: %s", name)

                    for k in _match_queued_keys(queued, name):
                        queued.remove(k)
                        # Blacklist this dead release so the re-search picks a
                        # different, seeded one instead of the same dead torrent.
                        bl = blk.setdefault(k, [])
                        if name not in bl:
                            bl.append(name)
                        logger.info("[CompletionWatcher] Unqueued %s for retry (blacklisted dead release)", k)
                        _dead_removed = True

                st["queued"] = queued
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(st, f, indent=2)

            # ── 2b. Slow/stalled slot-hoggers (rotate, don't delete) ───────
            # Torrents stuck at ~0 B/s for >20 min still occupy an active
            # download slot even when seeders exist, starving the well-seeded
            # queued torrents. Demote them to the bottom of the queue so qBit
            # promotes a queued torrent into the freed slot. We keep their
            # partial data and DON'T blacklist — they get another turn when they
            # cycle back to the top. Skipped while qBit is offline (everything
            # stalls then, so rotating is pointless).
            if not _qbit_down:
                try:
                    slow = qbit.get_slow_stalled(min_stall_minutes=20)
                    # Only demote if there are queued torrents waiting to take
                    # the slot — otherwise demotion just churns positions.
                    queued_waiting = any(
                        t.get("state") in ("queuedDL", "queuedForChecking")
                        for t in torrents
                    )
                    if slow and queued_waiting:
                        hashes = [t["hash"] for t in slow]
                        if qbit.set_bottom_priority(hashes):
                            logger.info(
                                "[CompletionWatcher] Demoted %d stalled torrent(s) "
                                "(~0 B/s >20min) to the bottom of the queue so "
                                "well-seeded queued torrents rotate in: %s",
                                len(slow),
                                ", ".join(t.get("name", "")[:40] for t in slow[:5])
                                + (" …" if len(slow) > 5 else ""),
                            )
                except Exception as _slow_exc:  # pylint: disable=broad-except
                    logger.debug("[CompletionWatcher] slow-stall rotation skipped: %s", _slow_exc)

            # ── 3. Completed torrents (rename + cleanup; throttled prune) ──
            newly_done = [
                t for t in torrents
                if t.get("progress", 0) >= 1.0
                and t["hash"] not in _processed_hashes
            ]
            if newly_done:
                logger.info(
                    "[CompletionWatcher] %d newly completed torrent(s) — running rename+cleanup",
                    len(newly_done),
                )
                for t in newly_done:
                    qbit.delete_torrent(t["hash"], delete_files=False)
                    _processed_hashes.add(t["hash"])

                time.sleep(8)  # give Windows/NAS time to fully release file handles

                # Protect torrents that are STILL downloading — never flatten/move
                # their release folder out from under them (that truncates the file
                # and causes "cannot find the path specified" / "End of file" errors).
                import os as _os2
                incomplete_paths: set[str] = set()
                for t in torrents:
                    if t.get("progress", 0) < 1.0:
                        for key in ("content_path", "save_path"):
                            p = t.get(key) or ""
                            if p:
                                incomplete_paths.add(_os2.path.normcase(_os2.path.normpath(p)))
                if incomplete_paths:
                    logger.info(
                        "[CompletionWatcher] Protecting %d still-downloading path(s) from rename/cleanup",
                        len(incomplete_paths),
                    )

                # Scope the rename to ONLY the show folders that just completed —
                # walking the whole ~30k-file NAS every cycle would block this loop
                # for minutes. save_path is the season folder; its parent is the show.
                done_show_dirs: set[str] = set()
                for t in newly_done:
                    sp = t.get("save_path") or t.get("content_path") or ""
                    if sp:
                        done_show_dirs.add(str(Path(sp).parent))

                from renamer import rename_video_files, cleanup_library
                lib_paths = [
                    lib.get("path", "")
                    for lib in cfg.get("libraries", [])
                    if lib.get("enabled", True) and lib.get("path")
                ]
                r = rename_video_files(
                    lib_paths, skip_paths=incomplete_paths,
                    only_show_dirs=done_show_dirs or None,
                )
                logger.info("[CompletionWatcher] Renamed %d file(s)", r["renamed"])

                # The full-library duplicate cleanup AND queued-key prune are both
                # expensive whole-NAS scans, so run them together only occasionally —
                # never let them block the fast recovery above.
                global _last_prune_ts
                if time.time() - _last_prune_ts >= _PRUNE_INTERVAL_SEC:
                    _last_prune_ts = time.time()
                    try:
                        # Full-library rename to flatten/remove any straggler release
                        # folders left behind in shows that aren't actively completing
                        # this cycle (the per-cycle rename is scoped to just-completed
                        # shows for speed). Still protects in-progress torrents.
                        rfull = rename_video_files(lib_paths, skip_paths=incomplete_paths)
                        if rfull["renamed"]:
                            logger.info(
                                "[CompletionWatcher] Full sweep flattened %d straggler file(s)",
                                rfull["renamed"],
                            )
                        c = cleanup_library(lib_paths)
                        logger.info(
                            "[CompletionWatcher] Cleanup removed %d junk item(s)",
                            c["deleted_files"] + c["deleted_folders"],
                        )
                    except Exception as _ce:
                        logger.debug("[CompletionWatcher] Cleanup skipped: %s", _ce)
                    try:
                        from scanner import scan_tv_library
                        with open(STATE_FILE, encoding="utf-8") as _sf:
                            st = json.load(_sf)
                        queued_list: list = st.get("queued", [])
                        tv_libs = [lib.get("path", "") for lib in cfg.get("libraries", [])
                                   if lib.get("type", "tv") in ("tv", "animation") and lib.get("path")]
                        confirmed_on_disk: set[str] = set()
                        for tv_path in tv_libs:
                            for show, seasons in scan_tv_library(tv_path).items():
                                for s_num, eps in seasons.items():
                                    for ep_num in eps:
                                        confirmed_on_disk.add(
                                            f"tv::{show}::S{s_num:02d}E{ep_num:02d}".lower()
                                        )
                        pruned = [k for k in queued_list
                                  if k.lower() not in confirmed_on_disk or "::pack" in k.lower()]
                        if len(pruned) < len(queued_list):
                            removed_n = len(queued_list) - len(pruned)
                            st["queued"] = pruned
                            import re as _re3
                            text = json.dumps(st, indent=2)
                            text = _re3.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
                            with open(STATE_FILE, "w", encoding="utf-8") as _sf:
                                _sf.write(text)
                            logger.info("[CompletionWatcher] Pruned %d queued-key(s) confirmed on disk", removed_n)
                    except Exception as _e:
                        logger.debug("[CompletionWatcher] State prune skipped: %s", _e)

            # ── 4. Replace culled dead torrents with seeded releases ──────
            # If we removed any 0-seed/dead/errored torrent this cycle, kick off a
            # downloader run so the now-unqueued episodes get re-searched. The finder
            # filters out 0-seed results (min_seeders) and ranks by seed health, and
            # the dead release is blacklisted — so the replacement has real seeds.
            if _dead_removed:
                global _last_requeue_run_ts
                now_ts = time.time()
                if now_ts - _last_requeue_run_ts >= _REQUEUE_RUN_INTERVAL_SEC:
                    if active_run_status():
                        logger.debug("[CompletionWatcher] Re-search needed but a run is already active — skipping")
                    else:
                        import subprocess as _sp
                        try:
                            _sp.Popen(
                                child_argv("--run-now"),
                                cwd=str(BASE_DIR),
                            )
                            _last_requeue_run_ts = now_ts
                            logger.info(
                                "[CompletionWatcher] Spawned re-search run to replace culled dead "
                                "torrent(s) with seeded releases",
                            )
                        except Exception as exc:
                            logger.error("[CompletionWatcher] Failed to spawn re-search run: %s", exc)

        except Exception as exc:
            logger.debug("[CompletionWatcher] Error (will retry): %s", exc)


def start_completion_watcher() -> None:
    global _completion_watcher_started
    with _completion_watcher_lock:
        if _completion_watcher_started:
            return
        t = threading.Thread(target=_completion_watcher, daemon=True, name="CompletionWatcher")
        t.start()
        _completion_watcher_started = True


# ---------------------------------------------------------------------------
# Auto-scheduler: fires downloader subprocess on the configured interval
# ---------------------------------------------------------------------------

_scheduler_started = False
_scheduler_lock    = threading.Lock()

def _auto_scheduler() -> None:
    """Background thread: trigger downloader.py automatically on schedule, plus a
    frequent lightweight RSS poll for newly-aired episodes (Sonarr-style)."""
    import subprocess as _sp
    logger.info("[Scheduler] Auto-scheduler started")
    _last_triggered: float = 0.0
    _last_rss: float = 0.0

    while True:
        try:
            config   = load_config()
            state    = load_state()
            interval = int(config.get("schedule_hours", 6)) * 3600
            last_run_str = state.get("last_run")

            # ── Frequent RSS poll (between full scans) ──────────────────────
            rss_minutes = int(config.get("rss_poll_minutes", 0) or 0)
            if rss_minutes > 0 and (time.time() - _last_rss) >= rss_minutes * 60:
                _last_rss = time.time()
                # Only when nothing else is writing state (scan/watcher idle).
                if (state.get("scan_status") or {}).get("status") != "running":
                    logger.info("[Scheduler] RSS poll — checking for new episodes")
                    try:
                        _sp.Popen(
                            child_argv("--rss-grab"),
                            cwd=str(BASE_DIR),
                        )
                    except Exception as exc:
                        logger.debug("[Scheduler] RSS poll trigger failed: %s", exc)

            if last_run_str:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    lr = _dt.fromisoformat(last_run_str.replace("Z", "+00:00"))
                    seconds_since = (datetime.now(timezone.utc) - lr).total_seconds()
                    due = seconds_since >= interval
                except Exception:
                    due = False
            else:
                due = True  # Never run — fire immediately

            now = time.time()
            if due and (now - _last_triggered) > 300 and not active_run_status():  # debounce 5 min
                logger.info("[Scheduler] Run is overdue — triggering downloader")
                python = sys.executable
                cmd    = child_argv("--run-now")
                try:
                    _sp.Popen(cmd, cwd=str(BASE_DIR))
                    _last_triggered = now
                except Exception as exc:
                    logger.error("[Scheduler] Failed to launch downloader: %s", exc)

        except Exception as exc:
            logger.debug("[Scheduler] Error: %s", exc)

        time.sleep(60)  # check every minute


def start_auto_scheduler() -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        t = threading.Thread(target=_auto_scheduler, daemon=True, name="AutoScheduler")
        t.start()
        _scheduler_started = True


# ---------------------------------------------------------------------------
# In-app updater
#
# Two modes:
#   * FROZEN (the shipped .exe): updates come from GitHub *Releases*. The running
#     service downloads the new build .zip (with live progress), then a detached
#     helper stops the service, swaps the app folder, and restarts it. Writable
#     data (config.json/state.json/caches) live in DATA_DIR (%ProgramData%) and
#     are never touched.
#   * SOURCE (python web.py): updates come from `git pull`, as before.
# ---------------------------------------------------------------------------

GITHUB_OWNER = "loguefx"
GITHUB_REPO  = "Project"
# Service name used by the updater helper (kept in sync with service.py).
SERVICE_NAME_FOR_UPDATER = "ShowTVDownloader"

# Shared progress state for the web UI's progress bar.
_update_lock = threading.Lock()
_update_state: dict = {
    "active": False, "phase": "idle", "pct": 0,
    "downloaded": 0, "total": 0, "message": "", "error": "",
    "from_version": "", "to_version": "", "done": False,
}


def _set_update_state(**kw) -> None:
    with _update_lock:
        _update_state.update(kw)
        dl, tot = _update_state["downloaded"], _update_state["total"]
        if _update_state["phase"] == "downloading" and tot > 0:
            _update_state["pct"] = int(dl * 100 / tot)


def _get_update_state() -> dict:
    with _update_lock:
        return dict(_update_state)


# ---- version helpers ------------------------------------------------------

def _parse_version(s: str) -> tuple:
    s = (s or "").strip().lstrip("vV")
    nums: list = []
    for part in re.split(r"[._\-+]", s):
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    return tuple(nums) if nums else (0,)


def _gh_token() -> str:
    try:
        cfg = load_config()
        tok = (cfg.get("update", {}) or {}).get("github_token")
        if tok:
            return str(tok)
    except Exception:  # pylint: disable=broad-except
        pass
    return os.environ.get("GITHUB_TOKEN", "")


def _gh_headers(accept: str = "application/vnd.github+json") -> dict:
    h = {"Accept": accept, "User-Agent": "ShowTVDownloader-Updater"}
    tok = _gh_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _release_latest() -> dict:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    r = req.get(url, headers=_gh_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def _repo_accessible() -> bool:
    """True if we can read the repo metadata (public, or private + valid token)."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        r = req.get(url, headers=_gh_headers(), timeout=20)
        return r.status_code == 200
    except Exception:  # pylint: disable=broad-except
        return False


def _pick_zip_asset(release: dict) -> Optional[dict]:
    for a in release.get("assets", []) or []:
        if str(a.get("name", "")).lower().endswith(".zip"):
            return a
    return None


# ---- git mode (source checkouts) ------------------------------------------

def _git(*args, timeout: int = 60):
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(BASE_DIR),
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "git is not installed on this machine"
    except subprocess.TimeoutExpired:
        return 124, "", "git command timed out"
    except Exception as exc:  # pylint: disable=broad-except
        return 1, "", str(exc)


def _git_repo_present() -> bool:
    return (BASE_DIR / ".git").exists()


def _current_branch() -> str:
    rc, out, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    return out if rc == 0 and out else "main"


def _has_remote() -> bool:
    rc, out, _ = _git("remote")
    return rc == 0 and bool(out.strip())


def _schedule_self_restart(delay_sec: int = 4) -> bool:
    """SOURCE mode only: relaunch `python web.py` after a short delay."""
    py     = sys.executable
    script = str(Path(RESOURCE_DIR) / "web.py")
    ps_cmd = (
        f"Start-Sleep -Seconds {delay_sec}; "
        f"Start-Process -FilePath \"{py}\" -ArgumentList '-u',\"{script}\" "
        f"-WorkingDirectory \"{BASE_DIR}\" -WindowStyle Hidden "
        f"-RedirectStandardOutput \"_web_server.log\" "
        f"-RedirectStandardError \"_web_server.err.log\""
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            cwd=str(BASE_DIR),
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            close_fds=True,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("[Update] Could not spawn restarter: %s", exc)
        return False
    threading.Timer(2.0, lambda: os._exit(0)).start()
    logger.info("[Update] Restart scheduled — server will relaunch in ~%ds.", delay_sec)
    return True


def _git_check() -> dict:
    if not _git_repo_present():
        return {"ok": False, "error": "This install isn't a git checkout and isn't a packaged "
                                      "build, so it can't self-update."}
    if not _has_remote():
        return {"ok": False, "error": "No GitHub remote is configured for this checkout."}
    branch = _current_branch()
    rc, _, err = _git("fetch", "origin", branch, timeout=120)
    if rc != 0:
        return {"ok": False, "error": f"Couldn't reach GitHub (git fetch): {err or 'unknown error'}"}
    _, behind_str, _ = _git("rev-list", "--count", f"HEAD..origin/{branch}")
    try:
        behind = int(behind_str or "0")
    except ValueError:
        behind = 0
    _, status_out, _ = _git("status", "--porcelain")
    _, cur_desc, _ = _git("log", "-1", "--pretty=%h %s (%cr)")
    changelog: list = []
    if behind:
        _, log_out, _ = _git("log", "--pretty=%h %s", f"HEAD..origin/{branch}")
        changelog = [ln for ln in log_out.splitlines() if ln.strip()][:50]
    return {
        "ok": True, "mode": "git", "branch": branch,
        "update_available": behind > 0, "behind_by": behind,
        "current": cur_desc, "current_version": APP_VERSION,
        "dirty": bool(status_out.strip()), "notes": "\n".join(changelog),
    }


def _git_apply(force: bool) -> dict:
    branch = _current_branch()
    _git("fetch", "origin", branch, timeout=120)
    _, status_out, _ = _git("status", "--porcelain")
    if status_out.strip() and not force:
        return {"ok": False, "dirty": True,
                "error": "This machine has local edits to tracked files. Use Force update to "
                         "overwrite with the GitHub version (config/data are safe — untracked)."}
    if force:
        rc, out, err = _git("reset", "--hard", f"origin/{branch}", timeout=180)
    else:
        rc, out, err = _git("pull", "--ff-only", "origin", branch, timeout=180)
    if rc != 0:
        return {"ok": False, "error": f"Update failed: {err or out or 'unknown error'}"}
    _schedule_self_restart()
    return {"ok": True, "mode": "git", "restarting": True}


# ---- release mode (frozen .exe) -------------------------------------------

def _release_check() -> dict:
    try:
        rel = _release_latest()
    except req.HTTPError as exc:  # pylint: disable=broad-except
        code = getattr(exc.response, "status_code", 0)
        if code == 404:
            # 404 is ambiguous: either no releases yet (repo readable) or the
            # repo is private/inaccessible. Disambiguate by probing the repo.
            if _repo_accessible():
                return {"ok": True, "mode": "release",
                        "current_version": APP_VERSION,
                        "latest_version": APP_VERSION,
                        "update_available": False,
                        "notes": "",
                        "message": "No releases published yet — publish one with release.ps1."}
            return {"ok": False, "error": (
                "Couldn't read the GitHub repo. If it's private, add a token to config.json "
                "under \"update\": {\"github_token\": \"...\"} (needs 'repo' read access), "
                "or make the repo public.")}
        if code in (401, 403):
            return {"ok": False, "error": (
                "GitHub denied the request. Add a valid token to config.json under "
                "\"update\": {\"github_token\": \"...\"} with 'repo' read access.")}
        return {"ok": False, "error": f"GitHub error: {exc}"}
    except Exception as exc:  # pylint: disable=broad-except
        return {"ok": False, "error": f"Couldn't reach GitHub: {exc}"}

    latest = str(rel.get("tag_name") or rel.get("name") or "")
    asset = _pick_zip_asset(rel)
    available = _parse_version(latest) > _parse_version(APP_VERSION)
    return {
        "ok": True, "mode": "release",
        "current_version": APP_VERSION,
        "latest_version": latest.lstrip("vV"),
        "update_available": bool(available and asset),
        "notes": rel.get("body") or "",
        "published_at": rel.get("published_at") or "",
        "asset_name": (asset or {}).get("name", ""),
        "asset_size": (asset or {}).get("size", 0),
        "no_asset": asset is None,
    }


def _download_asset(asset: dict, dest: Path) -> None:
    """Stream a release asset to `dest`, updating progress state as it goes.
    Works for private repos (API asset URL + octet-stream) and public ones."""
    total = int(asset.get("size") or 0)
    _set_update_state(phase="downloading", downloaded=0, total=total, message="Downloading update…")
    # For private repos we must hit the API asset endpoint with octet-stream.
    if _gh_token() and asset.get("url"):
        url, headers = asset["url"], _gh_headers("application/octet-stream")
    else:
        url, headers = asset.get("browser_download_url"), _gh_headers("application/octet-stream")
    with req.get(url, headers=headers, stream=True, timeout=120, allow_redirects=True) as r:
        r.raise_for_status()
        if not total:
            total = int(r.headers.get("Content-Length") or 0)
            _set_update_state(total=total)
        done = 0
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)
                    done += len(chunk)
                    _set_update_state(downloaded=done)


# One-shot scheduled task used to run the swap helper independently of the
# service process (so `sc stop` of ourselves doesn't kill the helper mid-swap).
_UPDATE_TASK_NAME = "ShowTVDownloaderSelfUpdate"


def _write_apply_helper(staging_app: Path, update_dir: Path) -> Path:
    """Write a PowerShell helper that stops the service, mirrors the new app
    files over the install dir, restarts the service, then removes the one-shot
    scheduled task that launched it."""
    install_dir = str(EXE_DIR)
    log_path = str(update_dir / "apply_update.log")
    ps = f"""
$ErrorActionPreference = 'Continue'
$svc  = '{SERVICE_NAME_FOR_UPDATER}'
$src  = '{str(staging_app)}'
$dst  = '{install_dir}'
$task = '{_UPDATE_TASK_NAME}'
Start-Transcript -Path '{log_path}' -Force | Out-Null
Write-Output "Stopping $svc..."
sc.exe stop $svc | Out-Null
for ($i = 0; $i -lt 60; $i++) {{
    $s = (Get-Service -Name $svc -ErrorAction SilentlyContinue)
    if ($null -eq $s -or $s.Status -eq 'Stopped') {{ break }}
    Start-Sleep -Seconds 1
}}

# The service AND any worker subprocesses (watcher/campaign) are all the same
# onedir exe, so they keep $dst\\ShowTVDownloader.exe and _internal\\*.dll locked
# until every one of them exits. sc stop only stops the service; lingering
# processes would make robocopy fail with ERROR 32 (in use) and silently leave
# the OLD version in place. Wait for them to exit, then force-kill any straggler
# whose image lives under the install dir.
$dstLower = $dst.ToLower()
function Get-AppProcs {{
    Get-CimInstance Win32_Process -Filter "Name='ShowTVDownloader.exe'" -ErrorAction SilentlyContinue |
        Where-Object {{ $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($dstLower) }}
}}
for ($i = 0; $i -lt 30; $i++) {{
    if (@(Get-AppProcs).Count -eq 0) {{ break }}
    Start-Sleep -Seconds 1
}}
$stuck = @(Get-AppProcs)
if ($stuck.Count -gt 0) {{
    Write-Output ("Force-killing {{0}} lingering process(es) holding the install dir..." -f $stuck.Count)
    foreach ($p in $stuck) {{ Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }}
    Start-Sleep -Seconds 3
}}

Write-Output "Mirroring new build into $dst..."
robocopy $src $dst /E /R:1 /W:2 /NFL /NDL /NJH /NJS
$rc = $LASTEXITCODE
Write-Output "robocopy exit code: $rc"
if ($rc -ge 8) {{
    Write-Output "ERROR: file swap FAILED (exit $rc) - install dir may be partially updated."
}} else {{
    Write-Output "File swap OK."
}}
Write-Output "Starting $svc..."
sc.exe start $svc | Out-Null
Start-Sleep -Seconds 2
Remove-Item -LiteralPath $src -Recurse -Force -ErrorAction SilentlyContinue
Write-Output "Removing one-shot task $task..."
schtasks /Delete /TN $task /F | Out-Null
Stop-Transcript | Out-Null
""".strip()
    helper = update_dir / "apply_update.ps1"
    helper.write_text(ps, encoding="utf-8")
    return helper


def _spawn_apply_helper(helper: Path) -> None:
    """Run the swap helper via a one-shot Scheduled Task running as SYSTEM.

    A scheduled task runs in its own Task Scheduler-hosted process, completely
    independent of this service. That's essential: the helper's first act is to
    stop *this* service, and anything spawned as our child would be torn down
    along with us before it could copy the new files and start us again.
    """
    # A tiny .cmd launcher avoids schtasks' fragile /TR quote handling.
    launcher = helper.parent / "run_update.cmd"
    launcher.write_text(
        "@echo off\r\n"
        f'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{helper}"\r\n',
        encoding="utf-8",
    )
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    # (Re)create the one-shot task, then trigger it immediately.
    subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", _UPDATE_TASK_NAME,
         "/RU", "SYSTEM", "/RL", "HIGHEST", "/SC", "ONCE", "/ST", "23:59",
         "/TR", str(launcher)],
        capture_output=True, text=True, creationflags=flags,
    )
    subprocess.Popen(
        ["schtasks", "/Run", "/TN", _UPDATE_TASK_NAME],
        creationflags=flags, close_fds=True,
    )


def _run_release_update(force: bool = False) -> None:
    """Background worker: download + stage + hand off to the swap/restart helper."""
    import shutil
    import zipfile
    try:
        rel = _release_latest()
        latest = str(rel.get("tag_name") or rel.get("name") or "")
        asset = _pick_zip_asset(rel)
        if not asset:
            _set_update_state(active=False, phase="error",
                              error="The latest release has no .zip build asset to download.")
            return
        _set_update_state(active=True, phase="downloading", done=False, error="",
                          from_version=APP_VERSION, to_version=latest.lstrip("vV"))

        update_dir = DATA_DIR / "_update"
        if update_dir.exists():
            shutil.rmtree(update_dir, ignore_errors=True)
        update_dir.mkdir(parents=True, exist_ok=True)
        zip_path = update_dir / "build.zip"
        _download_asset(asset, zip_path)

        _set_update_state(phase="extracting", pct=100, message="Extracting…")
        staging = update_dir / "staging"
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(staging)
        zip_path.unlink(missing_ok=True)

        # The build dir is whichever folder (or staging itself) holds the exe.
        exe_name = Path(sys.executable).name
        app_src = None
        for cand in [staging, *[p for p in staging.rglob("*") if p.is_dir()]]:
            if (cand / exe_name).exists():
                app_src = cand
                break
        if app_src is None:
            _set_update_state(active=False, phase="error",
                              error=f"Downloaded build didn't contain {exe_name}.")
            return

        _set_update_state(phase="installing", message="Installing… the service will restart.")
        helper = _write_apply_helper(app_src, update_dir)
        _spawn_apply_helper(helper)
        _set_update_state(phase="restarting", done=True,
                          message="Update downloaded. Restarting service…")
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("[Update] release update failed")
        _set_update_state(active=False, phase="error", error=str(exc))


# ---- routes ---------------------------------------------------------------

@app.get("/api/update/check")
def api_update_check():
    if IS_FROZEN:
        return jsonify(_release_check())
    return jsonify(_git_check())


@app.post("/api/update/apply")
def api_update_apply():
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    if IS_FROZEN:
        st = _get_update_state()
        if st["active"] and not st["done"] and st["phase"] not in ("error",):
            return jsonify({"ok": True, "mode": "release", "started": True,
                            "message": "Update already in progress."})
        _set_update_state(active=True, phase="starting", pct=0, downloaded=0, total=0,
                          done=False, error="", message="Starting update…")
        threading.Thread(target=_run_release_update, kwargs={"force": force},
                         daemon=True, name="ReleaseUpdate").start()
        return jsonify({"ok": True, "mode": "release", "started": True})
    return jsonify(_git_apply(force))


@app.get("/api/update/progress")
def api_update_progress():
    return jsonify(_get_update_state())


# ---------------------------------------------------------------------------
# Shared startup — used by both `python web.py` and the Windows service wrapper
# ---------------------------------------------------------------------------

def _clear_stale_running_status() -> None:
    """Clear any stale "running" scan status left over from a prior process."""
    if not STATE_FILE.exists():
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as _sf:
            _st = json.load(_sf)
        if _st.get("scan_status", {}).get("status") == "running":
            _st["scan_status"]["status"] = "idle"
            _st["scan_status"]["detail"] = ""
            with open(STATE_FILE, "w", encoding="utf-8") as _sf:
                json.dump(_st, _sf, indent=2)
    except (json.JSONDecodeError, OSError, KeyError):
        pass


def bootstrap() -> None:
    """Idempotent startup work shared by the CLI and the service: clear stale
    status, log config warnings, and start the background watcher + scheduler."""
    _clear_stale_running_status()
    try:
        from downloader import validate_config
        for _w in validate_config(load_config()):
            logging.getLogger("web").warning("[Config] %s", _w)
    except Exception as _cfg_exc:  # pylint: disable=broad-except
        logging.getLogger("web").debug("[Config] validation skipped: %s", _cfg_exc)
    start_completion_watcher()
    start_auto_scheduler()


def run_foreground(host: str = "0.0.0.0", port: int = 5000, debug: bool = False) -> None:
    """Run the dev/CLI server in the foreground (blocking)."""
    bootstrap()
    # use_reloader=False is REQUIRED: the reloader spawns a worker child, and on
    # restart the parent's death orphans that worker — each orphan keeps its own
    # scheduler + CompletionWatcher alive, which (historically) piled up into many
    # overlapping downloader runs. One process, one scheduler, one watcher.
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Jellyfin Downloader Web UI")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_foreground(host=args.host, port=args.port, debug=args.debug)
