# Jellyfin Media Auto-Downloader

Automatically keeps your Jellyfin library up to date. Scans your NAS directly via UNC paths, queries TMDB for what should exist, finds missing content on SceneTime, applies strict quality filters, and drops files straight into the correct NAS folder via qBittorrent.

---

## Quick Start

### 1. Install dependencies

```bash
# Linux / macOS
bash setup.sh

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 2. Configure

Edit `config.json` with your actual credentials:

| Field | Description |
|---|---|
| `torrent_sources[].username` | SceneTime username |
| `torrent_sources[].password` | SceneTime password |
| `torrent_sources[].rss_feed` | SceneTime RSS URL with your passkey |
| `libraries[].path` | UNC path to your NAS share |
| `libraries[].watchlist` | Shows / movies you want downloaded |
| `tmdb.api_key` | Get a free key at https://www.themoviedb.org/settings/api |
| `qbittorrent.url` | qBittorrent Web UI URL (default: http://localhost:8080) |
| `qbittorrent.password` | qBittorrent Web UI password |
| `notifications.discord_webhook` | Discord webhook URL (optional) |
| `notifications.ntfy_topic` | Ntfy topic name (optional) |

### 3. Run

```bash
# See what WOULD be downloaded (no changes made)
python downloader.py --dry-run

# Run immediately
python downloader.py --run-now

# Print current library inventory and exit
python downloader.py --inventory

# Only process one specific library
python downloader.py --run-now --library "TV Shows"

# Run as a background service (loops on schedule_hours interval)
python downloader.py
```

---

## File Structure

```
jellyfin_downloader/
├── downloader.py          # Main orchestrator + CLI
├── scanner.py             # UNC path scanner + Jellyfin naming
├── tmdb_client.py         # TMDB API wrapper
├── sources/
│   ├── base_source.py     # Abstract base all sources extend
│   └── scenetime.py       # SceneTime login + search + RSS
├── torrent_finder.py      # Quality filter pipeline
├── qbit_client.py         # qBittorrent Web API
├── notifier.py            # Discord + Ntfy notifications
├── config.json            # Your configuration (edit this)
├── state.json             # Download history (auto-managed)
├── requirements.txt       # Python dependencies
├── setup.sh               # One-command Linux installer
└── downloader.log         # Running log (auto-created)
```

---

## Quality Filters

The downloader enforces strict quality gates before anything gets queued:

- Resolution: must be **1080p**
- Source: **BluRay, WEB-DL, WEBRip, HDTV** only — no CAM/TS
- Video codec: **x264, x265, HEVC**
- Audio: **DTS, TrueHD, AC3, EAC3, AAC** — releases with `SILENT` or `NOAUDIO` are skipped
- Release groups: whitelisted only (`NTb, FLUX, CMRG, SPARKS, FGT, NTG, EVOLVE, GHOST`)
- File size: 0.5–4.0 GB per episode, 2.0–20.0 GB per movie
- PROPER releases are preferred over existing matches
- Re-encoded releases (`REENC`) are skipped

All thresholds are configurable in `config.json` under `quality`.

---

## Jellyfin Naming Convention

Files are saved in Jellyfin-standard format so metadata matches automatically.

### TV Shows
```
\\NAS\Jellyfin\TV Shows\
  The Bear\
    Season 01\
      The Bear - S01E01 - System.mkv
      The Bear - S01E02 - Hands.mkv
    Season 02\
      The Bear - S02E01 - Fishes.mkv
```

### Movies
```
\\NAS\Jellyfin\Movies\
  The Dark Knight (2008) {imdb-tt0468569}\
    The Dark Knight (2008) {imdb-tt0468569}.mkv
```

IMDB IDs are fetched automatically from TMDB — no manual lookup needed.

---

## Adding More Libraries

Just add an entry to the `libraries` array in `config.json` — no code changes required:

```json
{
  "name":    "Anime",
  "type":    "tv",
  "path":    "\\\\192.168.0.181\\Jellyfin\\Anime",
  "enabled": true,
  "watchlist": ["Jujutsu Kaisen", "Demon Slayer"]
}
```

## Adding More Torrent Sources

Add a new entry to `torrent_sources`. The app tries each enabled source in order and uses the first passing result. To add a new tracker, create `sources/yourtracker.py` extending `BaseSource` and register it in `downloader.py`'s `SOURCE_MAP`.

---

## Notifications

Set up Discord and/or Ntfy in `config.json`. You'll receive notifications for:
- New episode downloaded
- Movie downloaded
- Season fully queued
- Torrent not found (warning)
- Errors

---

## Requirements

- Python 3.11+
- qBittorrent with Web UI enabled
- TMDB API key (free at https://www.themoviedb.org)
- SceneTime account with passkey
- Network access to your NAS UNC path
