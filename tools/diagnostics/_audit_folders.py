"""
Deep audit: find every show subfolder that our _parse_season_folder returns None for,
group them by pattern, and report which shows would have blind spots.
"""
import sys, io, re, json
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from scanner import _parse_season_folder, VIDEO_EXTS

cfg = json.load(open("config.json"))
tv_libs = [
    l for l in cfg.get("libraries", [])
    if l.get("type", "").lower() in ("tv", "animation") and l.get("enabled", True)
]

# Patterns we want to catch — test these against the current parser
unrecognised: dict[str, list[str]] = defaultdict(list)  # pattern → [example paths]
recognised_patterns: dict[str, int] = defaultdict(int)

for lib in tv_libs:
    lib_path = lib.get("path", "")
    root = Path(lib_path)
    if not root.exists():
        continue
    try:
        show_dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        continue

    for show_dir in show_dirs:
        try:
            entries = [d for d in show_dir.iterdir() if d.is_dir()]
        except OSError:
            continue

        for entry in entries:
            parsed = _parse_season_folder(entry.name)
            if parsed is not None:
                # Recognised — classify the pattern
                pat = re.sub(r"\d+", "N", entry.name)
                recognised_patterns[pat] += 1
            else:
                # Unrecognised — check if it has video files (a real miss, not just extras)
                try:
                    has_video = any(
                        f.suffix.lower() in VIDEO_EXTS
                        for f in entry.iterdir()
                        if f.is_file()
                    )
                except OSError:
                    has_video = False

                if has_video:
                    pat = re.sub(r"\d+", "N", entry.name)
                    unrecognised[pat].append(f"{show_dir.name} / {entry.name}")

print("=" * 70)
print("SEASON FOLDER AUDIT — UNRECOGNISED PATTERNS WITH VIDEO FILES")
print("=" * 70)
if not unrecognised:
    print("\nNone — every season folder is correctly parsed.\n")
else:
    for pat, examples in sorted(unrecognised.items(), key=lambda x: -len(x[1])):
        print(f"\nPattern: {pat!r}  ({len(examples)} occurrence(s))")
        for ex in examples[:5]:
            print(f"  {ex}")
        if len(examples) > 5:
            print(f"  ... and {len(examples)-5} more")

print()
print("=" * 70)
print("RECOGNISED PATTERNS (top 15)")
print("=" * 70)
for pat, count in sorted(recognised_patterns.items(), key=lambda x: -x[1])[:15]:
    print(f"  {count:5d}x  {pat!r}")
