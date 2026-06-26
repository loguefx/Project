"""
Detect and remove duplicate TV episode files on the NAS.

Most common case: a combined multi-episode file (e.g. "Show - S15E05-E06.mkv")
exists alongside a single-episode file it already contains ("Show - S15E06.mkv").
The single is redundant and should be removed, keeping the combined file.

Also handles exact duplicates: two files covering the SAME episode(s) (e.g. two
different releases). The preferred copy is kept (more episodes first, then .mp4
over .mkv, then the larger file) and the rest are flagged for removal.

This is a FAST, filename-only scan — no ffprobe/decoding — so it can back a quick
"Find Duplicates" button.
"""

import logging
import os
import re
from pathlib import Path

from scanner import (
    VIDEO_EXTS,
    parse_tv_filename,
    _parse_season_folder,
    _TV_MULTI_EP_RE,
    clean_show_name,
)

logger = logging.getLogger(__name__)


def episodes_covered(filename: str) -> set[int]:
    """Return the set of episode numbers a filename covers.

    Handles single episodes (S15E06) and multi-episode files in any common
    style (S15E05E06, S15E05-E06, S15E05 - E06, S15E05.E06).
    """
    parsed = parse_tv_filename(filename)
    if not parsed:
        return set()
    _, ep, _ = parsed
    eps = {ep}
    multi = _TV_MULTI_EP_RE.search(filename)
    if multi:
        for s in re.findall(r"E(\d{1,3})", multi.group(0), re.IGNORECASE):
            eps.add(int(s))
    return eps


def _keep_priority(path: Path) -> tuple:
    """Sort key — the HIGHEST tuple is the copy we keep.

    Priority order:
      1. Covers more episodes  (a combined file beats the singles it contains)
      2. .mp4 over other containers (user preference)
      3. Larger file size (usually the better encode)
    """
    eps = episodes_covered(path.name)
    is_mp4 = 1 if path.suffix.lower() == ".mp4" else 0
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return (len(eps), is_mp4, size)


def _find_duplicates_in_season(season_dir: Path, show_name: str, season_num: int) -> list[dict]:
    """Return a list of redundant files in one season folder.

    Greedy, loss-safe algorithm: keep the highest-priority files first and only
    flag a file for removal when EVERY episode it covers is already provided by a
    higher-priority file we're keeping. This guarantees we never delete a file
    whose unique episode would otherwise be lost (e.g. overlapping combined files
    S05-E06 and S06-E07 are both kept).
    """
    try:
        files = [
            f for f in season_dir.iterdir()
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS and episodes_covered(f.name)
        ]
    except OSError:
        return []

    # Highest priority first.
    files.sort(key=_keep_priority, reverse=True)

    covered: dict[int, Path] = {}   # episode number -> the kept file that provides it
    dups: list[dict] = []
    for f in files:
        eps = episodes_covered(f.name)
        if eps and all(e in covered for e in eps):
            keeper = covered[sorted(eps)[0]]
            try:
                size_mb = round(f.stat().st_size / (1024 * 1024), 1)
            except OSError:
                size_mb = 0
            ep_list = ", ".join(f"E{e:02d}" for e in sorted(eps))
            dups.append({
                "show": show_name,
                "season": season_num,
                "remove_path": str(f),
                "remove_name": f.name,
                "keep_name": keeper.name,
                "episodes": sorted(eps),
                "size_mb": size_mb,
                "reason": f"{ep_list} already provided by '{keeper.name}'",
            })
        else:
            for e in eps:
                covered.setdefault(e, f)
    return dups


def find_duplicate_episodes(library_paths: list[str]) -> list[dict]:
    """Scan every show/season in the given TV/animation library paths and return
    a list of duplicate (redundant) episode files that can be safely removed."""
    results: list[dict] = []
    for lib in library_paths:
        root = Path(lib)
        if not root.exists():
            logger.debug("[Duplicates] Library path missing: %s", lib)
            continue
        try:
            show_dirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for show_dir in show_dirs:
            show_name = clean_show_name(show_dir.name)
            try:
                children = list(show_dir.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_dir():
                    continue
                season_num = _parse_season_folder(child.name)
                if season_num is None:
                    continue
                results.extend(_find_duplicates_in_season(child, show_name, season_num))
    logger.info("[Duplicates] Scan found %d duplicate file(s) across %d library path(s).",
                len(results), len(library_paths))
    return results


def remove_duplicate_files(paths: list[str]) -> dict:
    """Delete the given duplicate files. Returns {removed: [...], errors: [...]}."""
    removed: list[str] = []
    errors: list[dict] = []
    for p in paths:
        try:
            os.remove(p)
            removed.append(p)
            logger.info("[Duplicates] Removed redundant file: %s", p)
        except FileNotFoundError:
            # Already gone — treat as success.
            removed.append(p)
        except Exception as exc:
            errors.append({"path": p, "error": str(exc)})
            logger.warning("[Duplicates] Could not remove %s: %s", p, exc)
    return {"removed": removed, "errors": errors}
