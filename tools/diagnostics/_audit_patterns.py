"""
Deep audit of every filename and season-folder name on the NAS.
Tests each one against the scanner's parse_tv_filename and _parse_season_folder.
Reports any that return None — those are blind spots that can cause duplicate downloads.
"""
import sys, io, json, re, logging
from pathlib import Path
from collections import defaultdict

logging.basicConfig(level=logging.WARNING)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from scanner import parse_tv_filename, _parse_season_folder

cfg = json.load(open("config.json"))
tv_libs = [
    l for l in cfg.get("libraries", [])
    if l.get("type", "").lower() in ("tv", "animation") and l.get("enabled", True)
]

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts"}

# ── Collect every sample ─────────────────────────────────────────────────────
# file_samples[pattern_key] = list of (full_path, filename)
unrecognised_files   = []   # parse_tv_filename returned None
unrecognised_folders = []   # _parse_season_folder returned None but name looks season-like
all_file_formats     = defaultdict(list)   # bucket by detected pattern
all_folder_formats   = defaultdict(list)

SEASON_HINT_RE = re.compile(r"(?i)(season|s\d{1,2})\s*\d{1,2}")
SE_RE          = re.compile(r"[Ss]\d{1,2}[Ee]\d{1,2}")

total_video_files = 0
total_season_dirs = 0

print("Collecting samples from all libraries...\n", flush=True)

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
            entries = list(show_dir.iterdir())
        except OSError:
            continue

        for entry in entries:
            if not entry.is_dir():
                continue

            # ── Season folder audit ───────────────────────────────────────────
            season_num = _parse_season_folder(entry.name)
            if season_num is not None:
                total_season_dirs += 1
                all_folder_formats[season_num].append(entry.name)
            else:
                # Flag if it looks season-like but wasn't recognised
                if SEASON_HINT_RE.search(entry.name):
                    unrecognised_folders.append((str(show_dir), entry.name))

            # ── Episode file audit ────────────────────────────────────────────
            try:
                files = list(entry.iterdir())
            except OSError:
                continue

            for f in files:
                if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                    continue
                total_video_files += 1
                parsed = parse_tv_filename(f.name)
                if parsed is None:
                    unrecognised_files.append((str(entry), f.name))
                else:
                    # Classify by structural pattern (replace numbers with #)
                    pat = re.sub(r"\d+", "#", f.name)
                    pat = re.sub(r"[A-Za-z]{4,}", "WORD", pat)
                    all_file_formats[pat].append(f.name)

        # Also check for video files directly inside show_dir (no season subfolder)
        for entry in entries:
            if entry.is_file() and entry.suffix.lower() in VIDEO_EXTS:
                total_video_files += 1
                parsed = parse_tv_filename(entry.name)
                if parsed is None:
                    unrecognised_files.append((str(show_dir), entry.name))

# ── Report ────────────────────────────────────────────────────────────────────
print("=" * 70)
print("NAS PATTERN AUDIT REPORT")
print("=" * 70)
print(f"Total video files scanned : {total_video_files}")
print(f"Total season folders      : {total_season_dirs}")
print()

# Season folder formats found
print("── SEASON FOLDER FORMATS RECOGNISED ──")
seen_folder_examples = set()
for snum, examples in sorted(all_folder_formats.items())[:5]:
    unique = list(dict.fromkeys(examples))[:4]
    for ex in unique:
        pat = re.sub(r"\d+", "N", ex)
        if pat not in seen_folder_examples:
            seen_folder_examples.add(pat)
            print(f"  Pattern: {pat!r:45s} e.g. {ex!r}")

print()
print("── SEASON FOLDERS NOT RECOGNISED (but look season-like) ──")
if unrecognised_folders:
    seen = set()
    for show_path, name in unrecognised_folders:
        pat = re.sub(r"\d+", "N", name)
        if pat not in seen:
            seen.add(pat)
            print(f"  Pattern : {pat!r:45s}  e.g. {name!r}")
            print(f"  Location: {show_path}")
else:
    print("  None — all season-like folder names are recognised.")

print()
print("── VIDEO FILES NOT RECOGNISED BY parse_tv_filename ──")
if unrecognised_files:
    seen = set()
    for folder_path, name in unrecognised_files:
        # Only report if it actually has an SxxExx somewhere (genuine miss)
        if SE_RE.search(name):
            pat = re.sub(r"\d+", "N", name)
            pat = re.sub(r"\.[a-zA-Z0-9]+$", ".EXT", pat)
            if pat not in seen:
                seen.add(pat)
                print(f"  Pattern : {pat!r}")
                print(f"  Example : {name!r}")
                print(f"  Location: {folder_path}")
                print()
    no_se = [(p, n) for p, n in unrecognised_files if not SE_RE.search(n)]
    if no_se:
        print(f"  (+ {len(no_se)} files with no SxxExx pattern at all — legitimately unrecognisable)")
else:
    print("  None — every video file is correctly parsed.")

print()
print("── FILE FORMAT EXAMPLES BY PATTERN ──")
pattern_counts = sorted(all_file_formats.items(), key=lambda x: -len(x[1]))
shown = 0
for pat, examples in pattern_counts:
    if shown >= 20:
        break
    ex = examples[0]
    print(f"  {len(examples):4d}x  {ex!r}")
    shown += 1

print()
print("=" * 70)
unrecog_with_se = [(p, n) for p, n in unrecognised_files if SE_RE.search(n)]
if not unrecognised_folders and not unrecog_with_se:
    print("RESULT: PASS — every season folder and episode file is correctly parsed.")
else:
    print(f"RESULT: ISSUES FOUND")
    print(f"  Season folders not recognised : {len(unrecognised_folders)}")
    print(f"  Episode files not recognised  : {len(unrecog_with_se)}")
    print()
    print("  => Fix scanner.py _parse_season_folder and/or parse_tv_filename")
    print("     for the patterns listed above.")
