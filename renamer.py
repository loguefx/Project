"""
Renames downloaded video files from scene-style names to clean Jellyfin-friendly names.
Also flattens nested release subfolders into the Season folder, then removes them.

Before: Season 01/The.Bear.S01E01.1080p.HEVC.x265-MeGusta/The.Bear.S01E01.1080p.HEVC.x265-MeGusta.mkv
After:  Season 01/The Bear - S01E01.mkv   (release subfolder deleted)
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts"}
SUB_EXTENSIONS   = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}
KEEP_EXTENSIONS  = VIDEO_EXTENSIONS | SUB_EXTENSIONS

# Matches SxxExx or SxxExx-Exx (multi-episode)
_SE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})(?:[.-]?[Ee](\d{1,2}))?")

# Detects "Name - SxxExx - Title.ext" (title present after episode tag)
_WITH_TITLE_RE = re.compile(r"- S\d{2}E\d{2} - .+\.", re.IGNORECASE)


def _extract_se(filename: str) -> tuple[int, int, int | None] | None:
    m = _SE_RE.search(filename)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None


def _clean_show_name(raw: str) -> str:
    name = re.sub(r"\[(tmdb|tvdb|imdb)-\d+\]", "", raw, flags=re.I).strip()
    name = name.replace(".", " ").replace("_", " ")
    return re.sub(r" {2,}", " ", name).strip()


def _detect_naming_pattern(show_dir: Path) -> str:
    """
    Scan existing episode files in a show folder to detect the naming convention.
    Returns 'with_title' if files use "Show - SxxExx - Title.ext", else 'simple'.
    """
    try:
        for season_dir in show_dir.iterdir():
            if not season_dir.is_dir() or not _is_season_folder(season_dir.name):
                continue
            try:
                for f in season_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                        if _WITH_TITLE_RE.search(f.name):
                            return "with_title"
                        if _SE_RE.search(f.name):
                            return "simple"
            except PermissionError:
                continue
    except (PermissionError, OSError):
        pass
    return "simple"


def _get_episode_title(show_name: str, season: int, episode: int) -> str:
    """
    Look up an episode title from the local TVMaze disk cache (no network call).
    Returns empty string if not found.
    """
    try:
        from runtime_paths import DATA_DIR
        cache_path = DATA_DIR / "tvmaze_cache.json"
        if not cache_path.exists():
            return ""
        import json
        cache = json.loads(cache_path.read_text("utf-8"))
        entry = cache.get(show_name.lower(), {})
        titles = entry.get("ep_titles", {})
        # Keys may be int or str depending on how they were stored
        s_titles = titles.get(season) or titles.get(str(season)) or {}
        return s_titles.get(episode) or s_titles.get(str(episode)) or ""
    except Exception:
        return ""


def _sanitize_title(title: str) -> str:
    """Make an episode title safe for use in a filename."""
    # Remove characters illegal on Windows/NAS file systems
    title = re.sub(r'[<>:"/\\|?*]', "", title)
    title = title.strip(". ")
    return title[:60]  # cap length


def _make_clean_name(
    show_name: str,
    season: int,
    episode: int,
    end_ep: int | None,
    ext: str,
    include_title: bool = False,
) -> str:
    ep_tag = f"S{season:02d}E{episode:02d}-E{end_ep:02d}" if end_ep else f"S{season:02d}E{episode:02d}"
    if include_title and not end_ep:
        raw_title = _get_episode_title(show_name, season, episode)
        safe_title = _sanitize_title(raw_title)
        if safe_title:
            return f"{show_name} - {ep_tag} - {safe_title}{ext}"
    return f"{show_name} - {ep_tag}{ext}"


def _is_season_folder(name: str) -> bool:
    # "Season 1", "Season 01"
    if re.match(r"(?i)season\s*\d+", name):
        return True
    # "S01", "S1"  (bare Sxx with nothing else)
    if re.match(r"(?i)^S\d{1,2}$", name):
        return True
    # "Show Name Season 3 (Uncensored)", "Show_Season 2", "Show - Season 1"
    # Negative lookbehind prevents false matches on "Offseason", "Preseason".
    if re.search(r"(?<![A-Za-z])Season\s+\d+", name, re.IGNORECASE):
        return True
    # Defer to the scanner's full season-folder parser so the renamer recognises
    # EVERY convention the scanner does — "Part 4" (Netflix splits), "Series 2"
    # (UK), "Show Name 2", "Show Name S1", etc. Without this, release folders
    # inside e.g. "The Ranch/Part 4" are never flattened or removed.
    try:
        from scanner import _parse_season_folder
        return _parse_season_folder(name) is not None
    except Exception:
        return False


def _find_episode_file(season_dir: Path, season: int, episode: int) -> Path | None:
    """
    Return the first video file in *season_dir* whose name contains S{season:02d}E{episode:02d},
    regardless of filename format or extension.  Returns None if no match.
    This prevents renaming a new download on top of an already-present episode
    that uses a different naming convention (e.g. 'Show_S01E01_Title.mp4').
    """
    pattern = re.compile(rf"[Ss]{season:02d}[Ee]{episode:02d}", re.IGNORECASE)
    try:
        for f in season_dir.iterdir():
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS and pattern.search(f.name):
                return f
    except OSError:
        pass
    return None


def _try_remove_folder(folder: Path) -> None:
    """Remove a release subfolder and all its remaining contents (NFOs, SFVs, etc.).
    Silently skips if the folder is non-empty due to locked files."""
    try:
        shutil.rmtree(str(folder))
        logger.info("[Renamer] Removed release folder: %s", folder.name)
    except Exception as e:
        logger.warning("[Renamer] Could not remove folder %s: %s", folder.name, e)


def _is_season_pack_folder(name: str) -> bool:
    has_season  = bool(re.search(r"[Ss]\d{1,2}(?!\d*[Ee]\d)", name))
    has_episode = bool(re.search(r"[Ss]\d{1,2}[Ee]\d{1,2}", name))
    return has_season and not has_episode


def _is_under(path: Path, skip_paths: set[str]) -> bool:
    """True if *path* is one of, or nested inside, any path in *skip_paths*.
    Used to avoid touching files that belong to a torrent still downloading."""
    if not skip_paths:
        return False
    try:
        p = os.path.normcase(os.path.normpath(str(path)))
    except Exception:
        return False
    for s in skip_paths:
        if p == s or p.startswith(s + os.sep):
            return True
    return False


def rename_video_files(
    library_paths: list[str],
    dry_run: bool = False,
    skip_paths: "set[str] | None" = None,
    only_show_dirs: "set[str] | None" = None,
) -> dict:
    """
    Walk all library paths and rename/flatten scene-style video files.
    Never deletes any files from the NAS.

    skip_paths: normalized absolute paths of torrents that are NOT yet 100%
    complete. Any file/folder at or under one of these is left untouched so we
    never move a partially-downloaded file out of its release folder.

    only_show_dirs: if given, only the show folders whose absolute path is in this
    set are processed. Lets callers scope an expensive full-NAS walk down to just
    the few shows that changed (e.g. the ones that just finished downloading).

    Returns summary: {"renamed": N, "skipped": N, "errors": N}
    """
    renamed = skipped = errors = 0
    skip_norm = {os.path.normcase(os.path.normpath(s)) for s in (skip_paths or set())}
    only_norm = (
        {os.path.normcase(os.path.normpath(s)) for s in only_show_dirs}
        if only_show_dirs is not None else None
    )

    for lib_path_str in library_paths:
        lib_path = Path(lib_path_str)
        if not lib_path.exists():
            logger.warning("[Renamer] Library path not found: %s", lib_path_str)
            continue

        try:
            show_dirs = [d for d in lib_path.iterdir() if d.is_dir()]
        except PermissionError:
            logger.warning("[Renamer] Permission denied: %s", lib_path_str)
            continue

        for show_dir in show_dirs:
            # Scoped run: skip shows that didn't change this cycle (avoids
            # descending into hundreds of show folders over the NAS).
            if only_norm is not None and \
                    os.path.normcase(os.path.normpath(str(show_dir))) not in only_norm:
                continue
            show_name     = _clean_show_name(show_dir.name)
            use_titles    = _detect_naming_pattern(show_dir) == "with_title"
            try:
                season_dirs = [d for d in show_dir.iterdir() if d.is_dir() and _is_season_folder(d.name)]
            except PermissionError:
                logger.warning("[Renamer] Permission denied: %s", show_dir)
                continue

            for season_dir in season_dirs:
                try:
                    entries = list(season_dir.iterdir())
                except PermissionError:
                    logger.warning("[Renamer] Permission denied: %s", season_dir)
                    continue

                for entry in entries:
                    # Skip anything that belongs to a torrent still downloading —
                    # we must not move a partially-downloaded file.
                    if _is_under(entry, skip_norm):
                        logger.debug("[Renamer] Skipping (torrent still downloading): %s", entry.name)
                        skipped += 1
                        continue

                    if entry.is_dir():
                        r, s, e = _process_release_folder(
                            entry, season_dir, show_name, dry_run, use_titles=use_titles
                        )
                        renamed += r; skipped += s; errors += e
                        continue

                    if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
                        se = _extract_se(entry.name)
                        if not se:
                            skipped += 1
                            continue
                        season, episode, end_ep = se
                        target_name = _make_clean_name(
                            show_name, season, episode, end_ep, entry.suffix.lower(),
                            include_title=use_titles,
                        )
                        dst = season_dir / target_name
                        # If already at the clean target name, nothing to do.
                        if entry.name == target_name:
                            skipped += 1
                            continue
                        # If the clean-named file already exists OR any other video
                        # for this episode already exists, the current file is a
                        # duplicate.  Delete scene-named (ugly) files; skip clean ones.
                        existing = _find_episode_file(season_dir, season, episode)
                        already_present = dst.exists() or (existing and existing != entry)
                        if already_present:
                            present_name = dst.name if dst.exists() else (existing.name if existing else "?")
                            if entry.name != target_name and not dry_run:
                                try:
                                    entry.unlink()
                                    logger.info(
                                        "[Renamer] Deleted scene-named duplicate (episode already present as %s): %s",
                                        present_name, entry.name,
                                    )
                                    renamed += 1
                                except OSError as _e:
                                    logger.warning("[Renamer] Could not delete %s: %s", entry.name, _e)
                                    skipped += 1
                            else:
                                skipped += 1
                            continue
                        if dry_run:
                            logger.info("[Renamer] DRY RUN: %s → %s", entry.name, target_name)
                            renamed += 1
                            continue
                        try:
                            entry.rename(dst)
                            logger.info("[Renamer] %s → %s", entry.name, target_name)
                            renamed += 1
                        except OSError as e:
                            logger.error("[Renamer] Failed to rename %s: %s", entry.name, e)
                            errors += 1

    logger.info("[Renamer] Done — renamed: %d, skipped: %d, errors: %d", renamed, skipped, errors)
    return {"renamed": renamed, "skipped": skipped, "errors": errors}


def _process_release_folder(
    release_dir: Path,
    season_dir: Path,
    show_name: str,
    dry_run: bool,
    use_titles: bool = False,
) -> tuple[int, int, int]:
    """
    Move video file(s) from a release subfolder up to season_dir with a clean name.
    Never deletes any files — junk files (NFO, SFV, etc.) are left in the subfolder.
    Returns (renamed, skipped, errors).
    """
    renamed = skipped = errors = 0

    try:
        contents    = list(release_dir.iterdir())
        video_files = [f for f in contents if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
    except PermissionError:
        video_files = []
        for ext in (".mkv", ".mp4", ".avi"):
            candidate = release_dir / (release_dir.name + ext)
            if candidate.exists():
                video_files = [candidate]
                break
        if not video_files:
            logger.warning("[Renamer] Permission denied and no guessable file in: %s", release_dir.name)
            return 0, 0, 1

    if not video_files:
        # Folder has no video (NFO/SFV only) — try to remove the empty/junk folder
        _try_remove_folder(release_dir)
        return 0, 1, 0

    all_moved = True  # track whether every video was successfully moved out

    for video in video_files:
        se = _extract_se(video.name) or _extract_se(release_dir.name)
        if not se:
            skipped += 1
            all_moved = False
            continue

        season, episode, end_ep = se
        target_name = _make_clean_name(
            show_name, season, episode, end_ep, video.suffix.lower(),
            include_title=use_titles,
        )
        dst = season_dir / target_name

        # Skip and clean up the source if this episode already exists in the
        # season folder under ANY name (clean-named or original-format).
        existing = _find_episode_file(season_dir, season, episode)
        if dst.exists() or (existing and existing != video):
            present_name = dst.name if dst.exists() else (existing.name if existing else "?")
            logger.debug(
                "[Renamer] Episode S%02dE%02d already present as %s — removing source: %s",
                season, episode, present_name, video.name,
            )
            if dry_run:
                skipped += 1
                continue
            try:
                video.unlink()
                logger.info("[Renamer] Removed duplicate source (episode already present): %s", video.name)
                renamed += 1
            except OSError as e:
                logger.warning("[Renamer] Could not remove duplicate %s (locked?): %s", video.name, e)
                skipped += 1
                all_moved = False
            continue

        if dry_run:
            logger.info("[Renamer] DRY RUN: %s/%s → %s", release_dir.name, video.name, target_name)
            renamed += 1
            continue

        try:
            shutil.move(str(video), str(dst))
            logger.info("[Renamer] %s → %s", video.name, target_name)
            renamed += 1
        except OSError as e:
            if "used by another process" in str(e) or "[WinError 32]" in str(e):
                logger.debug("[Renamer] File in use (still seeding?), will retry next run: %s", video.name)
                skipped += 1
                all_moved = False
            else:
                logger.error("[Renamer] Failed to move %s: %s", video.name, e)
                errors += 1
                all_moved = False

    # After moving/removing all videos, delete the release subfolder.
    # Only do so when every video was successfully handled (nothing still locked).
    if all_moved and not dry_run:
        _try_remove_folder(release_dir)

    return renamed, skipped, errors


def cleanup_library(library_paths: list[str], dry_run: bool = False) -> dict:
    """Alias kept for backwards compatibility — delegates to cleanup_duplicates."""
    return cleanup_duplicates(library_paths, dry_run=dry_run)


# Priority order when deciding which duplicate to KEEP (higher index = preferred).
# .mp4 is ranked above .mkv so that existing library files are never discarded in
# favour of newly downloaded MKV copies.
_EXT_PREFERENCE = {".avi": 0, ".wmv": 0, ".mov": 1, ".m4v": 1, ".mkv": 2, ".ts": 2, ".mp4": 3}


def _ext_rank(path: Path) -> tuple[int, int]:
    """Return (extension_rank, file_size_bytes) for sorting — higher = better."""
    rank = _EXT_PREFERENCE.get(path.suffix.lower(), 0)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return (rank, size)


def cleanup_duplicates(library_paths: list[str], dry_run: bool = False) -> dict:
    """
    Scan every season folder across all library paths and remove duplicate episode
    files — i.e. multiple video files that map to the same SxxExx episode number.

    Keeps the "best" copy (prefers .mkv over .mp4 over others; breaks ties by
    file size — larger = higher quality).  The losers are deleted.

    Also removes any leftover empty release sub-folders.

    Returns {"deleted_files": N, "deleted_folders": N, "errors": N}
    """
    deleted_files = deleted_folders = errors = 0

    for lib_path_str in library_paths:
        lib_path = Path(lib_path_str)
        if not lib_path.exists():
            continue

        try:
            show_dirs = [d for d in lib_path.iterdir() if d.is_dir()]
        except OSError:
            continue

        for show_dir in show_dirs:
            try:
                season_dirs = [
                    d for d in show_dir.iterdir()
                    if d.is_dir() and _is_season_folder(d.name)
                ]
            except OSError:
                continue

            for season_dir in season_dirs:
                # Group all video files by (season, episode) number
                by_episode: dict[tuple[int, int], list[Path]] = {}
                try:
                    for f in season_dir.iterdir():
                        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTENSIONS:
                            continue
                        se = _extract_se(f.name)
                        if se:
                            key = (se[0], se[1])
                            by_episode.setdefault(key, []).append(f)
                except OSError:
                    continue

                # For each episode with multiple files, keep the best and delete the rest
                for (s_num, e_num), files in by_episode.items():
                    if len(files) < 2:
                        continue
                    # Sort best-last so we can keep the last element
                    files_sorted = sorted(files, key=_ext_rank)
                    keep   = files_sorted[-1]
                    losers = files_sorted[:-1]
                    logger.info(
                        "[Cleanup] S%02dE%02d in %s — keeping %s, removing %d duplicate(s)",
                        s_num, e_num, season_dir.name, keep.name, len(losers),
                    )
                    for loser in losers:
                        if dry_run:
                            logger.info("[Cleanup] DRY RUN would delete: %s", loser.name)
                            deleted_files += 1
                            continue
                        try:
                            loser.unlink()
                            logger.info("[Cleanup] Deleted duplicate: %s", loser.name)
                            deleted_files += 1
                        except OSError as exc:
                            logger.warning("[Cleanup] Could not delete %s: %s", loser.name, exc)
                            errors += 1

                # Remove any empty release sub-folders left behind
                try:
                    for sub in season_dir.iterdir():
                        if sub.is_dir() and not any(sub.iterdir()):
                            if not dry_run:
                                sub.rmdir()
                                logger.info("[Cleanup] Removed empty folder: %s", sub.name)
                            deleted_folders += 1
                except OSError:
                    pass

    logger.info(
        "[Cleanup] Done — deleted %d duplicate file(s), %d empty folder(s), %d error(s)",
        deleted_files, deleted_folders, errors,
    )
    return {"deleted_files": deleted_files, "deleted_folders": deleted_folders, "errors": errors}
