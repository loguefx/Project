"""
Scans UNC / local paths to build an inventory of what's already on disk.
Returns structured dicts so the orchestrator can diff against TMDB.
"""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches: Show Name - S01E02 - Episode Title.mkv  (Jellyfin standard)
_TV_FILE_RE = re.compile(
    r"^(?P<show>.+?)\s*[-–]\s*S(?P<season>\d{2})E(?P<episode>\d{2})"
    r"(?:E\d{2})*\s*(?:[-–]\s*(?P<title>.+?))?\.(?:mkv|mp4|avi|m4v)$",
    re.IGNORECASE,
)

# Matches multi-episode files in any common style:
#   S01E01E02      (contiguous)
#   S01E01-E02     (dash separated)
#   S01E01 - E02   (spaced)
#   S01E01.E02     (dot separated)
# Each additional episode REQUIRES the leading "E" so that resolution/quality
# tokens like "S01E01.1080p" are never mistaken for a second episode.
_TV_MULTI_EP_RE = re.compile(r"S\d{1,2}E\d{1,3}(?:[-_.\s]*E\d{1,3})+", re.IGNORECASE)

# Matches season folder: Season 01, Season 1, S01
_SEASON_FOLDER_RE = re.compile(r"^(?:Season\s*|S)(\d{1,2})$", re.IGNORECASE)

# Fallback: "Season" keyword anywhere in the folder name.
# Handles: "Show Name Season 3", "Show Season 3 (Uncensored)", "Show_Season 2"
# Negative lookbehind prevents false matches on "Offseason", "Preseason" etc.
_SEASON_FOLDER_LOOSE_RE = re.compile(
    r"(?<![A-Za-z])Season\s+(\d{1,2})\b", re.IGNORECASE
)

# UK convention: "Series 1", "Series 01"
_SERIES_FOLDER_RE = re.compile(r"^(?:Series\s*)(\d{1,2})$", re.IGNORECASE)

# Netflix/streaming half-season splits: "Part 1", "Part 2"
_PART_FOLDER_RE = re.compile(r"^Part\s+(\d{1,2})$", re.IGNORECASE)

# Show name followed by bare season number: "Stranger Things 2", "Wipeout 2023"
# Only match single/double digits (season numbers), not 4-digit years beyond 2030.
_SHOW_TRAILING_NUM_RE = re.compile(r"\s+(\d{1,2})$")

# Show name with inline Sxx: "Trip Tank S1", "Trip Tank S01"
_SHOW_INLINE_S_RE = re.compile(r"\bS(\d{1,2})$", re.IGNORECASE)

# Matches all Jellyfin folder ID formats:
#   Movie Title (2024) [tmdb-12345]
#   Movie Title (2024) [tvdb-12345]
#   Movie Title (2024) {imdb-tt1234567}
#   Movie Title (2024)
_MOVIE_FOLDER_RE = re.compile(
    r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)"
    r"(?:\s*[\[\{](?P<id_type>tmdb|tvdb|imdb)-(?P<id_value>[^\]\}]+)[\]\}])?",
    re.IGNORECASE,
)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv"}


# ---------------------------------------------------------------------------
# TV Scanner
# ---------------------------------------------------------------------------

def _scan_single_show(show_dir: Path) -> tuple[str, dict]:
    """Scan one show directory and return (show_name, seasons_dict)."""
    show_name = show_dir.name
    seasons: dict = {}

    try:
        entries = sorted(show_dir.iterdir())
    except PermissionError:
        logger.warning("[Scanner] Permission denied reading %s — skipping", show_dir)
        return show_name, seasons

    unrecognised_dirs: list = []  # track for show-level fallback

    for entry in entries:
        if entry.is_dir():
            season_num = _parse_season_folder(entry.name)
            if season_num is not None:
                episodes = _scan_episode_files(entry)
                if episodes:
                    # Merge (not overwrite) so "Season 5" + "Show Season 5" both count
                    seasons.setdefault(season_num, set()).update(episodes)
            else:
                inferred = _scan_episode_files_by_season(entry)
                if inferred:
                    for s_num, eps in inferred.items():
                        seasons.setdefault(s_num, set()).update(eps)
                else:
                    # Track dirs whose folder name AND filenames were both unrecognised
                    unrecognised_dirs.append(entry)
        elif entry.is_file() and entry.suffix.lower() in VIDEO_EXTS:
            parsed = parse_tv_filename(entry.name)
            if parsed:
                season, episode, _ = parsed
                seasons.setdefault(season, set()).add(episode)

    # ── Show-level fallback ─────────────────────────────────────────────────
    # If NOTHING was recognised (anthology shows like "American Horror Story /
    # American Horror Story_ Asylum", "Stranger Things / Stranger Things", etc.)
    # treat each subfolder that has video files as a sequential season to
    # prevent the downloader from treating all content as "missing".
    if not seasons and unrecognised_dirs:
        dirs_with_video = []
        for d in sorted(unrecognised_dirs):
            try:
                has_video = any(
                    f.suffix.lower() in VIDEO_EXTS
                    for f in d.iterdir()
                    if f.is_file()
                )
            except PermissionError:
                has_video = False
            if has_video:
                dirs_with_video.append(d)

        if dirs_with_video:
            for seq_num, d in enumerate(dirs_with_video, start=1):
                try:
                    count = sum(
                        1 for f in d.iterdir()
                        if f.is_file() and f.suffix.lower() in VIDEO_EXTS
                    )
                except PermissionError:
                    count = 1
                seasons[seq_num] = set(range(1, count + 1))
            logger.debug(
                "[Scanner] Show-level fallback for %r: %d anthology/special "
                "subfolder(s) mapped to seasons 1‒%d",
                show_name, len(dirs_with_video), len(dirs_with_video),
            )

    return show_name, seasons


def scan_tv_library(library_path: str, workers: int = 8) -> dict:
    """
    Scan a TV library path and return:
    {
        "Show Name": {
            1: {1, 2, 3},   # season -> set of episode numbers
            2: {1, 2},
        },
        ...
    }
    Uses a thread pool to scan show folders in parallel (IO-bound NAS reads).
    """
    root = Path(library_path)
    inventory: dict = {}

    if not root.exists():
        logger.warning("[Scanner] TV library path does not exist: %s", library_path)
        return inventory

    try:
        show_dirs = [d for d in root.iterdir() if d.is_dir()]
    except PermissionError:
        logger.warning("[Scanner] Permission denied listing %s", library_path)
        return inventory

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_scan_single_show, d): d for d in show_dirs}
        for future in as_completed(futures):
            try:
                show_name, seasons = future.result()
                inventory[show_name] = seasons
                if not seasons:
                    logger.debug("[Scanner] Show folder empty or unrecognised: %s", show_name)
            except Exception as exc:
                logger.warning("[Scanner] Error scanning %s: %s", futures[future], exc)

    logger.info(
        "[Scanner] TV library scanned: %d shows found at %s",
        len(inventory), library_path,
    )
    return inventory


# Blu-ray / DVD disc-rip filename markers, e.g.
#   "Legend Of Korra- Book Four- Balance - Disc 1 T00.mp4"
# Files like these don't carry SxxExx numbers but represent a *complete* season,
# so any season folder containing them must be treated as fully owned.
_DISC_RIP_RE = re.compile(
    r"(?i)(?:\bdisc\s*\d+\b|[ _.\-]T\d{2}\b|\bbook\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)\b)"
)
# When a season folder is identified as a disc rip, mark this many episodes as
# owned so the whole season is covered regardless of how TVMaze numbers it.
_DISC_RIP_OWNED_CEIL = 300


def _scan_episode_files_by_season(season_dir: Path) -> dict[int, set[int]]:
    """
    Like _scan_episode_files but groups results by season number inferred from
    the filenames themselves.  Used when the containing folder name doesn't
    follow the standard "Season XX" convention.
    Returns {season_num: {episode_nums}}.
    """
    result: dict[int, set[int]] = {}
    video_files_seen: list = []
    try:
        entries = list(season_dir.iterdir())
    except PermissionError:
        return result

    for f in entries:
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            video_files_seen.append(f)
            parsed = parse_tv_filename(f.name)
            if parsed:
                season, episode, _ = parsed
                result.setdefault(season, set()).add(episode)
                multi = _TV_MULTI_EP_RE.search(f.name)
                if multi:
                    for ep_str in re.findall(r"E(\d{1,3})", multi.group(0), re.IGNORECASE):
                        result[season].add(int(ep_str))
        elif f.is_dir():
            try:
                for sub in f.iterdir():
                    if sub.is_file() and sub.suffix.lower() in VIDEO_EXTS:
                        parsed = parse_tv_filename(sub.name)
                        if parsed:
                            season, episode, _ = parsed
                            result.setdefault(season, set()).add(episode)
            except PermissionError:
                se_match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", f.name)
                if se_match:
                    s, e = int(se_match.group(1)), int(se_match.group(2))
                    result.setdefault(s, set()).add(e)

    nonstandard = [f for f in video_files_seen if not parse_tv_filename(f.name)]

    if nonstandard and any(_DISC_RIP_RE.search(f.name) for f in nonstandard):
        # Blu-ray/DVD disc rip of a complete season — treat as fully owned so we
        # never duplicate episodes the rip already contains.
        seasons = set(result.keys())
        inferred_season = _parse_season_folder(season_dir.name)
        if inferred_season is not None:
            seasons.add(inferred_season)
        for s in (seasons or {1}):
            result.setdefault(s, set()).update(range(1, _DISC_RIP_OWNED_CEIL + 1))
        logger.info(
            "[Scanner] Disc-rip season %s — %d rip file(s) present; treating season as fully owned to avoid duplicates",
            season_dir.name, len(nonstandard),
        )
    elif not result and video_files_seen:
        # Non-standard naming but not a disc rip; infer season from the folder name
        # and count files as episodes 1..N.
        inferred_season = _parse_season_folder(season_dir.name)
        if inferred_season is not None:
            count = len(video_files_seen)
            result[inferred_season] = set(range(1, count + 1))
            logger.debug(
                "[Scanner] Non-standard filenames in %s — sequential fallback: "
                "season %d, %d episode(s) assumed",
                season_dir.name, inferred_season, count,
            )

    return result


def _scan_episode_files(season_dir: Path) -> set[int]:
    episodes: set[int] = set()
    try:
        entries = list(season_dir.iterdir())
    except PermissionError:
        return episodes

    video_files_seen: list = []   # track for fallback

    for f in entries:
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            video_files_seen.append(f)
            parsed = parse_tv_filename(f.name)
            if parsed:
                _, episode, _ = parsed
                episodes.add(episode)
                # Handle multi-episode files (S01E01E02, S01E01-E02, …)
                multi = _TV_MULTI_EP_RE.search(f.name)
                if multi:
                    for ep_str in re.findall(r"E(\d{1,3})", multi.group(0), re.IGNORECASE):
                        episodes.add(int(ep_str))
        elif f.is_dir():
            # Look inside release subfolders (season packs, unflattened downloads)
            # This prevents re-downloading episodes that exist but haven't been renamed yet
            try:
                for sub in f.iterdir():
                    if sub.is_file() and sub.suffix.lower() in VIDEO_EXTS:
                        parsed = parse_tv_filename(sub.name)
                        if parsed:
                            _, episode, _ = parsed
                            episodes.add(episode)
            except PermissionError:
                # Can't read inside the subfolder — check its name for SxxExx clues
                se_match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", f.name)
                if se_match:
                    episodes.add(int(se_match.group(2)))
                    logger.debug("[Scanner] Inferred episode %s from locked folder name: %s",
                                 se_match.group(2), f.name)

    # ── Fallback / disc-rip handling for non-standard naming ───────────────
    # Files that don't carry an SxxExx tag (e.g. Blu-ray rips "Disc 1 T00").
    nonstandard = [f for f in video_files_seen if not parse_tv_filename(f.name)]

    if nonstandard and any(_DISC_RIP_RE.search(f.name) for f in nonstandard):
        # This folder is a Blu-ray/DVD disc rip of a complete season (track files
        # like "Disc 1 T00" don't map 1:1 to episode numbers, and there may be more
        # or fewer files than episodes). Treat the WHOLE season as owned so we never
        # re-download episodes the rip already contains — even if a few SxxExx files
        # were previously (wrongly) downloaded into it.
        episodes |= set(range(1, _DISC_RIP_OWNED_CEIL + 1))
        logger.info(
            "[Scanner] Disc-rip season %s — %d rip file(s) present; treating season as fully owned to avoid duplicates",
            season_dir.name, len(nonstandard),
        )
    elif not episodes and video_files_seen:
        # Non-standard naming but not a disc rip — count sequentially so a
        # fully-populated season isn't treated as empty.
        count = len(video_files_seen)
        episodes = set(range(1, count + 1))
        logger.debug(
            "[Scanner] Non-standard filenames in %s — using sequential fallback: %d episode(s) assumed",
            season_dir.name, count,
        )

    return episodes


def _parse_season_folder(name: str) -> Optional[int]:
    """
    Return the season number for a folder name, or None if not a season folder.

    Recognised patterns (all case-insensitive):
      Season N / Season NN / S01              — standard
      Show Name Season N (qualifier)          — prefixed / qualified
      Series N                                — UK convention
      Part N                                  — Netflix half-season splits
      Show Name N  (trailing 1–2 digit num)   — e.g. "Stranger Things 2"
      Show Name S1 / Show Name S01            — e.g. "Trip Tank S1"
    """
    # 1. Standard: "Season 1", "Season 01", "S01"
    m = _SEASON_FOLDER_RE.match(name)
    if m:
        return int(m.group(1))

    # 2. "Season" keyword anywhere: "Show Name Season 3 (Uncensored)"
    m = _SEASON_FOLDER_LOOSE_RE.search(name)
    if m:
        return int(m.group(1))

    # 3. UK: "Series 1", "Series 12"
    m = _SERIES_FOLDER_RE.match(name)
    if m:
        return int(m.group(1))

    # 4. Netflix splits: "Part 1", "Part 2"
    m = _PART_FOLDER_RE.match(name)
    if m:
        return int(m.group(1))

    # 5. Inline Sxx at end: "Trip Tank S1", "Show S02"
    m = _SHOW_INLINE_S_RE.search(name)
    if m:
        return int(m.group(1))

    # 6. Trailing bare number: "Stranger Things 2", "Wipeout 3"
    #    Guard against 4-digit years (>= 2000) so "Wipeout 2023" is not mis-mapped.
    m = _SHOW_TRAILING_NUM_RE.search(name)
    if m:
        num = int(m.group(1))
        return num  # 1–2 digit only (regex), never a year

    return None


def parse_tv_filename(filename: str) -> Optional[tuple[int, int, str]]:
    """
    Parse a TV episode filename.
    Returns (season_number, episode_number, episode_title) or None.
    """
    m = _TV_FILE_RE.match(filename)
    if m:
        season = int(m.group("season"))
        episode = int(m.group("episode"))
        title = (m.group("title") or "").strip()
        return season, episode, title

    # Fallback: look for SxxExx anywhere in the filename
    fallback = re.search(r"S(\d{1,2})E(\d{1,2})", filename, re.IGNORECASE)
    if fallback:
        return int(fallback.group(1)), int(fallback.group(2)), ""

    return None


# ---------------------------------------------------------------------------
# Movie Scanner
# ---------------------------------------------------------------------------

def scan_movie_library(library_path: str) -> dict:
    """
    Scan a Movie library path and return:
    {
        "The Dark Knight (2008)": {"imdb_id": "tt0468569", "path": "/full/path/folder"},
        ...
    }
    """
    root = Path(library_path)
    inventory: dict = {}

    if not root.exists():
        logger.warning("[Scanner] Movie library path does not exist: %s", library_path)
        return inventory

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        parsed = parse_movie_foldername(entry.name)
        if parsed:
            title, year, id_type, id_value = parsed
            key = f"{title} ({year})"
            inventory[key] = {
                "title": title,
                "year": year,
                "id_type": id_type,
                "id_value": id_value,
                "path": str(entry),
                "has_file": _has_video_file(entry),
            }
        else:
            logger.debug("[Scanner] Unrecognised movie folder: %s", entry.name)

    logger.info(
        "[Scanner] Movie library scanned: %d movies found at %s",
        len(inventory), library_path,
    )
    return inventory


def parse_movie_foldername(name: str) -> Optional[tuple[str, int, Optional[str], Optional[str]]]:
    """
    Parse a movie folder name.
    Returns (title, year, id_type, id_value) where id_type is 'tmdb'/'tvdb'/'imdb' or None.
    """
    m = _MOVIE_FOLDER_RE.match(name)
    if m:
        title = m.group("title").strip()
        year = int(m.group("year"))
        id_type = (m.group("id_type") or "").lower() or None
        id_value = m.group("id_value") or None
        return title, year, id_type, id_value
    return None


def _has_video_file(directory: Path) -> bool:
    for f in directory.iterdir():
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            return True
    return False


# ---------------------------------------------------------------------------
# Naming helpers (used by downloader to create correct Jellyfin structure)
# ---------------------------------------------------------------------------

def make_tv_season_folder(season_num: int, zero_pad: bool = False) -> str:
    """Return a season folder name, e.g. 'Season 4' or 'Season 04'.

    Pass zero_pad=True only when the existing show folder already uses that style.
    The default is no padding to match Jellyfin's standard convention.
    """
    if zero_pad:
        return f"Season {season_num:02d}"
    return f"Season {season_num}"


def find_existing_season_folder(show_dir: "Path", season_num: int):  # type: ignore[name-defined]
    """
    Return the Path of the existing season folder for *season_num* inside
    *show_dir*, regardless of how it is named ("Season 4", "Season 04",
    "SpongeBob SquarePants Season 4", …).  Returns None if no match found.
    """
    try:
        for child in show_dir.iterdir():
            if child.is_dir() and _parse_season_folder(child.name) == season_num:
                return child
    except OSError:
        pass
    return None


def detect_season_padding(show_dir: "Path") -> bool:  # type: ignore[name-defined]
    """
    Return True only if the MAJORITY of existing season folders in *show_dir*
    use zero-padded numbers (e.g. "Season 04").

    Checking just the first folder (old behaviour) was non-deterministic because
    os.scandir order is filesystem-dependent — it would flip between padded and
    unpadded depending on which folder the OS returned first, producing mixed
    naming like "Season 1 / Season 2 / Season 04 / Season 05".
    """
    import re
    padded = unpadded = 0
    try:
        for child in show_dir.iterdir():
            if child.is_dir():
                m = re.match(r"Season\s+(\d+)$", child.name, re.IGNORECASE)
                if m:
                    if m.group(1).startswith("0"):
                        padded += 1
                    else:
                        unpadded += 1
    except OSError:
        pass
    # Only use zero-padding when ALL existing folders are padded.
    # A mixed situation should default to the Jellyfin standard (no padding).
    return padded > 0 and unpadded == 0


def make_tv_filename(show_name: str, season: int, episode: int, title: str) -> str:
    safe_title = _sanitise_filename(title) if title else ""
    base = f"{show_name} - S{season:02d}E{episode:02d}"
    if safe_title:
        base += f" - {safe_title}"
    return base + ".mkv"


def make_movie_folder(title: str, year: int, tmdb_id: Optional[int] = None) -> str:
    """Build a Jellyfin-standard movie folder name: Title (Year) [tmdb-XXXXX]"""
    base = f"{title} ({year})"
    if tmdb_id:
        base += f" [tmdb-{tmdb_id}]"
    return base


def make_movie_filename(title: str, year: int, tmdb_id: Optional[int] = None) -> str:
    return make_movie_folder(title, year, tmdb_id) + ".mkv"


def make_tv_show_folder(show_name: str, tmdb_id: Optional[int] = None) -> str:
    """Build a Jellyfin-standard TV show folder: Show Name [tmdb-XXXXX]"""
    base = _sanitise_filename(show_name)
    if tmdb_id:
        base += f" [tmdb-{tmdb_id}]"
    return base


_ID_TAG_RE = re.compile(
    r"\s*(?:\[(?:tmdb|tvdb|imdb)-[^\]]+\]|\{imdb-[^}]+\})",
    re.IGNORECASE,
)

def clean_show_name(raw: str) -> str:
    """Strip Jellyfin ID tags like [tmdb-12345] from a folder/show name."""
    return _ID_TAG_RE.sub("", raw).strip()


def detect_existing_naming_pattern(show_path: str) -> Optional[str]:
    """
    Scan an existing show folder and return the detected naming pattern.
    Returns a format string or None if no files found.
    """
    root = Path(show_path)
    if not root.exists():
        return None
    for child in root.rglob("*"):
        if child.is_file() and child.suffix.lower() in VIDEO_EXTS:
            parsed = parse_tv_filename(child.name)
            if parsed:
                return child.name  # Return a sample filename as pattern reference
    return None


def _sanitise_filename(name: str) -> str:
    """Remove characters that are illegal in Windows/NTFS filenames."""
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()
