# 🎬 Jellyfin Media Auto-Downloader
**Project Spec & Architecture Document**

---

## Overview

A background service that keeps your Jellyfin media library automatically up to date by scanning your NAS directly via UNC paths. Libraries are fully configurable — you can add as many directories as you want, each with their own type, path, and settings. The torrent source is configurable too — currently SceneTime, but swappable in the future. It reads your existing folder structure to know what you have, detects missing movies, missing episodes, and missing seasons, and automatically finds and downloads high-quality 1080p versions — dropping them straight into the correct NAS folder. No Jellyfin API, no hardcoded paths, no duplicates, no bad encodes.

---

## Torrent Source — SceneTime

SceneTime is a private tracker, so the downloader logs in using your credentials and either:
- **Scrapes the search page** for a given title/episode
- **Uses the RSS feed** (if SceneTime provides one) to catch new releases automatically

### Login & Session Handling
- Logs in once at startup using username + password from config
- Stores the session cookie for subsequent requests
- Automatically re-authenticates if the session expires

### Search Strategy
```
For TV:   Search "Show Name S01E01 1080p"  → parse results
For Movie: Search "Movie Name 2024 1080p"  → parse results
```

### Why SceneTime Helps Quality
- Private tracker = curated scene releases only
- Scene groups follow strict encoding standards
- Much less garbage than public trackers
- PROPER releases (fixes for bad encodes) are clearly tagged

---

## Quality Filtering — No Stutter, No Bad Audio

Stuttering and audio issues almost always come from bad encodes. The service enforces strict filtering before anything gets queued.

### Video — No Stutter
| Filter | Rule |
|---|---|
| Resolution | Must contain `1080p` in release name |
| Video codec | Must be `x264`, `x265`, or `HEVC` |
| Source | Prefer `BluRay`, `WEB-DL`, `WEBRip` — skip `CAM`, `TS`, `HDCAM`, `HDTS` |
| File size (episode) | 0.5 GB – 4.0 GB (too small = garbage encode) |
| File size (movie) | 2.0 GB – 20.0 GB |
| Re-encode flag | Skip releases tagged `REENC` or `REPACK` unless it's a PROPER fix |
| PROPER handling | If a PROPER exists for something already queued, prefer the PROPER |

### Audio — No Missing/Bad Audio
| Filter | Rule |
|---|---|
| Audio codec | Must contain `DTS`, `TrueHD`, `AC3`, `DD`, `AAC`, or `EAC3` in release name |
| Skip if | Release name contains `SILENT`, `NOAUDIO`, or has no audio tag at all |
| Prefer | `DTS` > `TrueHD` > `AC3/DD` > `AAC` |

### Release Groups (Whitelist)
Only grab from known good scene groups — everything else is skipped:
```
NTb, FLUX, CMRG, SPARKS, FGT, YIFY (avoid for audio), NTG,
EVOLVE, DEFLATE, DIMENSION, LOL, KILLERS, ROVERS, GHOST
```

### Full Filter Decision Tree
```
New release found on SceneTime:

├── Is it 1080p?                    → No  → ❌ Skip
├── Is source BluRay/WEB-DL/WEBRip? → No  → ❌ Skip (no CAM/TS)
├── Is video codec x264/x265/HEVC?  → No  → ❌ Skip
├── Does it have an audio tag?      → No  → ❌ Skip
├── Is audio DTS/AC3/AAC/TrueHD?   → No  → ❌ Skip
├── Is release group whitelisted?   → No  → ❌ Skip
├── Is file size in valid range?    → No  → ❌ Skip
├── Is it a CAM/TS/HDCAM release?  → Yes → ❌ Skip
└── Passes all filters             → ✅ Queue in qBittorrent
```

---

## Core Features

### 📂 Configurable Library System
- All libraries defined in `config.json` — nothing hardcoded
- Each library has a `type` (`tv` or `movie`), `name`, `path`, `enabled` toggle, and its own `watchlist`
- Add as many libraries as you want with no code changes
- Disable a library with `"enabled": false` without losing config

### 📂 NAS Path Scanning (No Jellyfin API)
- Scans configured UNC paths directly
- Reads folder/file names to build full inventory of what you have
- If the file is already on disk → skip, never re-download

### 📺 TV Show Management
- Inventories every show, season, and episode from your TV library paths
- Checks TMDB for what episodes should exist
- Detects missing seasons and missing individual episodes
- Monitors for new episodes as they air

### 🎥 Movie Management
- Inventories every movie from your Movie library paths
- Per-library watchlist — movies you want downloaded
- Checks disk before ever queuing a download

### ⬇️ qBittorrent Integration
- Sends magnet links to qBittorrent Web API
- Saves directly to the correct library UNC path
- Creates proper Jellyfin folder structure automatically

### 🔁 Schedule
- Configurable interval (default every 6 hours)
- Runs as a background service

### 🔔 Notifications
- Discord webhook or Ntfy push notification on:
  - New episode downloaded
  - Movie downloaded
  - Missing season filled
  - Error / torrent not found

---

## Config File (`config.json`)

```json
{
  "torrent_sources": [
    {
      "name":     "SceneTime",
      "enabled":  true,
      "url":      "https://www.scenetime.com",
      "username": "your_scenetime_username",
      "password": "your_scenetime_password",
      "rss_feed": "https://www.scenetime.com/rss.php?feed=dl&passkey=your_passkey"
    }
  ],

  "libraries": [
    {
      "name":    "TV Shows",
      "type":    "tv",
      "path":    "\\\\192.168.0.181\\Jellyfin\\TV Shows",
      "enabled": true,
      "watchlist": [
        "The Bear",
        "Severance",
        "House of the Dragon"
      ]
    },
    {
      "name":    "Movies",
      "type":    "movie",
      "path":    "\\\\192.168.0.181\\Jellyfin\\Movies",
      "enabled": true,
      "watchlist": [
        "The Dark Knight (2008)",
        "Dune Part Two (2024)"
      ]
    }
  ],

  "tmdb": {
    "api_key": "your_tmdb_api_key_here"
  },

  "qbittorrent": {
    "url":      "http://localhost:8080",
    "username": "admin",
    "password": "your_qbit_password"
  },

  "quality": {
    "resolution":            "1080p",
    "min_seeders":           5,
    "min_size_episode_gb":   0.5,
    "max_size_episode_gb":   4.0,
    "min_size_movie_gb":     2.0,
    "max_size_movie_gb":     20.0,
    "preferred_audio":       ["DTS", "TrueHD", "AC3", "EAC3", "AAC"],
    "skip_audio_tags":       ["SILENT", "NOAUDIO"],
    "preferred_video":       ["x264", "x265", "HEVC"],
    "allowed_sources":       ["BluRay", "WEB-DL", "WEBRip", "HDTV"],
    "blocked_sources":       ["CAM", "TS", "HDCAM", "HDTS", "DVDSCR", "R5"],
    "trusted_groups":        ["NTb", "FLUX", "CMRG", "SPARKS", "FGT", "NTG", "EVOLVE", "GHOST"],
    "prefer_proper":         true,
    "skip_reenc":            true
  },

  "notifications": {
    "discord_webhook": "https://discord.com/api/webhooks/your_webhook_here",
    "ntfy_topic":      "your_ntfy_topic"
  },

  "schedule_hours": 6
}
```

### Adding a New Torrent Source Later

Just add to the `torrent_sources` array — the finder will try each enabled source in order and use the first good result:

```json
{
  "name":     "MyNewTracker",
  "enabled":  true,
  "url":      "https://mynewtracker.com",
  "username": "your_username",
  "password": "your_password",
  "rss_feed": ""
}
```

### Adding a New Library Later

```json
{
  "name":    "Anime",
  "type":    "tv",
  "path":    "\\\\192.168.0.181\\Jellyfin\\Anime",
  "enabled": true,
  "watchlist": [
    "Jujutsu Kaisen",
    "Demon Slayer"
  ]
}
```

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│              Scheduler (cron / systemd)              │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│            Main Orchestrator (Python)                │
│  1. Load libraries + torrent sources from config    │
│  2. Scan each enabled library UNC path              │
│  3. Query TMDB for expected content                 │
│  4. Diff → build list of missing items              │
│  5. For each missing → search SceneTime             │
│  6. Apply quality filters                           │
│  7. Queue passing torrents in qBittorrent           │
│  8. Notify                                          │
└──────┬──────────────────────────┬───────────────────┘
       │                          │
       ▼                          ▼
┌────────────┐          ┌─────────────────────┐
│ UNC Path   │          │   TMDB API          │
│ Scanner    │          │   Episode/movie     │
│            │          │   metadata          │
│ Per library│          └─────────────────────┘
└────────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │  SceneTime Scraper    │
              │                       │
              │  - Login / session    │
              │  - Search by title    │
              │  - RSS feed monitor   │
              │  - Parse results      │
              │  - Apply all filters  │
              └──────────┬────────────┘
                         │
                         ▼
              ┌───────────────────────┐
              │  qBittorrent API      │
              │  Save to NAS path     │
              └───────────────────────┘
```

---

## File Structure

```
jellyfin_downloader/
├── downloader.py          # Main orchestrator
├── scanner.py             # UNC path scanner
├── tmdb_client.py         # TMDB API wrapper
├── sources/
│   ├── base_source.py     # Base class all sources extend
│   └── scenetime.py       # SceneTime login + search + RSS
├── torrent_finder.py      # Calls sources, applies quality filters
├── qbit_client.py         # qBittorrent Web API wrapper
├── notifier.py            # Discord / Ntfy
├── config.json            # Your configuration
├── state.json             # Download history (auto-managed)
├── setup.sh               # One-command installer
├── downloader.log         # Running log
└── README.md              # Setup instructions
```

The `sources/` folder is designed so you can add a new tracker later by just adding a new file — no changes to the rest of the code.

---

## CLI Usage

```bash
# See what WOULD be downloaded without downloading anything
python3 downloader.py --dry-run

# Run immediately
python3 downloader.py --run-now

# Scan and print current library inventory
python3 downloader.py --inventory

# Only process one specific library
python3 downloader.py --run-now --library "TV Shows"

# Install as background service
bash setup.sh
```

---

## What It Will NOT Do
- Will not touch files already on your NAS
- Will not grab CAM, TS, or any non-scene source
- Will not download anything missing audio or with bad encode flags
- Will not download anything already present on disk
- Will not use the Jellyfin API
- Will not require code changes to add libraries or torrent sources

---

## Future Ideas
- Per-library quality overrides (4K for one library)
- Web UI dashboard with queue and library status
- Support additional private trackers by adding to `sources/`
- Unify with manga downloader into one media service

---

## 📁 Naming Conventions

Correct naming is critical — it's how Jellyfin matches your files to the right metadata automatically.

---

### 🎬 Movies

**Folder + file name includes the IMDB ID** so Jellyfin locks onto the exact correct metadata with no guessing:

```
\\NAS\Jellyfin\Movies\
  The Dark Knight (2008) {imdb-tt0468569}\
    The Dark Knight (2008) {imdb-tt0468569}.mkv

  Dune Part Two (2024) {imdb-tt15239678}\
    Dune Part Two (2024) {imdb-tt15239678}.mkv
```

**Format:**
```
Folder:  {Movie Title} ({Year}) {imdb-ttXXXXXXX}
File:    {Movie Title} ({Year}) {imdb-ttXXXXXXX}.mkv
```

The IMDB ID is fetched automatically from TMDB during the download process — you never need to look it up yourself.

---

### 📺 TV Shows — New Show (doesn't exist on NAS yet)

When a show is brand new to your library, the service creates the full folder structure and names every file with the full Jellyfin-standard convention:

```
\\NAS\Jellyfin\TV Shows\
  The Bear\
    Season 01\
      The Bear - S01E01 - System.mkv
      The Bear - S01E02 - Hands.mkv
      The Bear - S01E03 - Brigade.mkv
    Season 02\
      The Bear - S02E01 - Fishes.mkv
```

**Format:**
```
Show Folder:   {Show Name}
Season Folder: Season {XX}              (zero-padded, e.g. Season 01, Season 02)
File:          {Show Name} - S{XX}E{XX} - {Episode Title}.mkv
```

---

### 📺 TV Shows — Existing Show (already on NAS)

When episodes are being added to a show that already exists, the service **reads the existing files in that show's folder** and matches the naming convention already in use — so nothing looks out of place:

```
Existing files found:
  Breaking Bad - S01E01 - Pilot.mkv
  Breaking Bad - S01E02 - Cat's in the Bag.mkv

New episode added using same convention:
  Breaking Bad - S02E01 - Seven Thirty-Seven.mkv  ✅
```

**How it detects the existing convention:**
1. Scans the show folder for existing `.mkv` / `.mp4` files
2. Parses the first file name to extract the pattern in use
3. Applies that same pattern to all new files added to that show
4. Falls back to the default Jellyfin standard if no existing files are found

---

### Season Folder Padding

Season folders are always zero-padded to 2 digits so they sort correctly in file explorers and Jellyfin:

```
✅  Season 01, Season 02 ... Season 09, Season 10
❌  Season 1, Season 2 ... Season 9, Season 10
```

---

### Episode & Season Number Padding

Episode and season numbers in file names are always zero-padded:

```
✅  S01E01, S01E09, S01E10, S02E01
❌  S1E1, S1E9, S1E10, S2E1
```

---

### Multi-Episode Files

If a torrent contains a multi-episode file (e.g. a 2-part premiere), it's named accordingly:

```
The Bear - S01E01E02 - System + Hands.mkv
```

---

### Where Episode Titles Come From

Episode titles are fetched automatically from TMDB during the download process — the same API call used to check for missing episodes also returns the episode title. No manual entry needed.

---

### Full Naming Examples

| Type | Example |
|---|---|
| Movie | `Oppenheimer (2023) {imdb-tt15398776}.mkv` |
| Movie folder | `Oppenheimer (2023) {imdb-tt15398776}\` |
| New TV episode | `Severance - S02E01 - Goodbye, Mrs. Selvig.mkv` |
| New TV season folder | `Season 02\` |
| Existing show (match) | Matches whatever naming pattern is already in use |
| Multi-episode | `The Bear - S02E01E02 - Fishes + Honeydew.mkv` |

