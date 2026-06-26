"""
Comprehensive scanner audit.

For every show in every configured library, this script:
  1. Walks the directory tree and counts every video file by SxxExx pattern
     (or sequentially if no episode pattern is found in filenames).
  2. Runs the scanner and gets its inventory for the same path.
  3. Compares the two — flags any show where the scanner reports FEWER
     episodes than what is actually on disk (the dangerous case that triggers
     re-downloads of content the user already owns).

Outputs both a per-show report and a summary list of show folders/patterns
that the scanner is mis-parsing so the regex set can be fixed.
"""
from __future__ import annotations

import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import scanner  # noqa: E402

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts"}
SE_RE = re.compile(r"S(\d{1,2})E(\d{1,2})", re.IGNORECASE)

config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# 1. Raw disk walk
# ---------------------------------------------------------------------------

def raw_inventory(show_dir: Path) -> dict[int, set[int]]:
    """
    Walk a show folder and return {season: {episodes}} purely from filenames.
    If a season folder contains video files but NONE of them have a SxxExx
    pattern, assume sequential episodes 1..N (Blu-ray T01/T02 style).
    """
    out: dict[int, set[int]] = defaultdict(set)
    if not show_dir.exists():
        return {}
    try:
        for season_dir in show_dir.iterdir():
            if not season_dir.is_dir():
                # Episode/movie file directly in show folder
                if season_dir.is_file() and season_dir.suffix.lower() in VIDEO_EXTS:
                    m = SE_RE.search(season_dir.name)
                    if m:
                        out[int(m.group(1))].add(int(m.group(2)))
                continue
            # Try to learn season from filenames first
            video_files = []
            try:
                for f in season_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                        video_files.append(f)
            except OSError:
                continue
            if not video_files:
                continue

            matched_any = False
            for f in video_files:
                for m in SE_RE.finditer(f.name):
                    out[int(m.group(1))].add(int(m.group(2)))
                    matched_any = True

            if not matched_any:
                # Fallback: infer season from folder name, episodes 1..N
                season_num = scanner._parse_season_folder(season_dir.name)
                if season_num is not None:
                    for ep_num in range(1, len(video_files) + 1):
                        out[season_num].add(ep_num)
    except OSError as exc:
        print(f"  ERROR walking {show_dir}: {exc}")

    return dict(out)


# ---------------------------------------------------------------------------
# 2. Run scanner and compare
# ---------------------------------------------------------------------------

total_libraries     = 0
total_shows         = 0
total_eps_disk      = 0
total_eps_scanner   = 0
missing_to_scanner  = []   # shows where scanner reports fewer eps than disk has

print("=" * 70)
print("SCANNER AUDIT — comparing scanner inventory vs raw disk walk")
print("=" * 70)

for lib in config.get("libraries", []):
    if not lib.get("enabled", True):
        continue
    lib_type = lib.get("type", "").lower()
    if lib_type not in ("tv", "animation"):
        continue
    total_libraries += 1
    lib_path = lib.get("path", "")
    lib_name = lib.get("name", "?")

    print(f"\n[{lib_name}] {lib_path}")

    try:
        # The scanner runs in parallel with a thread pool — let it do its thing
        scanner_inv = scanner.scan_tv_library(lib_path)
    except Exception as exc:
        print(f"  SCANNER ERROR: {exc}")
        continue

    root = Path(lib_path)
    if not root.exists():
        print("  (path does not exist)")
        continue

    try:
        show_dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError as exc:
        print(f"  Cannot list: {exc}")
        continue

    for show_dir in sorted(show_dirs):
        total_shows += 1
        disk = raw_inventory(show_dir)
        scn  = scanner_inv.get(show_dir.name, {})
        disk_eps = sum(len(v) for v in disk.values())
        scn_eps  = sum(len(v) for v in scn.values())
        total_eps_disk    += disk_eps
        total_eps_scanner += scn_eps

        # Look for episodes on disk that the scanner missed
        gaps: list[tuple[int, set[int]]] = []
        for s, eps in disk.items():
            scn_eps_set = scn.get(s, set())
            missed = eps - scn_eps_set
            if missed:
                gaps.append((s, missed))

        if gaps:
            missing_to_scanner.append({
                "show":      show_dir.name,
                "path":      str(show_dir),
                "library":   lib_name,
                "disk_eps":  disk_eps,
                "scn_eps":   scn_eps,
                "gaps":      gaps,
            })

# ---------------------------------------------------------------------------
# 3. Report
# ---------------------------------------------------------------------------

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"  Libraries scanned   : {total_libraries}")
print(f"  Shows               : {total_shows}")
print(f"  Episodes on disk    : {total_eps_disk}")
print(f"  Episodes scanner saw: {total_eps_scanner}")
print(f"  Mis-detected shows  : {len(missing_to_scanner)}")

if missing_to_scanner:
    print("\n" + "=" * 70)
    print("SHOWS WHERE SCANNER UNDER-COUNTS (will trigger false re-downloads)")
    print("=" * 70)
    for entry in missing_to_scanner:
        print(f"\n  {entry['show']}  ({entry['library']})")
        print(f"    Path : {entry['path']}")
        print(f"    Disk : {entry['disk_eps']} eps  |  Scanner : {entry['scn_eps']} eps")
        for s, missed in entry["gaps"]:
            sample = sorted(missed)[:10]
            more = f" (+{len(missed)-10} more)" if len(missed) > 10 else ""
            print(f"    S{s:02d} missed in scanner: {sample}{more}")
else:
    print("\n  ALL CLEAR — scanner sees every episode on disk.")
