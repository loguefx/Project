"""Reformat _broken_report.txt into a clean per-episode breakdown."""
import io, sys, re
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

report = Path("_broken_report.txt").read_text(encoding="utf-8")

# show -> season -> [episode strings]
broken: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
show_paths: dict[str, str] = {}
season_paths: dict[tuple[str, str], str] = {}
season_totals: dict[tuple[str, str], tuple[int, int]] = {}  # (broken, total)

current_show = None
current_show_path = None
current_season = None

lines = report.splitlines()
for i, line in enumerate(lines):
    # Show header
    m_show = re.match(r"^\s{2}([A-Z][\w'!]*(?:\s[\w'!]+)*)$", line)
    # Show header lines come after "===" lines
    if line.strip().startswith("===") and i + 1 < len(lines):
        nxt = lines[i + 1]
        if nxt.startswith("  ") and not nxt.strip().startswith("Path:"):
            current_show = nxt.strip()
            # Path is next line
            if i + 2 < len(lines):
                path_line = lines[i + 2].strip()
                if path_line.startswith("Path:"):
                    current_show_path = path_line[5:].strip()
                    show_paths[current_show] = current_show_path

    # Season line: "  Season 1: 25 file(s)  |  17 ok  |  8 broken  (13.3s)"
    m_season = re.match(r"^\s{2}([\w ]+?):\s+(\d+)\s+file\(s\)\s+\|\s+(\d+)\s+ok\s+\|\s+(\d+)\s+broken", line)
    if m_season and current_show:
        current_season = m_season.group(1).strip()
        total = int(m_season.group(2))
        ok    = int(m_season.group(3))
        bad   = int(m_season.group(4))
        season_totals[(current_show, current_season)] = (bad, total)

    # Broken file line: "      [BAD]  ShowName - SxxExx.mkv  (123 MB)"
    m_bad = re.match(r"^\s+\[BAD\]\s+(.+?)\s+\((\d+) MB\)", line)
    if m_bad and current_show and current_season:
        filename = m_bad.group(1).strip()
        size_mb  = int(m_bad.group(2))
        # Pull SxxExx (or SxxExx-Eyy)
        m_se = re.search(r"S(\d{2})E(\d{2})(?:-E(\d{2}))?", filename)
        if m_se:
            s = int(m_se.group(1))
            e1 = int(m_se.group(2))
            e2 = int(m_se.group(3)) if m_se.group(3) else None
            ep_str = f"S{s:02d}E{e1:02d}"
            if e2 is not None:
                ep_str = f"S{s:02d}E{e1:02d}-E{e2:02d}"
        else:
            ep_str = filename
        broken[current_show][current_season].append({
            "ep":       ep_str,
            "filename": filename,
            "size_mb":  size_mb,
        })

# ── Output ──────────────────────────────────────────────────────────────────
print("=" * 80)
print("  DETAILED BROKEN-EPISODE REPORT")
print("  (no files have been deleted — this is purely a list)")
print("=" * 80)
print()

total_broken_episodes = 0

for show in sorted(broken.keys(), key=str.lower):
    bad_seasons = broken[show]
    if not bad_seasons:
        continue
    total = sum(len(eps) for eps in bad_seasons.values())
    total_broken_episodes += total
    print(f"┌{'─' * 78}")
    print(f"│ {show.upper()}")
    print(f"│ Path: {show_paths.get(show, '?')}")
    print(f"│ {total} broken file(s) across {len(bad_seasons)} season(s)")
    print(f"└{'─' * 78}")

    for season in sorted(bad_seasons.keys(),
                         key=lambda s: int(re.sub(r"\D", "", s) or 0)):
        bad, total_files = season_totals.get((show, season), (0, 0))
        whole = (bad == total_files and total_files > 0)
        tag = "  ◀  WHOLE SEASON" if whole else f"  ◀  {bad} of {total_files} broken"
        print(f"\n  ▸ {season} ({len(bad_seasons[season])} broken){tag}")
        eps = bad_seasons[season]
        for ep in eps:
            label = ep["ep"]
            size  = ep["size_mb"]
            size_note = " (EMPTY 0 MB)" if size == 0 else f" ({size} MB)"
            print(f"      • {label}{size_note}")

    print()

print("=" * 80)
print(f"  TOTAL: {total_broken_episodes} broken episodes across {len(broken)} show(s)")
print("=" * 80)
