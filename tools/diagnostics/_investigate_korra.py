"""
Investigate why The Legend Of Korra is being re-downloaded despite being on disk.
"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from pathlib import Path
from scanner import scan_tv_library, clean_show_name
from downloader import normalise_show_name

LIB_PATH = r"\\192.168.0.181\Jellyfin5\Adult Animations & Cartoons"
SHOW_DIR  = r"\\192.168.0.181\Jellyfin5\Adult Animations & Cartoons\The Legend Of Korra"

print("=" * 60)
print("1. RAW DISK FOLDER NAME")
print("=" * 60)
folder_name = Path(SHOW_DIR).name
print(f"  Folder on disk : {folder_name!r}")
cleaned = clean_show_name(folder_name)
print(f"  After clean    : {cleaned!r}")
normed = normalise_show_name(cleaned)
print(f"  After normalise: {normed!r}")

print()
print("=" * 60)
print("2. WHAT THE SCANNER RETURNS FOR THIS LIBRARY")
print("=" * 60)
inventory = scan_tv_library(LIB_PATH)
korra_key = None
for k in inventory:
    if "korra" in k.lower():
        korra_key = k
        break

if korra_key:
    seasons = inventory[korra_key]
    print(f"  Scanner key    : {korra_key!r}")
    print(f"  Seasons found  : {sorted(seasons.keys())}")
    for s, eps in sorted(seasons.items()):
        print(f"    Season {s}: {len(eps)} episodes -> {sorted(eps)}")
else:
    print("  NOT FOUND in scanner inventory!")
    print(f"  All keys: {list(inventory.keys())}")

print()
print("=" * 60)
print("3. MATCH TEST: TVMaze name vs disk inventory key")
print("=" * 60)
tvmaze_names = ["The Legend of Korra", "The Legend Of Korra", "Legend of Korra"]
for name in tvmaze_names:
    norm = normalise_show_name(name)
    disk_norm = normalise_show_name(korra_key) if korra_key else "N/A"
    match = "MATCH" if norm == disk_norm else "NO MATCH"
    print(f"  TVMaze: {name!r:35s} -> norm: {norm!r:35s} [{match}]")

print()
print("=" * 60)
print("4. COOLDOWN / STATE CHECK")
print("=" * 60)
import json as _json
state = _json.load(open("state.json", encoding="utf-8"))
queued = state.get("queued_episodes", {})
korra_queued = {k: v for k, v in queued.items() if "korra" in k.lower()}
if korra_queued:
    print(f"  Found {len(korra_queued)} queued entries for Korra:")
    for k, v in list(korra_queued.items())[:20]:
        print(f"    {k}: {v}")
else:
    print("  No queued_episodes entries for Korra.")

print()
print("=" * 60)
print("5. _match_disk_show TEST")
print("=" * 60)
# Simulate what _match_disk_show does
from downloader import normalise_show_name
disk_keys = list(inventory.keys())
for tvmaze_name in ["The Legend of Korra", "The Legend Of Korra"]:
    norm_target = normalise_show_name(tvmaze_name)
    matched = None
    for dk in disk_keys:
        if normalise_show_name(dk) == norm_target:
            matched = dk
            break
    print(f"  TVMaze {tvmaze_name!r} -> disk match: {matched!r}")
