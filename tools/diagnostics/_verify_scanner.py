"""
Verify that the scanner correctly detects every episode that exists on disk.
Prints a full report: libraries, show counts, and any gaps the scanner misses.
"""
import sys
import io
import json
import re
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from scanner import scan_tv_library

cfg = json.load(open("config.json"))
tv_libs = [
    l for l in cfg.get("libraries", [])
    if l.get("type", "").lower() in ("tv", "animation") and l.get("enabled", True)
]

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv"}
SE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
SEASON_RE = re.compile(r"(?i)season\s+(\d+)|^season(\d+)$|^s(\d+)$")

total_shows = 0
shows_folder_only = 0
scanner_missed = []
scanner_ok = 0
lib_summaries = []

for lib in tv_libs:
    lib_path = lib.get("path", "")
    lib_name = lib.get("name", "?")
    root = Path(lib_path)
    if not root.exists():
        print(f"[SKIP] Library path not found: {lib_path}")
        continue

    print(f"\nScanning [{lib_name}]  {lib_path} ...", flush=True)
    try:
        inventory = scan_tv_library(lib_path)
    except Exception as exc:
        print(f"  [ERROR] scan_tv_library failed: {exc}")
        continue

    lib_shows = 0
    lib_seasons_ok = 0
    lib_seasons_bad = 0

    try:
        show_dir_list = sorted(root.iterdir())
    except OSError as exc:
        print(f"  [ERROR] Cannot list library: {exc}")
        continue

    for show_dir in show_dir_list:
        if not show_dir.is_dir():
            continue
        total_shows += 1
        lib_shows += 1
        show_key = show_dir.name

        # Build ground-truth from disk
        disk_seasons: dict[int, set[int]] = {}
        try:
            for entry in show_dir.iterdir():
                if not entry.is_dir():
                    continue
                m = SEASON_RE.search(entry.name)
                if not m:
                    continue
                groups = [g for g in m.groups() if g is not None]
                if not groups:
                    continue
                s_num = int(groups[0])
                eps: set[int] = set()
                try:
                    for f in entry.iterdir():
                        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                            se = SE_RE.search(f.name)
                            if se:
                                eps.add(int(se.group(2)))
                except OSError:
                    pass
                if eps:
                    disk_seasons.setdefault(s_num, set()).update(eps)
        except OSError:
            continue

        if not disk_seasons:
            shows_folder_only += 1
            continue

        scanned = inventory.get(show_key, {})
        for s_num, disk_eps in sorted(disk_seasons.items()):
            scanned_eps = scanned.get(s_num, set())
            missed = disk_eps - scanned_eps
            if missed:
                scanner_missed.append({
                    "lib": lib_name,
                    "show": show_key,
                    "season": s_num,
                    "on_disk": sorted(disk_eps),
                    "scanner_saw": sorted(scanned_eps),
                    "missed": sorted(missed),
                })
                lib_seasons_bad += 1
            else:
                scanner_ok += 1
                lib_seasons_ok += 1

    lib_summaries.append((lib_name, lib_shows, lib_seasons_ok, lib_seasons_bad))
    status = "OK" if lib_seasons_bad == 0 else f"GAPS IN {lib_seasons_bad} SEASON(S)"
    print(f"  -> {lib_shows} shows, {lib_seasons_ok} seasons verified, {lib_seasons_bad} seasons with gaps  [{status}]", flush=True)

# ── Final report ────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("SCANNER VERIFICATION REPORT")
print("=" * 70)
print(f"Libraries scanned       : {len(tv_libs)}")
print(f"Total show folders      : {total_shows}")
print(f"  Folder-only (no eps)  : {shows_folder_only}")
print(f"Seasons verified OK     : {scanner_ok}")
print(f"Seasons with missed eps : {len(scanner_missed)}")
print()

if not scanner_missed:
    print("RESULT: PASS — Scanner correctly sees every episode on disk.")
else:
    print("RESULT: GAPS FOUND — scanner is missing some episodes:\n")
    for item in scanner_missed:
        print(f"  [{item['lib']}] {item['show']}  S{item['season']:02d}")
        print(f"    Disk   : {item['on_disk']}")
        print(f"    Scanner: {item['scanner_saw']}")
        print(f"    MISSED : {item['missed']}")
        print()
